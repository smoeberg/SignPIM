"""API-key auth: hashing, roles/scopes, tenant binding, revocation."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService, hash_key, generate_api_key
from services.ingestion import CSVIngestionService
from engine.kernel import PlatformKernel


@pytest.fixture()
def client():
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    return TestClient(app_mod.app)


@pytest.fixture()
def admin_key(client):
    """Bootstrap: create an admin key directly via AuthService (no auth chicken-egg)."""
    t = app_mod.persistence.get_or_create_tenant("acme")
    svc = AuthService(app_mod.persistence)
    out = svc.create_key(t.id, "bootstrap", role="admin")
    return out["key"]


def _h(key): return {"Authorization": f"Bearer {key}"}


def test_hash_is_sha256_and_irreversible():
    raw, _ = generate_api_key()
    h = hash_key(raw)
    assert len(h) == 64 and h != raw
    assert hash_key(raw) == h  # deterministic


def test_create_key_returns_plaintext_once(client, admin_key):
    r = client.post("/keys", params={"tenant": "acme", "name": "etl", "role": "writer"},
                    headers=_h(admin_key))
    assert r.status_code == 200
    body = r.json()
    assert body["key"].startswith("spim_")
    assert body["warning"].startswith("Store this key now")
    assert "products:write" in body["scopes"]


def test_reader_cannot_manage_keys(client, admin_key):
    r = client.post("/keys", params={"tenant": "acme", "name": "x", "role": "reader"},
                    headers=_h(admin_key))
    reader_key = r.json()["key"]
    r2 = client.post("/keys", params={"tenant": "acme", "name": "y", "role": "reader"},
                     headers=_h(reader_key))
    assert r2.status_code == 403  # reader lacks keys:manage


def test_invalid_key_401(client):
    r = client.get("/products", params={"tenant": "acme"},
                   headers={"Authorization": "Bearer spim_garbage"})
    assert r.status_code == 401


def test_missing_header_401(client):
    r = client.get("/products", params={"tenant": "acme"})
    assert r.status_code == 401


def test_malformed_key_401(client):
    r = client.get("/products", params={"tenant": "acme"},
                   headers={"Authorization": "Bearer not-a-key"})
    assert r.status_code == 401


def test_tenant_isolation_writer(client, admin_key):
    """A writer key from tenant A must not ingest into tenant B."""
    r = client.post("/keys", params={"tenant": "acme", "name": "etl", "role": "writer"},
                    headers=_h(admin_key))
    writer_key = r.json()["key"]
    r2 = client.post("/tenants/other-tenant/ingest", params={"body": "sku,name,price\nA,B,1.0"},
                     headers=_h(writer_key))
    assert r2.status_code == 403


def test_writer_can_ingest_own_tenant(client, admin_key):
    r = client.post("/keys", params={"tenant": "acme", "name": "etl", "role": "writer"},
                    headers=_h(admin_key))
    writer_key = r.json()["key"]
    r2 = client.post("/tenants/acme/ingest",
                     params={"body": "sku,name,price,ean,images\nK-1,Chair,499.0,5901234123457,x.jpg"},
                     headers=_h(writer_key))
    assert r2.status_code == 200
    assert r2.json()["rows_ingested"] == 1


def test_revoke_key_kills_access(client, admin_key):
    r = client.post("/keys", params={"tenant": "acme", "name": "tmp", "role": "reader"},
                    headers=_h(admin_key))
    tmp_key = r.json()["key"]
    tmp_id = r.json()["id"]
    assert client.get("/products", params={"tenant": "acme"}, headers=_h(tmp_key)).status_code == 200
    r2 = client.delete(f"/keys/{tmp_id}", params={"tenant": "acme"}, headers=_h(admin_key))
    assert r2.status_code == 200
    r3 = client.get("/products", params={"tenant": "acme"}, headers=_h(tmp_key))
    assert r3.status_code == 401


def test_list_keys_never_leaks_plaintext_or_hash(client, admin_key):
    r = client.get("/keys", params={"tenant": "acme"}, headers=_h(admin_key))
    assert r.status_code == 200
    body = r.text
    assert "key_hash" not in body and "scopes" not in body
    # full plaintext keys must never appear in listings (only 13-char prefixes)
    import re
    assert not re.search(r'"?spim_[A-Za-z0-9_-]{20,}"?', body)
