"""Product Master domain tests: supplier isolation, immutable source snapshots and matching."""
import pytest
from sqlalchemy import select

from core.persistence import PersistenceService
from core.models import ProductMaster, SupplierOffer, SupplierOfferSnapshot, ProductMatch
from core.product_master import ProductMasterError, ProductMasterService


@pytest.fixture
def catalog():
    persistence = PersistenceService(dsn="sqlite:///:memory:")
    tenant = persistence.get_or_create_tenant("catalog-test")
    service = ProductMasterService(persistence)
    yield persistence, tenant.id, service
    persistence.engine.dispose()


def test_one_canonical_product_can_have_offers_from_multiple_suppliers(catalog):
    persistence, tenant_id, service = catalog
    supplier_a = service.create_supplier(tenant_id, "supplier-a", "Supplier A", "adapter_a")
    supplier_b = service.create_supplier(tenant_id, "supplier-b", "Supplier B", "adapter_b")
    product = service.upsert_product_master(
        tenant_id, "MASTER-001", "Cordless drill", brand="Example", mpn="X-100"
    )

    offer_a = service.upsert_supplier_offer(
        supplier_a["id"], "SUP-001", {"supplier_sku": "SUP-001", "name": "Drill A"},
        cost_price="100.00", currency="dkk", stock_quantity=5,
    )
    offer_b = service.upsert_supplier_offer(
        supplier_b["id"], "SUP-001", {"supplier_sku": "SUP-001", "name": "Drill B"},
        cost_price="95.50", currency="EUR", stock_quantity=8,
    )

    proposal_a = service.suggest_brand_mpn_matches(offer_a["id"], "Example", "X-100")
    proposal_b = service.suggest_brand_mpn_matches(offer_b["id"], "Example", "X-100")
    service.confirm_match(proposal_a[0]["id"], expected_tenant_id=tenant_id, reviewer="reviewer")
    service.confirm_match(proposal_b[0]["id"], expected_tenant_id=tenant_id, reviewer="reviewer")
    assert offer_a["id"] != offer_b["id"]
    assert offer_a["product_id"] is None and offer_b["product_id"] is None
    with persistence.session() as session:
        linked_a = session.get(SupplierOffer, offer_a["id"])
        linked_b = session.get(SupplierOffer, offer_b["id"])
        assert linked_a.product_id == linked_b.product_id == product["id"]
    assert offer_a["supplier_id"] != offer_b["supplier_id"]
    with persistence.session() as session:
        assert session.scalar(select(ProductMaster).where(
            ProductMaster.tenant_id == tenant_id
        )).master_sku == "MASTER-001"
        assert len(session.scalars(select(SupplierOffer)).all()) == 2


def test_supplier_raw_data_is_preserved_and_each_observation_is_snapshotted(catalog):
    persistence, tenant_id, service = catalog
    supplier = service.create_supplier(tenant_id, "supplier-a", "Supplier A", "adapter_a")
    source = {"sku": "A-1", "name": "Original supplier title", "price": "12,50"}
    offer = service.upsert_supplier_offer(
        supplier["id"], "A-1", source,
        normalized_data={"name": "Normalized title", "cost_price": 12.5},
        cost_price="12.50", currency="DKK", stock_quantity=4,
    )
    source["name"] = "mutated by caller"
    service.upsert_supplier_offer(
        supplier["id"], "A-1",
        {"sku": "A-1", "name": "Updated supplier title", "price": "13,00"},
        normalized_data={"name": "Updated normalized title"},
        cost_price="13.00", currency="DKK", stock_quantity=7,
    )

    with persistence.session() as session:
        stored = session.get(SupplierOffer, offer["id"])
        snapshots = session.scalars(select(SupplierOfferSnapshot).where(
            SupplierOfferSnapshot.offer_id == offer["id"]
        ).order_by(SupplierOfferSnapshot.captured_at)).all()
        assert stored.raw_data["name"] == "Updated supplier title"
        assert stored.normalized_data["name"] == "Updated normalized title"
        assert len(snapshots) == 2
        assert snapshots[0].raw_data["name"] == "Original supplier title"
        assert snapshots[0].cost_price == 12.5
        assert snapshots[1].raw_data["name"] == "Updated supplier title"


def test_supplier_sku_is_scoped_to_supplier_not_tenant(catalog):
    _, tenant_id, service = catalog
    first = service.create_supplier(tenant_id, "supplier-a", "Supplier A", "adapter_a")
    second = service.create_supplier(tenant_id, "supplier-b", "Supplier B", "adapter_b")
    a = service.upsert_supplier_offer(first["id"], "DUPLICATE-SKU", {"sku": "DUPLICATE-SKU"})
    b = service.upsert_supplier_offer(second["id"], "DUPLICATE-SKU", {"sku": "DUPLICATE-SKU"})
    assert a["id"] != b["id"]


def test_exact_gtin_match_links_offer_and_brand_mpn_only_proposes(catalog):
    persistence, tenant_id, service = catalog
    supplier = service.create_supplier(tenant_id, "supplier-a", "Supplier A", "adapter_a")
    product = service.upsert_product_master(
        tenant_id, "MASTER-GTIN", "Example product",
        gtin="4006381333931", brand="Example", mpn="MODEL-1",
    )
    offer = service.upsert_supplier_offer(
        supplier["id"], "SUP-GTIN", {"gtin": "4006381333931", "brand": "Example", "mpn": "MODEL-1"}
    )
    match = service.match_offer_by_gtin(offer["id"], "4006381333931")
    assert match["method"] == "gtin_exact"
    assert match["status"] == "confirmed"
    assert match["confidence"] == "1.0000"

    another = service.upsert_supplier_offer(
        supplier["id"], "SUP-MPN", {"brand": "Example", "mpn": "MODEL-1"}
    )
    proposals = service.suggest_brand_mpn_matches(another["id"], "Example", "MODEL-1")
    assert len(proposals) == 1
    assert proposals[0]["status"] == "proposed"
    with persistence.session() as session:
        stored_offer = session.get(SupplierOffer, another["id"])
        assert stored_offer.product_id is None
        assert session.scalar(select(ProductMatch).where(
            ProductMatch.offer_id == another["id"]
        )).status == "proposed"


def test_invalid_gtin_is_rejected_for_canonical_product(catalog):
    _, tenant_id, service = catalog
    with pytest.raises(ProductMasterError, match="invalid length or check digit"):
        service.upsert_product_master(
            tenant_id, "MASTER-BAD", "Bad GTIN product", gtin="4006381333932"
        )



def test_gtin_match_cannot_override_supplier_source_identity(catalog):
    _, tenant_id, service = catalog
    supplier = service.create_supplier(tenant_id, "supplier-a", "Supplier A", "adapter_a")
    service.upsert_product_master(
        tenant_id, "MASTER-GTIN", "Example product", gtin="4006381333931"
    )
    offer = service.upsert_supplier_offer(
        supplier["id"], "SUP-GTIN", {"gtin": "4006381333931"}
    )
    with pytest.raises(ProductMasterError, match="does not match supplier source data"):
        service.match_offer_by_gtin(offer["id"], "4006381333932")
