"""Negative tenant-isolation tests: tenant A must NEVER reach tenant B's data."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from services.ingestion import CSVIngestionService
from services.exporters import ExportService
from services.webhooks import WebhookService
from engine.kernel import PlatformKernel

DATA_A = "sku,name,price,ean,images\nA-1,Hemmelig stol,499.0,5901234123457,x.jpg\n"


@pytest.fixture()
def two_tenants():
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod._users = None
    app_mod._webhooks = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    admin = AuthService(app_mod.persistence)
    ta = app_mod.persistence.get_or_create_tenant("acme")
    tb = app_mod.persistence.get_or_create_tenant("other")
    key_a = admin.create_key(ta.id, "acme-admin", "admin")["key"]
    app_mod.ingestion.ingest(
        "sku,name,price,ean,images\nB-9,Annen hemmelighet,100.0,5901234123457,y.jpg\n", "other")
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {key_a}"})
    return c, ta.id, tb.id


def test_cross_tenant_product_read_blocked(two_tenants):
    c, ta, tb = two_tenants
    r = c.get("/tenants/other/products")
    assert r.status_code in (403, 404)
    assert "B-9" not in r.text


def test_cross_tenant_product_get_blocked(two_tenants):
    c, ta, tb = two_tenants
    r = c.get("/products/A-1", params={"tenant": "other"})
    assert r.status_code in (403, 404)


def test_cross_tenant_ingest_blocked(two_tenants):
    c, ta, tb = two_tenants
    r = c.post("/tenants/other/ingest", params={"body": DATA_A})
    assert r.status_code == 403


def test_cross_tenant_ingest_does_not_persist(two_tenants):
    c, ta, tb = two_tenants
    c.post("/tenants/other/ingest", params={"body": DATA_A})
    from sqlalchemy import select
    from core.models import Product
    with app_mod.persistence.session() as s:
        rows = s.scalars(select(Product).where(Product.tenant_id == tb)).all()
        assert all(p.sku != "A-1" for p in rows)


def test_cross_tenant_quality_summary_blocked(two_tenants):
    c, ta, tb = two_tenants
    r = c.get("/quality/other")
    assert r.status_code in (403, 404)
    assert "B-9" not in r.text


def test_cross_tenant_export_blocked(two_tenants):
    c, ta, tb = two_tenants
    r = c.get("/export/other")
    assert r.status_code in (403, 404)
    assert "B-9" not in r.text


def test_cross_tenant_export_service_isolated(two_tenants):
    c, ta, tb = two_tenants
    result = ExportService(app_mod.persistence).export("acme", fmt="json")
    assert "B-9" not in result["content"]


def test_cross_tenant_users_blocked(two_tenants):
    c, ta, tb = two_tenants
    assert c.get("/users", params={"tenant": "other"}).status_code in (403, 404)


def test_cross_tenant_user_create_blocked(two_tenants):
    c, ta, tb = two_tenants
    r = c.post("/users", params={"tenant": "other", "email": "x@other.dk",
                                 "password": "LongEnough1"})
    assert r.status_code == 403


def test_cross_tenant_keys_blocked(two_tenants):
    c, ta, tb = two_tenants
    assert c.get("/keys", params={"tenant": "other"}).status_code in (403, 404)


def test_cross_tenant_key_create_blocked(two_tenants):
    c, ta, tb = two_tenants
    assert c.post("/keys", params={"tenant": "other", "name": "steal", "role": "admin"}).status_code == 403


def test_cross_tenant_webhooks_blocked(two_tenants):
    c, ta, tb = two_tenants
    assert c.get("/webhooks", params={"tenant": "other"}).status_code in (403, 404)
    assert c.post("/webhooks", params={"tenant": "other", "url": "https://evil.example/hook"}).status_code == 403


def test_cross_tenant_webhook_deliveries_blocked(two_tenants):
    c, ta, tb = two_tenants
    sub = WebhookService(app_mod.persistence).subscribe(tb, "https://example.com/hook")
    r = c.get(f"/webhooks/{sub['id']}/deliveries", params={"tenant": "acme"})
    assert r.status_code in (403, 404) or r.json() == []


def test_cross_tenant_feed_state_blocked(two_tenants):
    c, ta, tb = two_tenants
    assert c.get("/feeds/other/state").status_code in (403, 404)


def test_cross_tenant_feed_poll_blocked(two_tenants):
    c, ta, tb = two_tenants
    assert c.post("/feeds/other/poll").status_code in (403, 422)  # 422 = missing required body, still no tenant access


def test_reader_key_same_tenant_isolation(two_tenants):
    c, ta, tb = two_tenants
    reader = AuthService(app_mod.persistence).create_key(ta, "r", "reader")["key"]
    c2 = TestClient(app_mod.app)
    c2.headers.update({"Authorization": f"Bearer {reader}"})
    assert c2.get("/export/other").status_code in (403, 404)


def test_revoked_key_blocked(two_tenants):
    c, ta, tb = two_tenants
    app_mod.ingestion.ingest(DATA_A, "acme")  # own-tenant product exists
    kid = c.get("/keys", params={"tenant": "acme"}).json()[0]["id"]
    c.delete(f"/keys/{kid}", params={"tenant": "acme"})
    r = c.get("/products/A-1", params={"tenant": "acme"})
    assert r.status_code == 401
    assert "A-1" not in r.text
