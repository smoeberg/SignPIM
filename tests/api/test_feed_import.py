"""Feed import: idempotent checksum-based polling (local + SFTP logic)."""
import os
import hashlib
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from services.ingestion import CSVIngestionService
from services.feed_import import LocalFeedImporter, FeedImportState
from engine.kernel import PlatformKernel

CSV = "sku,name,price,ean,images\nP-1,Stol,499.0,5901234123457,x.jpg\n"


@pytest.fixture()
def env(tmp_path):
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    feed_dir = tmp_path / "feeds"
    feed_dir.mkdir()
    (feed_dir / "supplier_a.csv").write_text(CSV)
    t = app_mod.persistence.get_or_create_tenant("acme")
    key = AuthService(app_mod.persistence).create_key(t.id, "test", "admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c, str(feed_dir)


def test_poll_ingests_new_files(env):
    c, feed_dir = env
    r = c.post("/feeds/acme/poll", params={"directory": feed_dir})
    assert r.status_code == 200
    files = r.json()["files"]
    assert len(files) == 1
    assert files[0]["skipped"] is False
    assert files[0]["rows_ingested"] == 1


def test_poll_is_idempotent(env):
    c, feed_dir = env
    r1 = c.post("/feeds/acme/poll", params={"directory": feed_dir}).json()
    r2 = c.post("/feeds/acme/poll", params={"directory": feed_dir}).json()
    assert r1["files"][0]["skipped"] is False
    assert r2["files"][0]["skipped"] is True   # unchanged → skipped


def test_changed_file_reingested(env):
    c, feed_dir = env
    r1 = c.post("/feeds/acme/poll", params={"directory": feed_dir}).json()
    assert r1["files"][0]["skipped"] is False
    # change the file content
    with open(os.path.join(feed_dir, "supplier_a.csv"), "a") as f:
        f.write("P-2,Ny Bord,1999.0,5901234123457,y.jpg\n")
    r2 = c.post("/feeds/acme/poll", params={"directory": feed_dir}).json()
    assert r2["files"][0]["skipped"] is False
    assert r2["files"][0]["rows_ingested"] == 2  # full re-ingest (upsert semantics)


def test_state_endpoint(env):
    c, feed_dir = env
    c.post("/feeds/acme/poll", params={"directory": feed_dir})
    r = c.get("/feeds/acme/state")
    assert r.status_code == 200
    states = r.json()
    assert len(states) == 1
    assert len(states[0]["checksum"]) == 12     # short checksum shown, never full
    assert states[0]["rows"] == "1"


def test_only_csv_files_polled(env):
    c, feed_dir = env
    with open(os.path.join(feed_dir, "readme.txt"), "w") as f: f.write("notes")
    with open(os.path.join(feed_dir, "image.png"), "w") as f: f.write("not a feed")
    r = c.post("/feeds/acme/poll", params={"directory": feed_dir}).json()
    assert len(r["files"]) == 2                 # .csv + .txt, .png ignored
    by_file = {f["file"]: f for f in r["files"]}
    assert by_file["supplier_a.csv"]["rows_ingested"] == 1
    assert "error" in by_file["readme.txt"]     # invalid feed recorded, poll continues
