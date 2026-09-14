"""Provider-side roles and multi-organization account isolation."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.saas_auth import SaaSAuthService


@pytest.fixture()
def env(tmp_path):
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/saas.db")
    service = SaaSAuthService(app_mod.persistence)
    acme = app_mod.persistence.get_or_create_tenant("acme")
    other = app_mod.persistence.get_or_create_tenant("other")

    owner = service.create_account("owner@signpim.dk", "OwnerPassword1")
    service.grant_platform_role(owner.id, "platform_owner")
    platform_token = service.login("owner@signpim.dk", "OwnerPassword1")["token"]

    user = service.create_account("multi@example.com", "MultiPassword1")
    service.add_membership(user.id, acme.id, "admin")
    service.add_membership(user.id, other.id, "reader")
    return TestClient(app_mod.app), service, platform_token


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_platform_role_can_list_and_create_organizations(env):
    client, service, token = env
    assert client.get("/platform/organizations", headers=_bearer(token)).status_code == 200
    created = client.post("/platform/organizations", json={"slug": "new-customer"},
                          headers=_bearer(token))
    assert created.status_code == 200
    assert created.json()["slug"] == "new-customer"


def test_only_platform_role_can_write_global_config(env, monkeypatch):
    client, service, token = env
    monkeypatch.setenv("SIGNPIM_SECRET_KEY", "test-master-key")
    response = client.put("/platform/config/llm.provider", json={"value": "anthropic"},
                          headers=_bearer(token))
    assert response.status_code == 200
    assert client.get("/platform/config", headers=_bearer(token)).json()["llm.provider"] == "anthropic"


def test_organization_admin_cannot_use_platform_api(env):
    client, service, platform_token = env
    token = service.login("multi@example.com", "MultiPassword1", "acme")["token"]
    assert client.get("/platform/organizations", headers=_bearer(token)).status_code == 403


def test_one_account_has_different_roles_per_organization(env):
    client, service, platform_token = env
    acme = service.login("multi@example.com", "MultiPassword1", "acme")
    other = service.login("multi@example.com", "MultiPassword1", "other")
    assert acme["account"]["id"] == other["account"]["id"]
    assert acme["organization_role"] == "admin"
    assert other["organization_role"] == "reader"


def test_org_context_limits_member_admin(env):
    client, service, platform_token = env
    acme_token = service.login("multi@example.com", "MultiPassword1", "acme")["token"]
    other_token = service.login("multi@example.com", "MultiPassword1", "other")["token"]
    assert client.get("/organization/members", headers=_bearer(acme_token)).status_code == 200
    assert client.get("/organization/members", headers=_bearer(other_token)).status_code == 403


def test_platform_session_has_no_implicit_customer_data_access(env):
    client, service, token = env
    response = client.get("/organization/members", headers=_bearer(token))
    assert response.status_code == 403


def test_multiple_memberships_require_explicit_context(env):
    client, service, platform_token = env
    response = client.post("/auth/saas/login", json={
        "email": "multi@example.com", "password": "MultiPassword1"})
    assert response.status_code == 200
    assert response.json()["organization_required"] is True
    assert "token" not in response.json()
