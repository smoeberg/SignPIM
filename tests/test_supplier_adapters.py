import pytest

from core.persistence import PersistenceService
from core.product_master import ProductMasterService
from core.models import SupplierOffer, SupplierOfferSnapshot, ProductMaster
from services.supplier_adapters import (
    CSVSupplierAdapter, JSONSupplierAdapter, SupplierAdapterError,
    SupplierAdapterRegistry, SupplierHubService,
)


@pytest.fixture
def hub():
    persistence = PersistenceService()
    tenant = persistence.get_or_create_tenant("supplier-test")
    supplier = ProductMasterService(persistence).create_supplier(
        tenant.id, "supplier-a", "Supplier A", "csv",
        adapter_config={"field_map": {
            "supplier_sku": "Artikelnummer", "cost_price": "Indkoeb",
            "gtin": "EAN", "title": "Produktnavn", "currency": "Valuta",
        }, "delimiter": ";"},
    )
    return persistence, tenant, supplier, SupplierHubService(persistence)


def test_csv_adapter_maps_supplier_fields_but_preserves_raw_source(hub):
    persistence, tenant, supplier, service = hub
    payload = "Artikelnummer;Indkoeb;EAN;Produktnavn;Valuta\nA-100;12,50;4006381333931;Boremaskine;DKK\n"
    result = service.import_payload(supplier["id"], payload, expected_tenant_id=tenant.id)
    assert result["records_seen"] == 1
    assert result["offers_imported"] == 1
    with persistence.session() as session:
        offer = session.query(SupplierOffer).one()
        assert offer.supplier_sku == "A-100"
        assert offer.raw_data["Indkoeb"] == "12,50"
        assert offer.normalized_data["gtin"] == "4006381333931"
        assert str(offer.cost_price) == "12.5000"
        snapshot = session.query(SupplierOfferSnapshot).one()
        assert snapshot.raw_data == offer.raw_data


def test_json_adapter_supports_nested_record_path():
    adapter = JSONSupplierAdapter()
    records = adapter.parse(
        '{"data":{"items":[{"id":"P-1","wholesale":"10.25"}]}}',
        {"records_path": "data.items", "field_map": {
            "supplier_sku": "id", "cost_price": "wholesale"
        }},
    )
    assert records[0].supplier_sku == "P-1"
    assert records[0].cost_price == "10.25"
    assert records[0].raw_data == {"id": "P-1", "wholesale": "10.25"}


def test_adapter_registry_rejects_duplicate_and_unknown_keys():
    registry = SupplierAdapterRegistry.with_builtins()
    with pytest.raises(SupplierAdapterError, match="already registered"):
        registry.register(CSVSupplierAdapter())
    with pytest.raises(SupplierAdapterError, match="unknown supplier adapter"):
        registry.resolve("does-not-exist")


def test_csv_adapter_rejects_duplicate_headers():
    with pytest.raises(SupplierAdapterError, match="duplicate headers"):
        CSVSupplierAdapter().parse("sku;sku\nA;B\n", {"delimiter": ";"})


def test_hub_rejects_cross_tenant_supplier(hub):
    persistence, tenant, supplier, service = hub
    other = persistence.get_or_create_tenant("other-tenant")
    with pytest.raises(Exception, match="supplier not found"):
        service.import_payload(supplier["id"], "sku,price\nA,1\n", expected_tenant_id=other.id)


def test_import_does_not_create_canonical_products(hub):
    persistence, tenant, supplier, service = hub
    result = service.import_payload(
        supplier["id"], "Artikelnummer;Indkoeb\nA-100;12,50\n",
        expected_tenant_id=tenant.id,
    )
    assert result["offers_imported"] == 1
    with persistence.session() as session:
        assert session.query(ProductMaster).count() == 0
        assert session.query(SupplierOffer).count() == 1
