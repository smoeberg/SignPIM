"""Admin config API tests."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNPIM_SECRET_KEY", "test-master-key-123")
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/api.db")
    app_mod._auth = None
    c = TestClient(app_mod.app)
    t = app_mod.persistence.get_or_create_tenant("demo")
    svc = AuthService(app_mod.persistence)
    admin = svc.create_key(t.id, "cfg-admin", role="admin")
    reader = svc.create_key(t.id, "cfg-reader", role="reader")
    return {"client": c,
            "admin": {"Authorization": f"Bearer {admin['key']}"},
            "reader": {"Authorization": f"Bearer {reader['key']}"}}


def test_reader_cannot_access_admin_config(env):
    r = env["client"].get("/admin/config", params={"tenant": "demo"}, headers=env["reader"])
    assert r.status_code == 403


def test_admin_set_list_masked(env):
    c = env["client"]
    r = c.put("/admin/config/sftp.host", params={"tenant": "demo"},
              headers=env["admin"], json={"value": "sftp.skovgaard.example"})
    assert r.status_code == 200
    c.put("/admin/config/sftp.password", params={"tenant": "demo"},
          headers=env["admin"], json={"value": "s3cr3t-pass"})
    r = c.get("/admin/config", params={"tenant": "demo"}, headers=env["admin"])
    data = r.json()
    assert data["sftp.host"] == "sftp.skovgaard.example"
    assert data["sftp.password"] == "••••••••"


def test_unknown_key_400(env):
    r = env["client"].put("/admin/config/nope.nope", params={"tenant": "demo"},
                          headers=env["admin"], json={"value": "x"})
    assert r.status_code == 400
    assert "Unknown config key" in r.json()["detail"]


def test_effective_shows_source(env):
    c = env["client"]
    c.put("/admin/config/llm.provider", params={"tenant": "demo"},
          headers=env["admin"], json={"value": "anthropic"})
    r = c.get("/admin/config/effective", params={"tenant": "demo"}, headers=env["admin"])
    assert r.json()["llm.provider"] == {"source": "db", "value": "anthropic"}


def test_delete_missing_404(env):
    r = env["client"].delete("/admin/config/llm.model", params={"tenant": "demo"},
                             headers=env["admin"])
    assert r.status_code == 404


def test_set_without_secret_key_503(env, monkeypatch):
    monkeypatch.delenv("SIGNPIM_SECRET_KEY", raising=False)
    r = env["client"].put("/admin/config/sftp.password", params={"tenant": "demo"},
                          headers=env["admin"], json={"value": "x"})
    assert r.status_code == 503
