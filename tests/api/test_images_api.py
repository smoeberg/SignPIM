"""API-level image tests: auth, scope, tenant isolation, serving."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from services.ingestion import CSVIngestionService
from services.images import ImageService
from engine.kernel import PlatformKernel

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNPIM_MEDIA_ROOT", str(tmp_path / "media"))
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod._users = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)

    def _rescore(tenant_id, sku):
        from sqlalchemy import select
        from core.models import Product
        with app_mod.persistence.session() as s:
            row = s.scalars(select(Product).where(
                Product.tenant_id == tenant_id, Product.sku == sku)).first()
            if row is None:
                return
            d = dict(row.data or {})
            d["sku"] = sku
        r = app_mod.kernel.run_workflow("full_sync", d, tenant_id)
        app_mod.persistence.save_quality_score(tenant_id, sku, r["data"].get("quality_score"))

    app_mod.image_service = ImageService(
        app_mod.persistence, media_root=str(tmp_path / "media"), rescorer=_rescore)
    # re-mount /media to the tmp root (app mounts at import time)
    app_mod.app.router.routes = [
        r for r in app_mod.app.router.routes
        if getattr(r, "path", None) != "/media"
    ] + [__import__("fastapi.routing", fromlist=["Mount"]).Mount(
        "/media", __import__("starlette.staticfiles", fromlist=["StaticFiles"]).StaticFiles(
            directory=str(tmp_path / "media"), check_dir=False))]
    t = app_mod.persistence.get_or_create_tenant("demo")
    admin = AuthService(app_mod.persistence).create_key(t.id, "a", "admin")["key"]
    reader = AuthService(app_mod.persistence).create_key(t.id, "r", "reader")["key"]
    other = app_mod.persistence.get_or_create_tenant("other")
    okey = AuthService(app_mod.persistence).create_key(other.id, "o", "admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {admin}"})
    c.post("/tenants/demo/ingest", params={"body": "sku,name,price\nA1,Hammer,99\n"})
    c3 = TestClient(app_mod.app); c3.headers.update({"Authorization": f"Bearer {reader}"})
    c4 = TestClient(app_mod.app); c4.headers.update({"Authorization": f"Bearer {okey}"})
    return c, c3, c4


def test_upload_serve_rescore(env):
    c, reader, other = env
    r = c.post("/products/A1/images", params={"tenant": "demo"},
               content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert c.get(url).status_code == 200
    body = c.get(url).content
    assert body == PNG
    p = c.get("/products/A1", params={"tenant": "demo"}).json()
    assert p["data"]["images"] == [url]


def test_reader_cannot_upload(env):
    c, reader, other = env
    r = reader.post("/products/A1/images", params={"tenant": "demo"},
                    content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 403


def test_cross_tenant_blocked(env):
    c, reader, other = env
    r = other.post("/products/A1/images", params={"tenant": "demo"},
                   content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code in (403, 404)


def test_unbind(env):
    c, reader, other = env
    url = c.post("/products/A1/images", params={"tenant": "demo"},
                 content=PNG, headers={"Content-Type": "image/png"}).json()["url"]
    assert c.delete("/products/A1/images", params={"tenant": "demo", "url": url}).status_code == 200
    assert c.get("/products/A1/images", params={"tenant": "demo"}).json()["images"] == []
