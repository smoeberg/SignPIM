"""DB-backed config drives SFTP polling and LLM provider selection end-to-end."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from core.config_service import ConfigService
from services.feed_import import SFTPFeedImporter
from engine.kernel import PlatformKernel

CSV = "sku,name,price\nP-9,Vaterpas,129.50\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNPIM_SECRET_KEY", "test-master-key-123")
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/api.db")
    app_mod._auth = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = None  # not used by these tests
    t = app_mod.persistence.get_or_create_tenant("acme")
    svc = AuthService(app_mod.persistence)
    key = svc.create_key(t.id, "wiring-admin", role="admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {key}"})
    for e in ("SIGNPIM_SFTP_HOST", "SIGNPIM_SFTP_PASSWORD", "SIGNPIM_SFTP_KEY_PATH",
              "SIGNPIM_LLM_PROVIDER", "SIGNPIM_LLM_MODEL"):
        monkeypatch.delenv(e, raising=False)
    return c


def test_sftp_credentials_from_db(env, monkeypatch):
    def _put(k, v):
        r = env.put(f"/admin/config/{k}", params={"tenant": "acme"},
                    json={"value": v})
        assert r.status_code == 200, r.text

    _put("sftp.host", "ftp.db.dk")
    _put("sftp.user", "dbuser")
    _put("sftp.password", "dbpass")

    class _FakeClient:
        def open_sftp(self):
            return self

        def listdir_attr(self, p): return []

        def close(self):
            pass

    seen = {}

    def _fake_connect(self, host, port, username, password=None, key_path=None):
        seen.update(host=host, username=username, password=password)
        return _FakeClient()

    monkeypatch.setattr(SFTPFeedImporter, "_connect", _fake_connect)
    r = env.post("/feeds/acme/poll", params={"mode": "sftp"})
    assert r.status_code == 200, r.text
    assert seen["host"] == "ftp.db.dk"
    assert seen["username"] == "dbuser"
    assert seen["password"] == "dbpass"


def test_llm_provider_from_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNPIM_SECRET_KEY", "test-master-key-123")
    from services.ingestion import CSVIngestionService
    p = PersistenceService(dsn=f"sqlite:///{tmp_path}/llm.db")
    kernel = PlatformKernel(meta_dir="meta")
    kernel.bootstrap()
    ingestion = CSVIngestionService(p, kernel)
    cs = ConfigService(p)
    t = p.get_or_create_tenant("acme")
    cs.set("llm.provider", "anthropic", tenant_id=t.id)
    cs.set("llm.api_key_anthropic", "sk-ant-db-key", tenant_id=t.id)
    called = {}

    class _FakeAnthropic:
        name = "anthropic"
        deterministic = False

        def __init__(self, api_key=None, model=None):
            called["api_key"] = api_key
            called["model"] = model

        def score(self, *a, **k):
            return {"score": 75, "rationale": "ok"}

    from engine import llm_providers as lp
    monkeypatch.setattr(lp.LLMProviderRegistry, "resolve",
                        staticmethod(lambda name, **kw: _FakeAnthropic(**kw)))
    ingestion.ingest(CSV, "acme", workflow="enrich_sync")
    assert called["api_key"] == "sk-ant-db-key"


def test_tenant_settings_override_db(tmp_path, monkeypatch):
    """tenant.settings['llm'] wins over DB config on the same key."""
    monkeypatch.setenv("SIGNPIM_SECRET_KEY", "test-master-key-123")
    from services.ingestion import CSVIngestionService
    p = PersistenceService(dsn=f"sqlite:///{tmp_path}/ov.db")
    kernel = PlatformKernel(meta_dir="meta")
    kernel.bootstrap()
    ingestion = CSVIngestionService(p, kernel)
    cs = ConfigService(p)
    t = p.get_or_create_tenant("acme")
    cs.set("llm.model", "db-model", tenant_id=t.id)
    with p.session() as s:
        obj = s.get(type(t), t.id)
        obj.settings = {**(obj.settings or {}), "llm": {"model": "tenant-model"}}
    called = {}

    class _FakeAnthropic:
        name = "anthropic"
        deterministic = False

        def __init__(self, api_key=None, model=None):
            called["api_key"] = api_key
            called["model"] = model

        def score(self, *a, **k):
            return {"score": 75, "rationale": "ok"}

    from engine import llm_providers as lp
    monkeypatch.setattr(lp.LLMProviderRegistry, "resolve",
                        staticmethod(lambda name, **kw: _FakeAnthropic(**kw)))
    ingestion.ingest(CSV, "acme", workflow="enrich_sync")
    assert called["model"] == "tenant-model"
