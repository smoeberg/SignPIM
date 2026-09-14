"""Multi-user: accounts, password hashing, sessions, role management."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService, UserService, hash_password, verify_password
from services.ingestion import CSVIngestionService
from engine.kernel import PlatformKernel


@pytest.fixture()
def env():
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod._users = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    t = app_mod.persistence.get_or_create_tenant("acme")
    admin_key = AuthService(app_mod.persistence).create_key(t.id, "test", "admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {admin_key}"})
    return c, t.id


def test_password_hashing_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h.startswith("pbkdf2$")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)
    # same password, different salts → different hashes
    assert hash_password("x") != hash_password("x")


def test_create_user(env):
    c, tid = env
    r = c.post("/users", params={"tenant": "acme", "email": "jane@acme.dk",
                                 "password": "S3cretPw!", "role": "writer"})
    assert r.status_code == 200
    assert r.json()["email"] == "jane@acme.dk"
    assert r.json()["role"] == "writer"
    assert "password" not in r.json() and "password_hash" not in r.json()


def test_duplicate_email_400(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "dup@acme.dk", "password": "12345678"})
    r = c.post("/users", params={"tenant": "acme", "email": "dup@acme.dk", "password": "12345678"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_short_password_400(env):
    c, tid = env
    r = c.post("/users", params={"tenant": "acme", "email": "s@acme.dk", "password": "short"})
    assert r.status_code == 400


def test_login_success_and_session(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "bo@acme.dk",
                             "password": "LongEnough1", "role": "writer"})
    r = c.post("/auth/login", params={"tenant": "acme", "email": "bo@acme.dk",
                                      "password": "LongEnough1"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("spimsess_")
    assert body["user"]["role"] == "writer"
    assert "ingest:write" in body["scopes"]


def test_login_wrong_password_401(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "w@acme.dk", "password": "CorrectHorse"})
    r = c.post("/auth/login", params={"tenant": "acme", "email": "w@acme.dk",
                                      "password": "WrongHorse"})
    assert r.status_code == 401


def test_unknown_tenant_401(env):
    c, tid = env
    r = c.post("/auth/login", params={"tenant": "nope", "email": "x@y.dk", "password": "whatever12"})
    assert r.status_code == 401


def test_session_can_ingest(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "w2@acme.dk",
                             "password": "LongEnough1", "role": "writer"})
    tok = c.post("/auth/login", params={"tenant": "acme", "email": "w2@acme.dk",
                                        "password": "LongEnough1"}).json()["token"]
    c2 = TestClient(app_mod.app)  # fresh client, session auth only
    r = c2.post("/tenants/acme/ingest",
                params={"body": "sku,name,price,ean,images\nS-1,Bord,199,5901234123457,i.jpg"},
                headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["rows_ingested"] == 1


def test_me_endpoint(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "m@acme.dk",
                             "password": "LongEnough1", "role": "reader"})
    tok = c.post("/auth/login", params={"tenant": "acme", "email": "m@acme.dk",
                                        "password": "LongEnough1"}).json()["token"]
    r = TestClient(app_mod.app).get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["email"] == "m@acme.dk"
    assert r.json()["role"] == "reader"


def test_logout_kills_session(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "l@acme.dk", "password": "LongEnough1"})
    tok = c.post("/auth/login", params={"tenant": "acme", "email": "l@acme.dk",
                                        "password": "LongEnough1"}).json()["token"]
    c2 = TestClient(app_mod.app)
    assert c2.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    c2.post("/auth/logout", headers={"Authorization": f"Bearer {tok}"})
    assert c2.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_deactivated_user_cannot_login(env):
    c, tid = env
    r = c.post("/users", params={"tenant": "acme", "email": "d@acme.dk",
                                 "password": "LongEnough1", "role": "reader"})
    uid = r.json()["id"]
    c.patch(f"/users/{uid}", params={"tenant": "acme", "active": "false"})
    r2 = c.post("/auth/login", params={"tenant": "acme", "email": "d@acme.dk",
                                       "password": "LongEnough1"})
    assert r2.status_code == 401


def test_role_change_applies(env):
    c, tid = env
    r = c.post("/users", params={"tenant": "acme", "email": "rc@acme.dk",
                                 "password": "LongEnough1", "role": "reader"})
    uid = r.json()["id"]
    c.patch(f"/users/{uid}", params={"tenant": "acme", "role": "admin"})
    tok = c.post("/auth/login", params={"tenant": "acme", "email": "rc@acme.dk",
                                        "password": "LongEnough1"}).json()["token"]
    assert "keys:manage" in TestClient(app_mod.app).get(
        "/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["scopes"]


def test_users_endpoint_lists_no_hashes(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "h@acme.dk", "password": "LongEnough1"})
    r = c.get("/users", params={"tenant": "acme"})
    assert r.status_code == 200
    assert "pbkdf2" not in r.text and "password" not in r.text


def test_reader_cannot_create_users(env):
    c, tid = env
    c.post("/users", params={"tenant": "acme", "email": "rr@acme.dk",
                             "password": "LongEnough1", "role": "reader"})
    reader_key = c.post("/keys", params={"tenant": "acme", "name": "r", "role": "reader"}).json()["key"]
    c2 = TestClient(app_mod.app)
    c2.headers.update({"Authorization": f"Bearer {reader_key}"})
    r = c2.post("/users", params={"tenant": "acme", "email": "nn@acme.dk", "password": "LongEnough1"})
    assert r.status_code == 403
