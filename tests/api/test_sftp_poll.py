"""SFTP live coupling: API endpoint, env-based credentials, mocked transport."""
import os
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from services.ingestion import CSVIngestionService
from services.feed_import import SFTPFeedImporter
from engine.kernel import PlatformKernel

CSV = "sku;navn;pris\nP-9;Vaterpas;129,50\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    t = app_mod.persistence.get_or_create_tenant("acme")
    key = AuthService(app_mod.persistence).create_key(t.id, "test", "admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


class _FakeSFTP:
    def __init__(self, files):
        self._files = files
    def listdir_attr(self, d):
        class A: pass
        out = []
        for name, content in self._files.items():
            a = A(); a.filename = name; a.st_size = len(content); a.st_mtime = 1.0
            out.append(a)
        return out
    def open(self, path):
        import io
        name = path.rsplit("/", 1)[-1]
        return io.BytesIO(self._files[name].encode())
    def close(self): pass


class _FakeSSH:
    def __init__(self, files): self.files = files
    def open_sftp(self): return _FakeSFTP(self.files)
    def close(self): pass


def test_sftp_mode_requires_config(env, monkeypatch):
    monkeypatch.delenv("SIGNPIM_SFTP_HOST", raising=False)
    r = env.post("/feeds/acme/poll", params={"mode": "sftp"})
    assert r.status_code == 503


def test_sftp_mode_requires_credentials(env, monkeypatch):
    monkeypatch.setenv("SIGNPIM_SFTP_HOST", "ftp.leverandoer.dk")
    monkeypatch.delenv("SIGNPIM_SFTP_PASSWORD", raising=False)
    monkeypatch.delenv("SIGNPIM_SFTP_KEY_PATH", raising=False)
    r = env.post("/feeds/acme/poll", params={"mode": "sftp"})
    assert r.status_code == 503


def test_sftp_poll_ingests_and_is_idempotent(env, monkeypatch):
    monkeypatch.setenv("SIGNPIM_SFTP_HOST", "ftp.leverandoer.dk")
    monkeypatch.setenv("SIGNPIM_SFTP_USER", "signpim")
    monkeypatch.setenv("SIGNPIM_SFTP_PASSWORD", "secret")
    files = {"leverandoer_a_uge38.csv": CSV}
    fake = _FakeSSH(files)
    monkeypatch.setattr(SFTPFeedImporter, "_connect",
                        staticmethod(lambda *a, **k: fake))
    r1 = env.post("/feeds/acme/poll", params={"mode": "sftp"}).json()
    assert r1["files"][0]["rows_ingested"] == 1
    r2 = env.post("/feeds/acme/poll", params={"mode": "sftp"}).json()
    assert r2["files"][0]["skipped"] is True
    # changed file → re-ingest
    fake.files["leverandoer_a_uge38.csv"] = CSV + "P-10;Savklinge;49,00\n"
    r3 = env.post("/feeds/acme/poll", params={"mode": "sftp"}).json()
    assert r3["files"][0]["skipped"] is False
    assert r3["files"][0]["rows_ingested"] == 1


def test_sftp_poll_isolated_per_tenant(env, monkeypatch):
    monkeypatch.setenv("SIGNPIM_SFTP_HOST", "h")
    monkeypatch.setenv("SIGNPIM_SFTP_PASSWORD", "x")
    fake = _FakeSSH({"a.csv": CSV})
    monkeypatch.setattr(SFTPFeedImporter, "_connect",
                        staticmethod(lambda *a, **k: fake))
    env.post("/feeds/acme/poll", params={"mode": "sftp"})
    # other tenant with no access cannot poll acme via its own slug
    r = env.post("/feeds/other/poll", params={"mode": "sftp"})
    assert r.status_code in (403, 404)
