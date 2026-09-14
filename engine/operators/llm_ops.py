"""
AI operators for SignPIM.

Governance (docs/AI Operator Plan.md):
- AI operators live in the SAME RichOperatorRegistry as data rules.
- They run as workflow steps: validate → ai_op → validate → calculate → persist.
- AI output NEVER touches protected fields (sku, ean, price) — configurable
  whitelist via meta/ai/fields.yaml, not hardcoded.
- Everything is logged to llm_calls (prompt-hash only, PII-safe).
"""
import hashlib
import json
import re
import time
from typing import Any, Dict, List

from engine.llm_providers import LLMProvider, resolve_provider_from_settings
from engine.operators.registry import RichOperatorRegistry

PROTECTED_FIELDS_DEFAULT = ["sku", "ean", "price"]

# ---------- llm_calls cost logger ----------

class LLMCallLogger:
    """Persists AI-call costs. Uses persistence service lazily to avoid import cycles."""

    _persistence = None

    @classmethod
    def bind(cls, persistence):
        cls._persistence = persistence

    @classmethod
    def log(cls, tenant_id: str, operator: str, provider: str, model: str,
            prompt: str, response: str, tokens_in: int, tokens_out: int,
            latency_ms: int, success: bool):
        if cls._persistence is None:
            return
        cls._persistence.log_llm_call(
            tenant_id=tenant_id, operator=operator, provider=provider, model=model,
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            response_hash=hashlib.sha256(response.encode()).hexdigest(),
            tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=latency_ms, success=success,
        )


def _complete(provider: LLMProvider, prompt: str, tenant_id: str, operator: str) -> Dict[str, Any]:
    t0 = time.perf_counter()
    ok = True
    try:
        return provider.complete(prompt)
    except Exception:
        ok = False
        raise
    finally:
        dt = int((time.perf_counter() - t0) * 1000)
        try:
            LLMCallLogger.log(tenant_id, operator, provider.name,
                              "n/a" if not ok else "n/a",
                              prompt, "", 0, 0, dt, ok)
        except Exception:  # never fail the workflow because of logging
            pass


def _provider_for(tenant_settings: Dict[str, Any]) -> LLMProvider:
    return resolve_provider_from_settings(tenant_settings or {})


# ---------- 1. llm_classify ----------

