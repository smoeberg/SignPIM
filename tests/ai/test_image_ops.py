"""AI image resolution governance tests. 100% offline (mock provider + placeholder PNG)."""
import pytest

from engine.operators.llm_ops import ai_resolve_images
from services.images import ImageService


def _ctx(**kw):
    base = {"violations": [], "tenant_id": "t1",
            "tenant_settings": {"llm": {"provider": "mock"}}}
    base.update(kw)
    return base


class _Repo:
    """Persistence stub — session() must never be touched by the AI operator
    (governance: AI stores blobs, never binds product data)."""
    def __init__(self, products):
        self.products = products
    def session(self):
        raise AssertionError("AI operator must not touch persistence/product data")


# ---------- match mode ----------

def test_match_proposes_never_applies():
    ctx = _ctx(operator_config={"mode": "match",
                                "candidates": ["https://cdn.supplier/img/100245.jpg"]})
    data = {"sku": "100245", "name": "Boremaskine"}
    out = ai_resolve_images(dict(data), ctx)
    assert out.get("images") in (None, data.get("images"))  # product data untouched
    assert ctx["_proposed_image_matches"]["100245"] == "mock-value" or \
        "100245" in ctx["_proposed_image_matches"]  # PROPOSED, not applied


def test_match_skips_products_with_images():
    ctx = _ctx(operator_config={"mode": "match", "candidates": []})
    data = {"sku": "A-1", "images": ["/media/t1/exists.png"]}
    ai_resolve_images(dict(data), ctx)
    assert "_proposed_image_matches" not in ctx


def test_match_bad_json_adds_minor_violation():
    from unittest.mock import patch
    ctx = _ctx(operator_config={"mode": "match", "candidates": ["u"]})
    with patch("engine.operators.llm_ops._provider_for") as pf:
        pf.return_value.complete = lambda *a, **kw: {
            "text": "not json", "tokens_in": 1, "tokens_out": 1, "model": "m", "provider": "mock"}
        ai_resolve_images({"sku": "x"}, ctx)
    ai_resolve_images({"sku": "x"}, ctx)
    assert ctx["violations"][0]["rule_id"] == "ai_low_confidence"


# ---------- generate mode ----------

def test_generate_stores_via_imageservice_and_marks_ai_meta(tmp_path):
    svc = ImageService(persistence=_Repo({}), media_root=str(tmp_path))
    ctx = _ctx(operator_config={"mode": "generate"}, image_service=svc)
    data = {"sku": "100245", "name": "Boremaskine", "ean": "5901234123452"}
    out = ai_resolve_images(dict(data), ctx)
    meta = out["_ai_meta"]["images_proposed"]
    assert meta["url"].startswith("/media/t1/") and meta["sha256"]
    # blob stored on disk, PROPOSED in _ai_meta — NOT bound to product.images
    assert svc.read_file(meta["url"])
    assert "images" not in out  # human review applies it, never the AI


def test_generate_without_service_is_noop():
    ctx = _ctx(operator_config={"mode": "generate"})
    out = ai_resolve_images({"sku": "x"}, ctx)
    assert "_ai_meta" not in out


def test_generate_never_touches_protected_fields(tmp_path):
    svc = ImageService(persistence=_Repo({}), media_root=str(tmp_path))
    ctx = _ctx(operator_config={"mode": "generate"}, image_service=svc)
    data = {"sku": "100245", "ean": "5901234123452", "price": 499.95}
    out = ai_resolve_images(dict(data), ctx)
    assert out["sku"] == "100245" and out["ean"] == "5901234123452" and out["price"] == 499.95
