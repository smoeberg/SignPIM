import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.auth import AuthService
from core.models import ProductMaster, SupplierOffer, SupplierOfferSnapshot
from core.persistence import PersistenceService


@pytest.fixture
def client(tmp_path):
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/supplier-hub.db")
    app_mod._auth = None
    app_mod._users = None
    tenant = app_mod.persistence.get_or_create_tenant("acme")
    key = AuthService(app_mod.persistence).create_key(
        tenant.id, "supplier-admin", role="admin"
    )["key"]
    http = TestClient(app_mod.app)
    http.headers.update({"Authorization": f"Bearer {key}"})
    return http, app_mod.persistence


def test_supplier_hub_imports_csv_to_offers_and_preserves_source(client):
    http, persistence = client
    supplier = http.post("/catalog/suppliers?tenant=acme", json={
        "code": "csv-a", "name": "CSV Supplier", "adapter_key": "csv",
        "adapter_config": {"delimiter": ";", "field_map": {
            "supplier_sku": "ItemNo", "cost_price": "BuyPrice", "gtin": "EAN"
        }},
    }).json()
    response = http.post(
        f"/catalog/suppliers/{supplier['id']}/import?tenant=acme",
        json={"content_type": "text/csv",
              "payload": "ItemNo;BuyPrice;EAN\nA-1;25,50;4006381333931\n"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["offers_imported"] == 1
    with persistence.session() as session:
        offer = session.query(SupplierOffer).one()
        assert offer.raw_data["BuyPrice"] == "25,50"
        assert offer.normalized_data["gtin"] == "4006381333931"
        assert str(offer.cost_price) == "25.5000"
        assert session.query(SupplierOfferSnapshot).count() == 1
        assert session.query(ProductMaster).count() == 0


def test_supplier_hub_rejects_cross_tenant_supplier(client):
    http, persistence = client
    other = persistence.get_or_create_tenant("other")
    from core.product_master import ProductMasterService
    supplier = ProductMasterService(persistence).create_supplier(
        other.id, "foreign", "Foreign", "csv"
    )
    response = http.post(
        f"/catalog/suppliers/{supplier['id']}/import?tenant=acme",
        json={"payload": "sku,price\nA,1\n"},
    )
    assert response.status_code == 404


def test_supplier_hub_rejects_content_type_mismatch(client):
    http, _ = client
    supplier = http.post("/catalog/suppliers?tenant=acme", json={
        "code": "json-a", "name": "JSON Supplier", "adapter_key": "json",
    }).json()
    response = http.post(
        f"/catalog/suppliers/{supplier['id']}/import?tenant=acme",
        json={"content_type": "text/csv", "payload": '{"items": []}'},
    )
    assert response.status_code == 400
