"""Admin config backend tests: DB-stored settings, encrypted secrets, role gating."""
import base64
import os

import pytest


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNPIM_SECRET_KEY", "test-master-key-123")
    yield


@pytest.fixture()
def svc(cfg_env, tmp_path):
    from core.config_service import ConfigService
    from core.persistence import PersistenceService
    p = PersistenceService(dsn=f"sqlite:///{tmp_path}/t.db")
    t = p.get_or_create_tenant("demo")
    return ConfigService(p), t.id


def test_set_and_effective_plain_key(svc):
    c, tid = svc
    c.set("sftp.remote_path", "/out/feed.xml", tenant_id=tid, updated_by="admin@x.dk")
    eff = c.effective(tid)
    assert eff["sftp.remote_path"] == {"source": "db", "value": "/out/feed.xml"}


def test_secret_is_encrypted_at_rest(svc):
    c, tid = svc
    c.set("sftp.password", "hunter2", tenant_id=tid)
    from core.config_service import AppConfig
    with c.persistence.session() as s:
        raw = s.query(AppConfig).filter_by(tenant_id=tid, key="sftp.password").first().value
    assert "hunter2" not in raw
    assert raw.startswith("gAAA")  # fernet token


def test_secret_masked_in_list_and_effective(svc):
    c, tid = svc
    c.set("llm.api_key_anthropic", "sk-ant-secret", tenant_id=tid)
    assert c.list_keys(tid)["llm.api_key_anthropic"] == "••••••••"
    assert c.effective(tid)["llm.api_key_anthropic"]["value"] == "••••••••"
    # resolve returns real value internally
    assert c.resolve("llm.api_key_anthropic", tenant_id=tid) == "sk-ant-secret"


def test_tenant_overrides_global(svc):
    c, tid = svc
    c.set("llm.provider", "anthropic", tenant_id=None)
    c.set("llm.provider", "openai", tenant_id=tid)
    assert c.resolve("llm.provider", tenant_id=tid) == "openai"
    assert c.resolve("llm.provider", tenant_id=None) == "anthropic"
    other = c.persistence.get_or_create_tenant("other").id
    assert c.resolve("llm.provider", tenant_id=other) == "anthropic"


def test_env_fallback_when_db_empty(svc, monkeypatch):
    c, tid = svc
    monkeypatch.setenv("SIGNPIM_SFTP_HOST", "sftp.skovgaard.example")
    eff = c.effective(tid)
    assert eff["sftp.host"] == {"source": "env", "value": "sftp.skovgaard.example"}


def test_unknown_key_rejected(svc):
    c, tid = svc
    with pytest.raises(ValueError, match="Unknown config key"):
        c.set("totally.made_up", "x")


def test_secret_requires_master_key(svc, monkeypatch):
    c, tid = svc
    monkeypatch.delenv("SIGNPIM_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SIGNPIM_SECRET_KEY"):
        c.set("sftp.password", "x", tenant_id=tid)


def test_delete_key(svc):
    c, tid = svc
    c.set("llm.model", "claude-3-5-haiku-20241022", tenant_id=tid)
    assert c.delete("llm.model", tenant_id=tid) is True
    assert c.delete("llm.model", tenant_id=tid) is False
    assert c.resolve("llm.model", tenant_id=tid) is None