def llm_classify(data: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Classifies a free-text supplier field into normalized values.
    config: {'field': 'supplier_description', 'categories': [...], 'target': 'category'}"""
    cfg = ctx.get('operator_config', {})
    field = cfg["field"]
    target = cfg.get("target", "category")
    provider = _provider_for(ctx.get("tenant_settings", {}))
    prompt = (f"Classify this product description into exactly one of: "
              f"{', '.join(cfg.get('categories', []))}. Reply with the category only.\n\n{data.get(field, '')}")
    r = _complete(provider, prompt, ctx.get("tenant_id", ""), "llm_classify")
    value = r["text"].strip()
    if value not in cfg.get("categories", []):
        ctx["violations"].append({
            "rule_id": "ai_low_confidence",
            "severity": "minor",
            "field": target,
            "message": f"AI classification '{value}' not in allowed categories",
        })
        return data
    data[target] = value
    data.setdefault("_ai_meta", {})["category"] = {"model": r["model"], "confidence": 1.0 if provider.deterministic else 0.9}
    return data


RichOperatorRegistry.register("llm_classify", llm_classify,
                              category="ai", is_deterministic=False,
                              side_effects=True, security_level="elevated")


# ---------- 2. llm_enrich ----------

def llm_enrich(data: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Fills MISSING fields only. Protected fields are never written (field whitelist
    from meta/ai/fields.yaml or operator config, not hardcoded)."""
    cfg = ctx.get('operator_config', {})
    provider = _provider_for(ctx.get("tenant_settings", {}))
    allowed = cfg.get("allowed_fields") or _load_field_whitelist() or []
    protected = set(cfg.get("protected_fields", PROTECTED_FIELDS_DEFAULT))
    missing = [f for f in allowed if not data.get(f) and f not in protected]
    if not missing:
        return data
    prompt = (f"Fill ONLY these missing product fields: {', '.join(missing)}. "
              f"Return strict JSON.\n\nProduct: {json.dumps({k: v for k, v in data.items() if k != '_ai_meta'})}")
    r = _complete(provider, prompt, ctx.get("tenant_id", ""), "llm_enrich")
    try:
        parsed = json.loads(_extract_json(r["text"]))
    except Exception:
        ctx["violations"].append({
            "rule_id": "ai_low_confidence", "severity": "minor",
            "message": "AI enrichment returned unparseable JSON — ignored",
        })
        return data
    meta = data.setdefault("_ai_meta", {})
    for k, v in parsed.items():
        if k in protected:
            continue  # hard governance: protected fields are NEVER written by AI
        if k in allowed and v:
            data[k] = v
            meta[k] = {"model": r["model"], "confidence": 1.0 if provider.deterministic else 0.9}
    return data


def _extract_json(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


_WHITELIST_CACHE: List[str] = []

def _load_field_whitelist() -> List[str]:
    """Reads meta/ai/fields.yaml if present — config, not code."""
    if _WHITELIST_CACHE:
        return _WHITELIST_CACHE
    try:
        import yaml
        with open("meta/ai/fields.yaml") as f:
            cfg = yaml.safe_load(f) or {}
        fields = cfg.get("enrichable_fields") or []
        _WHITELIST_CACHE.extend(fields)
    except Exception:
        pass
    return _WHITELIST_CACHE


RichOperatorRegistry.register("llm_enrich", llm_enrich,
                              category="ai", is_deterministic=False,
                              side_effects=True, security_level="elevated")


# ---------- 3. llm_match_supplier ----------

def llm_match_supplier(data: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Maps raw supplier CSV headers to internal fields. Output is a PROPOSED mapping
    (never auto-applied): written to ctx['_proposed_mappings'] for human review."""
    cfg = ctx.get('operator_config', {})
    provider = _provider_for(ctx.get("tenant_settings", {}))
    headers = cfg.get("headers", [])
    internal = cfg.get("internal_fields", [])
    prompt = (f"Map these supplier CSV headers to internal PIM fields. "
              f"Internal fields: {', '.join(internal)}. Return strict JSON {{\"raw\": \"internal\"}}.\n\n{json.dumps(headers)}")
    r = _complete(provider, prompt, ctx.get("tenant_id", ""), "llm_match_supplier")
    try:
        mapping = json.loads(_extract_json(r["text"]))
    except Exception:
        return data
    proposed = ctx.setdefault("_proposed_mappings", {})
    for raw, internal_name in mapping.items():
        if internal_name in internal:
            proposed[raw] = internal_name
    return data


RichOperatorRegistry.register("llm_match_supplier", llm_match_supplier,
                              category="ai", is_deterministic=False,
                              side_effects=True, security_level="elevated")


# ---------- 4. llm_translate ----------

def llm_translate(data: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Translates a field into configured languages, writing e.g. name_da, name_en.
    Never touches the source field. Never touches protected fields."""
    cfg = ctx.get('operator_config', {})
    provider = _provider_for(ctx.get("tenant_settings", {}))
    field = cfg["field"]
    langs = cfg.get("languages", ["da", "en"])
    for lang in langs:
        key = f"{field}_{lang}"
        if data.get(key):
            continue
        prompt = (f"Translate to {'Danish' if lang == 'da' else 'English'}: "
                  f"{data.get(field, '')}")
        r = _complete(provider, prompt, ctx.get("tenant_id", ""), "llm_translate")
        data[key] = r["text"].strip()
        data.setdefault("_ai_meta", {})[key] = {"model": r["model"], "confidence": 1.0 if provider.deterministic else 0.9}
    return data


RichOperatorRegistry.register("llm_translate", llm_translate,
                              category="ai", is_deterministic=False,
                              side_effects=True, security_level="elevated")
