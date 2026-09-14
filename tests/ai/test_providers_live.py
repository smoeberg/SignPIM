"""Provider tests. Live smoke runs ONLY when a key/host is configured."""
import json
import os

import pytest

from engine.llm_providers import (AnthropicProvider, LLMProviderRegistry,
                                  MockLLMProvider, resolve_provider_from_settings)


def test_registry_has_all_providers():
    assert set(LLMProviderRegistry.available()) >= {"mock", "ollama", "openai", "anthropic"}


def test_resolve_from_settings_provider_name():
    p = resolve_provider_from_settings({"llm": {"provider": "anthropic",
                                                "model": "claude-3-5-haiku-20241022"}})
    assert isinstance(p, AnthropicProvider)
    assert p.model == "claude-3-5-haiku-20241022"


def test_resolve_default_is_mock(monkeypatch):
    monkeypatch.delenv("SIGNPIM_LLM_PROVIDER", raising=False)
    assert isinstance(resolve_provider_from_settings({}), MockLLMProvider)


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        LLMProviderRegistry.resolve("does-not-exist")


def test_anthropic_no_key_fails_closed():
    p = AnthropicProvider(api_key="")
    with pytest.raises(RuntimeError, match="no API key"):
        p.complete("test")


def test_anthropic_retries_on_429_then_raises():
    import urllib.request
    calls = []

    class FakeErr(Exception):
        code = 429

    def fake_urlopen(req, timeout):
        calls.append(1)
        raise FakeErr()

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        p = AnthropicProvider(api_key="sk-test", max_retries=2)
        with pytest.raises(RuntimeError, match="after retries"):
            p.complete("test")
        assert len(calls) == 3
    finally:
        urllib.request.urlopen = orig


requires_openai = pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"),
                                     reason="OPENAI_API_KEY not set — live smoke skipped")
requires_anthropic = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                                        reason="ANTHROPIC_API_KEY not set — live smoke skipped")
requires_ollama = pytest.mark.skipif(not os.environ.get("SIGNPIM_LLM_LIVE_OLLAMA"),
                                     reason="Ollama live smoke opt-in")


@requires_anthropic
def test_live_anthropic_smoke():
    p = resolve_provider_from_settings({"llm": {"provider": "anthropic"}})
    r = p.complete('Return strict JSON {"ok": true} and nothing else.', max_tokens=32)
    assert r["tokens_out"] > 0
    assert json.loads(r["text"]) == {"ok": True}


@requires_openai
def test_live_openai_smoke():
    p = resolve_provider_from_settings({"llm": {"provider": "openai"}})
    r = p.complete('Return strict JSON {"ok": true} and nothing else.', max_tokens=32)
    assert r["tokens_out"] > 0
    assert json.loads(r["text"].strip().removeprefix("```json").removesuffix("```")) == {"ok": True}


@requires_ollama
def test_live_ollama_smoke():
    p = resolve_provider_from_settings({"llm": {"provider": "ollama"}})
    r = p.complete('Return strict JSON {"ok": true} and nothing else.')
    assert json.loads(r["text"].strip().strip("`").replace("json", "", 1).strip()) == {"ok": True}
