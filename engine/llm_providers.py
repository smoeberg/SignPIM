"""
LLM provider layer for SignPIM AI operators.

Design rules (from docs/AI Operator Plan.md):
- Providers are PLUGINS, resolved by name from a registry — never hardcoded
  into operator logic. New providers register themselves (or via entry points).
- Provider selection is CONFIG (tenant settings `llm.provider`), not code.
- MockLLMProvider keeps tests 100% offline and deterministic.
"""
import hashlib
import re
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger("signpim.llm")


class LLMProvider(ABC):
    """Contract all providers implement. Operators depend on this, never on a concrete class."""

    name: str = "abstract"
    deterministic: bool = False

    @abstractmethod
    def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0,
                 max_tokens: int = 512) -> Dict[str, Any]:
        """Returns {"text": str, "tokens_in": int, "tokens_out": int, "model": str}."""


class MockLLMProvider(LLMProvider):
    """Deterministic offline provider. Behavior keyed off the prompt via stable hashing —
    same input always yields the same output, so tests are reproducible."""

    name = "mock"
    deterministic = True

    def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0,
                 max_tokens: int = 512) -> Dict[str, Any]:
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        m_fill = re.search(r"Fill ONLY these missing product fields: ([^\n.]+)", prompt)
        if m_fill:
            wanted = [f.strip() for f in m_fill.group(1).split(",") if f.strip()]
            fill = {k: f"mock-filled-{digest[:6]}" for k in wanted}
            return {"text": json.dumps(fill), "tokens_in": len(prompt) // 4,
                    "tokens_out": 8, "model": "mock-1"}
        if "Match a product to image URLs" in prompt:
            import json as _j
            m_cand = re.search(r"Candidates: (\[.*?\])", prompt)
            cands = _j.loads(m_cand.group(1)) if m_cand else []
            return {"text": _j.dumps({"url": cands[0]}) if cands else "{}",
                    "tokens_in": len(prompt) // 4, "tokens_out": 8, "model": "mock-1"}
        if "Propose generation parameters" in prompt:
            return {"text": json.dumps({"prompt": "generated product image"}),
                    "tokens_in": len(prompt) // 4, "tokens_out": 8, "model": "mock-1"}
        if "strict JSON" in prompt and "{" in prompt:
            # Deterministic JSON payload shaped for enrich/classify prompts.
            try:
                start = prompt.index("{")
                payload = json.loads(prompt[start:prompt.rindex("}") + 1])
                if isinstance(payload, dict):
                    fill = {k: f"mock-value-{digest[:6]}" for k in payload
                            if isinstance(k, str) and k not in ("_ai_meta",)}
                    return {"text": json.dumps(fill),
                            "tokens_in": len(prompt) // 4, "tokens_out": 8,
                            "model": "mock-1"}
            except Exception:
                pass
        return {"text": f"mock::{digest[:16]}",
                "tokens_in": len(prompt) // 4,
                "tokens_out": 8,
                "model": "mock-1"}


class OllamaProvider(LLMProvider):
    """Local-first provider (default in prod). Requires a running Ollama endpoint."""

    name = "ollama"
    deterministic = False

    def __init__(self, host: Optional[str] = None, model: str = "qwen2.5:7b",
                 timeout_s: int = 30):
        import json as _json  # noqa: F401
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.model = model
        self.timeout_s = timeout_s

    def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0,
                 max_tokens: int = 512) -> Dict[str, Any]:
        import urllib.request
        body = json.dumps({"model": self.model, "prompt": prompt, "system": system,
                           "stream": False, "options": {"temperature": temperature}}).encode()
        req = urllib.request.Request(
            f"{self.host.rstrip('/')}/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        last_err: Exception = ConnectionError("no attempt")
        for attempt in range(3):  # retry 2x
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read())
                return {"text": payload.get("response", ""),
                        "tokens_in": payload.get("prompt_eval_count", 0),
                        "tokens_out": payload.get("eval_count", 0),
                        "model": self.model}
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Ollama unreachable after retries: {last_err}") from last_err


class OpenAIProvider(LLMProvider):
    """Opt-in cloud provider. API key comes from tenant settings/env — never code."""

    name = "openai"
    deterministic = False

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini",
                 timeout_s: int = 30):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.timeout_s = timeout_s

    def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0,
                 max_tokens: int = 512) -> Dict[str, Any]:
        import urllib.request
        body = json.dumps({
            "model": self.model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        last_err: Exception = ConnectionError("no attempt")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read())
                usage = payload.get("usage", {})
                return {"text": payload["choices"][0]["message"]["content"],
                        "tokens_in": usage.get("prompt_tokens", 0),
                        "tokens_out": usage.get("completion_tokens", 0),
                        "model": payload.get("model", self.model)}
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"OpenAI call failed after retries: {last_err}") from last_err


class LLMProviderRegistry:
    """Plugin registry. Third parties can add providers without touching SignPIM core."""

    _providers: Dict[str, type] = {}

    @classmethod
    def register(cls, provider_cls: type) -> type:
        cls._providers[provider_cls.name] = provider_cls
        return provider_cls

    @classmethod
    def available(cls) -> List[str]:
        return sorted(cls._providers)

    @classmethod
    def resolve(cls, name: str, **kwargs) -> LLMProvider:
        if name not in cls._providers:
            raise KeyError(f"Unknown LLM provider '{name}'. Available: {cls.available()}")
        return cls._providers[name](**kwargs)


LLMProviderRegistry.register(MockLLMProvider)
LLMProviderRegistry.register(OllamaProvider)
LLMProviderRegistry.register(OpenAIProvider)


def resolve_provider_from_settings(settings: Dict[str, Any]) -> LLMProvider:
    """Config-driven selection: tenant settings['llm'] = {'provider': 'ollama', ...}."""
    cfg = (settings or {}).get("llm") or {}
    name = cfg.get("provider") or os.environ.get("SIGNPIM_LLM_PROVIDER", "mock")
    kwargs = {k: v for k, v in cfg.items() if k != "provider"}
    return LLMProviderRegistry.resolve(name, **kwargs)
