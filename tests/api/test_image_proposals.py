"""Governance tests: AI never binds images; only the human apply endpoint does."""
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
    app_mod.app.router.routes = [
        r for r in app_mod.app.router.routes
        if getattr(r, "path", None) != "/media"
    ] + [__import__("fastapi.routing", fromlist=["Mount"]).Mount(
        "/media", __import__("starlette.staticfiles", fromlist=["StaticFiles"]).StaticFiles(
            directory=str(tmp_path / "media"), check_dir=False))]
    t = app_mod.persistence.get_or_create_tenant("demo")
    admin = AuthService(app_mod.persistence).create_key(t.id, "a", "admin")["key"]
    reader = AuthService(app_mod.persistence).create_key(t.id, "r", "reader")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {admin}"})
    c3 = TestClient(app_mod.app); c3.headers.update({"Authorization": f"Bearer {reader}"})
    c.post("/tenants/demo/ingest", params={"body": "sku,name,price\n100245,Boremaskine,199\n"})
    return c, c3


def _seed_product_with_proposal(env):
    c, reader = env
    stored = c.post("/products/100245/images", params={"tenant": "demo"},
                    content=PNG, headers={"Content-Type": "image/png"}).json()
    # unbind it — we only want the blob stored, then hand-plant a proposal
    c.delete("/products/100245/images", params={"tenant": "demo", "url": stored["url"]})
    tid = app_mod.persistence.get_or_create_tenant("demo").id
    prod = app_mod.persistence.get_product(tid, "100245")
    data = dict(prod["data"] or {})
    ai = dict(data.get("_ai_meta") or {})
    ai["images_proposed"] = [{"url": stored["url"], "sha256": stored["sha256"],
                              "proposed_by": "ai_resolve_images"}]
    data["_ai_meta"] = ai
    app_mod.persistence.upsert_product(tid, "100245", data)
    return stored, stored["sha256"]


def test_list_proposals_requires_read_only(env):
    client, reader = env
    stored, sha = _seed_product_with_proposal(env)
    r = reader.get("/products/100245/images/proposals", params={"tenant": "demo"})
    assert r.status_code == 200
    props = r.json()["proposals"]
    if isinstance(props, dict):
        assert props.get("url") == stored["url"]
    else:
        assert props and props[0]["url"] == stored["url"]
    # reader cannot apply
    r2 = reader.post("/products/100245/images/apply", params={"tenant": "demo"},
                     json={"url": stored["url"], "sha256": sha})
    assert r2.status_code == 403


def test_apply_full_flow_binds_and_clears_proposal(env):
    c, reader = env
    stored, sha = _seed_product_with_proposal(env)
    r = c.post("/products/100245/images/apply", params={"tenant": "demo"},
               json={"url": stored["url"]})
    assert r.status_code == 200, r.text
    p = c.get("/products/100245", params={"tenant": "demo"}).json()
    assert stored["url"] in (p["data"].get("images") or [])
    ai = p["data"].get("_ai_meta") or {}
    raw = ai.get("images_proposed")
    remaining = [raw] if isinstance(raw, dict) else (raw or [])
    assert stored["url"] not in [x.get("url") for x in remaining if isinstance(x, dict)]


def test_apply_unknown_proposal_rejected(env):
    c, reader = env
    stored, sha = _seed_product_with_proposal(env)
    r = c.post("/products/100245/images/apply", params={"tenant": "demo"},
               json={"url": "/media/demo/nonexistent.png"})
    assert r.status_code == 404


def test_proposals_404_for_unknown_product(env):
    c, reader = env
    r = c.get("/products/NOPE/images/proposals", params={"tenant": "demo"})
    assert r.status_code == 404
