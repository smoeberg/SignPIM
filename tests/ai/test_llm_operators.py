"""AI operator tests. 100% offline via MockLLMProvider. No network in CI."""
import pytest

from engine.llm_providers import (LLMProvider, LLMProviderRegistry,
                                  MockLLMProvider, resolve_provider_from_settings)
from engine.operators.llm_ops import llm_classify, llm_enrich, llm_translate
from engine.operators.registry import RichOperatorRegistry


# ---------- provider registry ----------

def test_provider_registry_resolves_by_name():
    p = LLMProviderRegistry.resolve("mock")
    assert isinstance(p, MockLLMProvider)

def test_provider_registry_unknown_raises():
    with pytest.raises(KeyError):
        LLMProviderRegistry.resolve("nonexistent-llm")

def test_provider_config_driven_selection():
    p = resolve_provider_from_settings({"llm": {"provider": "mock"}})
    assert p.name == "mock"

def test_provider_default_is_mock():
    p = resolve_provider_from_settings({})
    assert p.name == "mock"

def test_mock_is_deterministic():
    r1 = MockLLMProvider().complete("same prompt")
    r2 = MockLLMProvider().complete("same prompt")
    assert r1["text"] == r2["text"]


# ---------- llm_enrich governance ----------

def _ctx(**kw):
    base = {"violations": [], "tenant_id": "t1", "tenant_settings": {"llm": {"provider": "mock"}}}
    base.update(kw)
    return base

def test_enrich_fills_missing_allowed_field():
    data = {"sku": "A-1", "name": "Widget"}
    out = llm_enrich(dict(data), _ctx(operator_config={"allowed_fields": ["description"]}))
    assert out.get("description")
    assert "_ai_meta" in out and "description" in out["_ai_meta"]

def test_enrich_never_touches_protected_fields():
    data = {"sku": "", "ean": "", "price": None, "name": "Widget"}
    out = llm_enrich(dict(data), _ctx(operator_config={"allowed_fields": ["sku", "ean", "price", "description"]}))
    assert out["sku"] == "" and out["ean"] == "" and out["price"] is None  # untouched

def test_enrich_only_fills_missing():
    data = {"sku": "A-1", "description": "existing"}
    out = llm_enrich(dict(data), _ctx(operator_config={"allowed_fields": ["description"]}))
    assert out["description"] == "existing"  # not overwritten

def test_enrich_whitelist_from_config():
    # mock fills anything, but the operator must only write allowed fields
    out = llm_enrich({"sku": "x"}, _ctx(operator_config={"allowed_fields": ["description"]}))
    assert "mystery" not in out  # mock "offered" it, whitelist refused it
    # and an allowed one got through
    assert "description" in out

def test_registry_has_ai_operators():
    assert RichOperatorRegistry.get("llm_enrich") is not None
    assert RichOperatorRegistry.get("llm_classify") is not None
    assert RichOperatorRegistry.get("llm_translate") is not None


# ---------- llm_classify ----------

def test_classify_accepts_valid_category():
    class Yes(MockLLMProvider):
        name = "yes"
        def complete(self, prompt, **kw):
            return {"text": "Electronics", "tokens_in": 1, "tokens_out": 1, "model": "m"}
    LLMProviderRegistry.register(Yes)
    ctx = _ctx(tenant_settings={"llm": {"provider": "yes"}},
               operator_config={"field": "raw_desc", "categories": ["Electronics", "Toys"], "target": "category"})
    out = llm_classify({"raw_desc": "cable"}, ctx)
    assert out["category"] == "Electronics"

def test_classify_rejects_invalid_category():
    class No(MockLLMProvider):
        name = "no"
        def complete(self, prompt, **kw):
            return {"text": "random-gibberish", "tokens_in": 1, "tokens_out": 1, "model": "m"}
    LLMProviderRegistry.register(No)
    ctx = _ctx(tenant_settings={"llm": {"provider": "no"}},
               operator_config={"field": "raw_desc", "categories": ["A", "B"], "target": "category"})
    out = llm_classify({"raw_desc": "cable"}, ctx)
    assert "category" not in out
    assert ctx["violations"][0]["rule_id"] == "ai_low_confidence"


# ---------- llm_translate ----------

def test_translate_writes_suffixed_fields():
    class Da(MockLLMProvider):
        name = "da"
        def complete(self, prompt, **kw):
            return {"text": "oversat", "tokens_in": 1, "tokens_out": 1, "model": "m"}
    LLMProviderRegistry.register(Da)
    ctx = _ctx(tenant_settings={"llm": {"provider": "da"}}, operator_config={"field": "name", "languages": ["da"]})
    out = llm_translate({"name": "Widget"}, ctx)
    assert out["name_da"] == "oversat"
    assert out["name"] == "Widget"  # source untouched


# ---------- THE INVARIANT: AI cannot cheat its way to a higher score ----------

def test_ai_output_must_pass_same_rules():
    """AI output passes through the SAME validation rules. A bad AI write is punished —
    the invariant from the AI Operator Plan."""
    from engine.kernel import PlatformKernel

    kernel = PlatformKernel(meta_dir="meta")
    kernel.bootstrap()

    # Simulate an AI that wrote an invalid price into the data (in prod this is
    # blocked by the protected-fields whitelist; here we verify the invariant:
    # even if it happened, the score would NOT benefit).
    data = {"sku": "A-1", "price": "-5.00", "name": "Widget"}
    result = kernel.run_workflow("full_sync", data, "t1")
    violations = [v["rule_id"] if isinstance(v, dict) else v for v in result["violations"]]
    assert "invalid_price" in violations
    assert result["data"]["quality_score"] < 100.0
