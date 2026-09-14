"""FastAPI + persistence + ingestion integration tests (SQLite in-memory)."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from services.ingestion import CSVIngestionService


@pytest.fixture()
def client():
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    c = TestClient(app_mod.app)
    # bootstrap an admin key for this test tenant
    from core.auth import AuthService
    t = app_mod.persistence.get_or_create_tenant("acme")
    out = AuthService(app_mod.persistence).create_key(t.id, "test-admin", role="admin")
    c.headers.update({"Authorization": f"Bearer {out['key']}"})
    return c

from engine.kernel import PlatformKernel

CSV_FEED = """sku,name,price,ean,images
GOOD-1,Nice Sofa,1999.0,5901234123457,https://img.example/sofa.jpg
BAD-EAN,Blue Chair,499.0,123,https://img.example/chair.jpg
NO-IMG,Old Desk,799.0,,
"""
CSV_MISSING_COLS = "sku,name\nA,B\n"
CSV_BAD_PRICE = "sku,name,price\nA,B,not-a-number\n"


def test_ingest_full_pipeline(client):
    r = client.post("/tenants/acme/ingest", params={"body": CSV_FEED})
    assert r.status_code == 200
    data = r.json()
    assert data["rows_ingested"] == 3
    assert data["errors"] == []
    assert data["summary"]["quality_score"] is not None
    dims = data["summary"]["by_dimension"]
    assert set(dims) == {"completeness", "consistency", "accuracy"}


def test_ingest_missing_columns_422(client):
    r = client.post("/tenants/acme/ingest", params={"body": CSV_MISSING_COLS})
    assert r.status_code == 422
    assert "Missing required columns" in r.json()["detail"]


def test_ingest_bad_numeric_422(client):
    r = client.post("/tenants/acme/ingest", params={"body": CSV_BAD_PRICE, "tenant": "acme"})
    assert r.status_code == 422
    assert "not numeric" in r.json()["detail"]


def test_upsert_and_get_product(client):
    payload = {"sku": "CHAIR-1", "name": "Chair", "price": 499.0,
               "ean": "5901234123457", "images": ["https://x/y.jpg"]}
    r = client.post("/products", params={"tenant": "acme"}, json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["saved"]["inserted"] is True
    assert body["saved"]["quality_score"] is not None

    r2 = client.post("/products", params={"tenant": "acme"}, json=payload)
    assert r2.json()["saved"]["inserted"] is False
    assert r2.json()["saved"]["version"] == 2

    r3 = client.get("/products/CHAIR-1", params={"tenant": "acme"})
    assert r3.status_code == 200
    assert r3.json()["data"]["sku"] == "CHAIR-1"


def test_get_product_404(client):
    r = client.get("/products/DOES-NOT-EXIST", params={"tenant": "acme"})
    assert r.status_code == 404


def test_quality_endpoint_3d(client):
    client.post("/tenants/acme/ingest", params={"body": CSV_FEED})
    r = client.get("/quality/acme")
    assert r.status_code == 200
    dims = r.json()["by_dimension"]
    assert set(dims) == {"completeness", "consistency", "accuracy"}
    assert 0.0 <= r.json()["quality_score"] <= 100.0


def test_quality_endpoint_404_empty_tenant(client):
    r = client.get("/quality/nobody")
    assert r.status_code == 404


def test_rules_and_mappings(client):
    r = client.post("/rules", params={"tenant": "acme"},
                    json={"rule_id": "short_description", "category": "completeness",
                          "severity": "minor", "parameters": {"min_length": 20}})
    assert r.status_code == 200

    r2 = client.post("/mappings", params={"tenant": "acme"},
                     json={"source_value": "BLUE", "normalized": "Blå", "field": "color"})
    assert r2.status_code == 200


def test_persistence_quality_persisted_after_ingest(client):
    client.post("/tenants/acme/ingest", params={"body": CSV_FEED})
    r = client.get("/products/GOOD-1", params={"tenant": "acme"})
    assert r.status_code == 200
    assert r.json()["quality_score"] is not None
