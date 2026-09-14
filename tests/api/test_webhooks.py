"""Quality-drop webhooks: subscription, HMAC signing, threshold firing, retry log."""
import json
import hashlib
import hmac as hmac_mod
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from services.ingestion import CSVIngestionService
from services.webhooks import WebhookService, sign_payload
from engine.kernel import PlatformKernel

GOOD = "sku,name,price,ean,images\nG-1,Stol,499.0,5901234123457,x.jpg\n"
BAD = "sku,name,price,ean,images\nB-1,IngenEAN,999.0,,\n"  # missing ean → violations


@pytest.fixture()
def env():
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod._users = None
    app_mod._webhooks = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    t = app_mod.persistence.get_or_create_tenant("acme")
    key = AuthService(app_mod.persistence).create_key(t.id, "test", "admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c, t.id


class FakePoster:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body, signature):
        self.calls.append({"url": url, "body": body, "signature": signature})
        return 200


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    import services.webhooks as wh
    monkeypatch.setattr(wh, "RETRY_DELAYS", [0, 0, 0])


def test_subscribe_and_list(env):
    c, tid = env
    r = c.post("/webhooks", params={"tenant": "acme", "url": "https://example.com/hook",
                                    "min_score": 90})
    assert r.status_code == 200
    body = r.json()
    assert body["secret"].startswith("whsec_")
    r2 = c.get("/webhooks", params={"tenant": "acme"})
    assert len(r2.json()) == 1
    assert "secret" not in r2.json()[0]   # secret never re-shown


def test_invalid_url_400(env):
    c, tid = env
    r = c.post("/webhooks", params={"tenant": "acme", "url": "ftp://bad", "min_score": 90})
    assert r.status_code == 400


def test_no_fire_when_above_threshold(env):
    c, tid = env
    ws = WebhookService(app_mod.persistence)
    ws.retry_delays = [0, 0, 0]
    poster = FakePoster()
    ws._poster = poster
    app_mod._webhooks = ws   # inject into API singleton
    ws.subscribe(tid, "https://example.com/hook", min_score=50)
    # good feed → high score → no webhook
    c.post("/tenants/acme/ingest", params={"body": GOOD})
    assert len(poster.calls) == 0


def test_fires_when_below_threshold(env):
    c, tid = env
    ws = WebhookService(app_mod.persistence)
    ws.retry_delays = [0, 0, 0]
    poster = FakePoster()
    ws._poster = poster
    app_mod._webhooks = ws
    ws.subscribe(tid, "https://example.com/hook", min_score=95)
    # bad feed (missing EAN) → score drops → webhook fires
    r = c.post("/tenants/acme/ingest", params={"body": BAD})
    assert r.json().get("webhooks_fired", 0) >= 1
    assert len(poster.calls) == 1
    payload = json.loads(poster.calls[0]["body"])
    assert payload["event"] == "quality.drop"
    assert payload["tenant"] == "acme"
    assert payload["quality_score"] < 95


def test_hmac_signature_verifiable(env):
    c, tid = env
    ws = WebhookService(app_mod.persistence)
    ws.retry_delays = [0, 0, 0]
    poster = FakePoster()
    ws._poster = poster
    app_mod._webhooks = ws
    sub = ws.subscribe(tid, "https://example.com/hook", min_score=95, secret="topsecret")
    c.post("/tenants/acme/ingest", params={"body": BAD})
    call = poster.calls[0]
    expected = "sha256=" + hmac_mod.new(b"topsecret", call["body"].encode(), hashlib.sha256).hexdigest()
    assert call["signature"] == expected
    # receiver-side verification
    assert sign_payload("topsecret", call["body"]) == call["signature"]


def test_delivery_logged(env):
    c, tid = env
    ws = WebhookService(app_mod.persistence)
    ws.retry_delays = [0, 0, 0]
    ws._poster = lambda url, body, sig: 200
    app_mod._webhooks = ws
    sub = ws.subscribe(tid, "https://example.com/hook", min_score=95)
    c.post("/tenants/acme/ingest", params={"body": BAD})
    r = c.get(f"/webhooks/{sub['id']}/deliveries", params={"tenant": "acme"})
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 1
    assert d[0]["success"] is True
    assert d[0]["status_code"] == 200


def test_failed_delivery_logged_with_attempts(env):
    c, tid = env
    ws = WebhookService(app_mod.persistence)

    def failing(url, body, sig):
        raise ConnectionError("refused")

    ws.retry_delays = [0, 0, 0]
    ws.retry_delays = [0, 0, 0]
    ws._poster = failing
    app_mod._webhooks = ws
    sub = ws.subscribe(tid, "https://example.com/hook", min_score=95)
    c.post("/tenants/acme/ingest", params={"body": BAD})
    r = c.get(f"/webhooks/{sub['id']}/deliveries", params={"tenant": "acme"})
    d = r.json()[0]
    assert d["success"] is False
    assert d["attempts"] == 4  # 1 initial + 3 retries


def test_unsubscribe_stops_firing(env):
    c, tid = env
    ws = WebhookService(app_mod.persistence)
    ws.retry_delays = [0, 0, 0]
    poster = FakePoster()
    ws._poster = poster
    app_mod._webhooks = ws
    sub = ws.subscribe(tid, "https://example.com/hook", min_score=95)
    c.delete(f"/webhooks/{sub['id']}", params={"tenant": "acme"})
    r = c.post("/tenants/acme/ingest", params={"body": BAD})
    assert r.json().get("webhooks_fired", 0) == 0


def test_webhook_failure_never_breaks_ingest(env):
    c, tid = env
    ws = WebhookService(app_mod.persistence)
    ws.retry_delays = [0, 0, 0]
    ws._poster = lambda url, body, sig: (_ for _ in ()).throw(RuntimeError("boom"))
    app_mod._webhooks = ws
    ws.subscribe(tid, "https://example.com/hook", min_score=95)
    r = c.post("/tenants/acme/ingest", params={"body": BAD})
    assert r.status_code == 200   # ingest succeeded despite webhook crash
    assert r.json()["rows_ingested"] == 1
