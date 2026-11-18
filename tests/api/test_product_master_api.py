"""HTTP contract tests for the new canonical catalogue endpoints."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from core.product_master import ProductMasterService


@pytest.fixture
def client(tmp_path):
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/catalog.db")
    app_mod._auth = None
    app_mod._users = None
    tenant = app_mod.persistence.get_or_create_tenant("acme")
    key = AuthService(app_mod.persistence).create_key(
        tenant.id, "catalog-admin", role="admin"
    )["key"]
    test_client = TestClient(app_mod.app)
    test_client.headers.update({"Authorization": f"Bearer {key}"})
    return test_client, app_mod.persistence, tenant.id


def test_catalog_api_creates_master_supplier_offer_and_exact_match(client):
    http, persistence, tenant_id = client
    supplier_response = http.post("/catalog/suppliers?tenant=acme", json={
        "code": "supplier-a",
        "name": "Supplier A",
        "adapter_key": "supplier_a",
    })
    assert supplier_response.status_code == 200, supplier_response.text
    supplier_id = supplier_response.json()["id"]

    product_response = http.post("/catalog/products?tenant=acme", json={
        "master_sku": "MASTER-001",
        "name": "Example product",
        "gtin": "4006381333931",
        "brand": "Example",
        "mpn": "MODEL-1",
    })
    assert product_response.status_code == 200, product_response.text
    product_id = product_response.json()["id"]

    offer_response = http.post(
        f"/catalog/suppliers/{supplier_id}/offers?tenant=acme",
        json={
            "supplier_sku": "SUP-001",
            "raw_data": {"ean": "4006381333931", "source_name": "Supplier title"},
            "normalized_data": {"gtin": "4006381333931"},
            "cost_price": 12.5,
            "currency": "DKK",
        },
    )
    assert offer_response.status_code == 200, offer_response.text
    offer_id = offer_response.json()["id"]
    assert offer_response.json()["product_id"] is None

    match_response = http.post(
        f"/catalog/offers/{offer_id}/match-gtin?tenant=acme&gtin=4006381333931"
    )
    assert match_response.status_code == 200, match_response.text
    assert match_response.json()["product_id"] == product_id


def test_catalog_api_rejects_cross_tenant_supplier_offer_access(client):
    http, persistence, tenant_id = client
    other_tenant = persistence.get_or_create_tenant("other")
    other_supplier = ProductMasterService(persistence).create_supplier(
        other_tenant.id, "foreign", "Foreign Supplier", "foreign_adapter"
    )
    response = http.post(
        f"/catalog/suppliers/{other_supplier['id']}/offers?tenant=acme",
        json={"supplier_sku": "FOREIGN-1", "raw_data": {"sku": "FOREIGN-1"}},
    )
    assert response.status_code == 400
    assert "does not belong to tenant" in response.json()["detail"]
