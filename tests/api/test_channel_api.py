"""HTTP contract for the channel connector (ADR-018)."""
import hashlib
import hmac
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import app as app_mod
from core.auth import AuthService
from core.persistence import PersistenceService


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNPIM_PS_KEY", "ws-s3cr3t")
    monkeypatch.setenv("SIGNPIM_PS_HOOK", "hook")
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/channel.db")
    app_mod.media_root = str(tmp_path / "media")
    app_mod._auth = None
    app_mod._users = None
    tenant = app_mod.persistence.get_or_create_tenant("acme")
    auth = AuthService(app_mod.persistence)
    admin = TestClient(app_mod.app)
    admin.headers.update({"Authorization": f"Bearer {auth.create_key(tenant.id, 'a', role='admin')['key']}"})
    writer = TestClient(app_mod.app)
    writer.headers.update({"Authorization": f"Bearer {auth.create_key(tenant.id, 'w', role='writer')['key']}"})
    anonymous = TestClient(app_mod.app)
    return admin, writer, anonymous


def test_connect_upload_media_and_receive_event(clients):
    admin, writer, anonymous = clients
    q = "?tenant=acme"
    channel = admin.post(f"/catalog/channels{q}", json={"code": "shop-a", "name": "Shop A"}).json()
    connected = admin.put(f"/catalog/channels/{channel['id']}/connection{q}", json={
        "base_url": "https://shop-a.example", "credential_ref": "env:SIGNPIM_PS_KEY",
        "webhook_secret_ref": "env:SIGNPIM_PS_HOOK",
        "connector_config": {"languages": {"da": 1}, "category_map": {"tools": 12}},
    })
    assert connected.status_code == 200, connected.text
    assert "ws-s3cr3t" not in connected.text

    # writers cannot change connections
    assert writer.put(f"/catalog/channels/{channel['id']}/connection{q}", json={
        "base_url": "https://evil.example", "credential_ref": "env:SIGNPIM_PS_KEY"}).status_code == 403

    product = admin.post(f"/catalog/products{q}", json={"master_sku": "M-1", "name": "Drill"}).json()
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (1, 2, 3)).save(buf, format="PNG")
    uploaded = writer.post(f"/catalog/products/{product['id']}/media{q}",
                           files={"file": ("a.png", buf.getvalue(), "image/png")},
                           data={"alt_text": "Front"})
    assert uploaded.status_code == 200, uploaded.text
    media = writer.get(f"/catalog/products/{product['id']}/media{q}").json()
    assert media[0]["alt_text"] == "Front" and media[0]["position"] == 0

    jobs = writer.get(f"/catalog/channels/{channel['id']}/sync-jobs{q}&status=pending").json()
    assert len(jobs) == 1 and jobs[0]["reason"] in ("product", "media")

    body = json.dumps({"event_id": "e1", "type": "product.updated", "data": {"product_id": "999"}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(b"hook", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ok = anonymous.post(f"/channels/{channel['id']}/events", content=body,
                        headers={"X-SignPIM-Timestamp": ts, "X-SignPIM-Signature": f"sha256={sig}"})
    assert ok.status_code == 200 and ok.json()["ignored"] == "product not managed by SignPIM"
    bad = anonymous.post(f"/channels/{channel['id']}/events", content=body,
                         headers={"X-SignPIM-Timestamp": ts, "X-SignPIM-Signature": "sha256=00"})
    assert bad.status_code == 401
