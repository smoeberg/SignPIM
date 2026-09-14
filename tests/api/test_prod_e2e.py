"""Production-path E2E: env-configured DSN, /health, OpenAPI contract."""
import os
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient


def test_health_endpoint():
    from api.app import app
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_env_dsn_is_respected():
    """POSTGRES_DSN/DATABASE_URL env var must override the default SQLite DSN."""
    os.environ["DATABASE_URL"] = "sqlite:///./_test_env_dsn.db"
    try:
        # importlib reload picks up env
        import importlib
        import api.app as app_mod
        importlib.reload(app_mod)
        assert app_mod._DSN == "sqlite:///./_test_env_dsn.db"
    finally:
        os.environ.pop("DATABASE_URL", None)
        if os.path.exists("./_test_env_dsn.db"):
            os.remove("./_test_env_dsn.db")


def test_openapi_spec_is_current():
    """The exported spec must match the live app (contract drift guard)."""
    from api.app import app
    live = app.openapi()
    with open("docs/openapi.json") as f:
        exported = json.load(f)
    assert set(live["paths"]) == set(exported["paths"]), (
        f"Contract drift: live={sorted(live['paths'])} vs exported={sorted(exported['paths'])}. "
        "Run: python scripts/export_openapi.py docs/openapi.json"
    )


def test_dockerfile_targets_existing_app():
    """Regression guard: the Dockerfile CMD must point to a real module."""
    with open("Dockerfile.api") as f:
        dockerfile = f.read()
    assert "api.app:app" in dockerfile, "CMD must reference api.app:app"
    assert "useradd" in dockerfile and "kodegen" in dockerfile, "must run non-root"
    assert "HEALTHCHECK" in dockerfile, "must define a healthcheck"
