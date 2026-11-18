"""Regression tests for defects found in the Supplier Hub / Product Master review."""
import pytest
from sqlalchemy import select

from core.models import ProductMatch, SupplierOffer, SupplierOfferSnapshot
from core.persistence import PersistenceService
from core.product_master import ProductMasterError, ProductMasterService, normalize_gtin
from services.supplier_adapters import SupplierHubService


@pytest.fixture
def ctx():
    persistence = PersistenceService(dsn="sqlite:///:memory:")
    tenant = persistence.get_or_create_tenant("review")
    yield persistence, tenant.id, ProductMasterService(persistence)
    persistence.engine.dispose()


def test_normalize_gtin_pads_to_14_digits():
    assert normalize_gtin("012345678905") == "00012345678905"
    assert normalize_gtin("0012345678905") == "00012345678905"
    assert normalize_gtin("4006381333932") is None


def test_upc12_offer_matches_ean13_master(ctx):
    persistence, tenant_id, service = ctx
    service.upsert_product_master(tenant_id, "M1", "Product", gtin="0012345678905")
    supplier = service.create_supplier(tenant_id, "s", "S", "csv", {"delimiter": ";"})
    result = SupplierHubService(persistence).import_payload(
        supplier["id"], "sku;ean\nA;012345678905\n", expected_tenant_id=tenant_id
    )
    assert result["gtin_matches"] == 1


def test_same_gtin_in_different_lengths_cannot_create_two_masters(ctx):
    _, tenant_id, service = ctx
    service.upsert_product_master(tenant_id, "M1", "Product", gtin="0012345678905")
    with pytest.raises(ProductMasterError, match="already used"):
        service.upsert_product_master(tenant_id, "M2", "Other", gtin="012345678905")


def test_brand_mpn_values_are_not_like_wildcards(ctx):
    _, tenant_id, service = ctx
    service.upsert_product_master(tenant_id, "M1", "Bosch", brand="Bosch", mpn="X1")
    supplier = service.create_supplier(tenant_id, "s", "S", "csv")
    offer = service.upsert_supplier_offer(supplier["id"], "1", {"brand": "Bo_ch", "mpn": "X%"})
    assert service.suggest_brand_mpn_matches(offer["id"]) == []


def test_brand_mpn_proposals_are_idempotent_and_rejections_stick(ctx):
    persistence, tenant_id, service = ctx
    service.upsert_product_master(tenant_id, "M1", "P", brand="Bosch", mpn="X1")
    supplier = service.create_supplier(tenant_id, "s", "S", "csv")
    offer = service.upsert_supplier_offer(supplier["id"], "1", {"brand": "BOSCH", "mpn": "x1"})
    first = service.suggest_brand_mpn_matches(offer["id"])
    second = service.suggest_brand_mpn_matches(offer["id"])
    assert len(first) == 1 and first[0]["id"] == second[0]["id"]
    service.reject_match(first[0]["id"], expected_tenant_id=tenant_id, reviewer="r")
    assert service.suggest_brand_mpn_matches(offer["id"]) == []
    with persistence.session() as s:
        assert len(s.scalars(select(ProductMatch)).all()) == 1


def test_unchanged_reimport_does_not_add_snapshots(ctx):
    persistence, tenant_id, service = ctx
    supplier = service.create_supplier(tenant_id, "s", "S", "csv", {"delimiter": ";"})
    hub = SupplierHubService(persistence)
    feed = "sku;price\nA;10,5\n"
    hub.import_payload(supplier["id"], feed, expected_tenant_id=tenant_id)
    hub.import_payload(supplier["id"], feed, expected_tenant_id=tenant_id)
    hub.import_payload(supplier["id"], "sku;price\nA;11\n", expected_tenant_id=tenant_id)
    with persistence.session() as s:
        assert len(s.scalars(select(SupplierOfferSnapshot)).all()) == 2


def test_csv_raw_data_is_kept_exactly_as_received(ctx):
    persistence, tenant_id, service = ctx
    supplier = service.create_supplier(tenant_id, "s", "S", "csv", {"delimiter": ";"})
    SupplierHubService(persistence).import_payload(
        supplier["id"], "sku;brand\n A ; Bosch \n", expected_tenant_id=tenant_id
    )
    with persistence.session() as s:
        offer = s.scalars(select(SupplierOffer)).one()
        assert offer.supplier_sku == "A"
        assert offer.raw_data == {"sku": " A ", "brand": " Bosch "}
        assert offer.normalized_data["brand"] == "Bosch"


def test_full_feed_deactivates_missing_offers_and_reactivates_returning(ctx):
    persistence, tenant_id, service = ctx
    supplier = service.create_supplier(tenant_id, "s", "S", "csv", {"delimiter": ";"})
    hub = SupplierHubService(persistence)
    hub.import_payload(supplier["id"], "sku\nA\nB\nC\n", expected_tenant_id=tenant_id)
    result = hub.import_payload(supplier["id"], "sku\nA\nB\n", expected_tenant_id=tenant_id)
    assert result["offers_deactivated"] == 1
    hub.import_payload(supplier["id"], "sku\nA\nB\nC\n", expected_tenant_id=tenant_id)
    with persistence.session() as s:
        assert all(o.active for o in s.scalars(select(SupplierOffer)).all())


def test_delisting_guard_skips_suspiciously_large_drop(ctx):
    persistence, tenant_id, service = ctx
    supplier = service.create_supplier(tenant_id, "s", "S", "csv", {"delimiter": ";"})
    hub = SupplierHubService(persistence)
    hub.import_payload(supplier["id"], "sku\nA\nB\nC\nD\n", expected_tenant_id=tenant_id)
    result = hub.import_payload(supplier["id"], "sku\nA\n", expected_tenant_id=tenant_id)
    assert result["offers_deactivated"] == 0 and "max_delist_pct" in result["delisting_skipped"]


def test_delta_feed_never_deactivates(ctx):
    persistence, tenant_id, service = ctx
    supplier = service.create_supplier(
        tenant_id, "s", "S", "csv", {"delimiter": ";", "feed_mode": "delta"}
    )
    hub = SupplierHubService(persistence)
    hub.import_payload(supplier["id"], "sku\nA\nB\n", expected_tenant_id=tenant_id)
    result = hub.import_payload(supplier["id"], "sku\nA\n", expected_tenant_id=tenant_id)
    assert result["offers_deactivated"] == 0


def test_full_sync_workflow_does_not_change_price():
    from engine.kernel import PlatformKernel
    kernel = PlatformKernel(meta_dir="meta")
    result = kernel.run_workflow("full_sync", {"sku": "A", "name": "W", "price": 100.0}, "t")
    assert result["data"]["price"] == 100.0
