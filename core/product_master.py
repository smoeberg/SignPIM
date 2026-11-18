"""Application service for the canonical Product Master and supplier offers.

Supplier payloads are source records: raw_data is copied without normalization,
while normalized_data contains the separately transformed representation.
Every offer observation is appended to SupplierOfferSnapshot.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from core.models import (
    ProductMaster,
    ProductMatch,
    Supplier,
    SupplierOffer,
    SupplierOfferSnapshot,
)


class ProductMasterError(ValueError):
    """Invalid Product Master or supplier-offer operation."""


def _decimal(value: Any, field: str) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProductMasterError(f"{field} must be a valid decimal")
    if not result.is_finite():
        raise ProductMasterError(f"{field} must be finite")
    return result


def _valid_gtin(value: Optional[str]) -> bool:
    if not value:
        return False
    digits = str(value).strip()
    if len(digits) not in (8, 12, 13, 14) or not digits.isdigit():
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(
        int(digit) * (3 if (len(body) - index) % 2 else 1)
        for index, digit in enumerate(body)
    )
    return (10 - total % 10) % 10 == check


def _as_dict(row: Any, fields: List[str]) -> Dict[str, Any]:
    result = {}
    for field in fields:
        value = getattr(row, field)
        if isinstance(value, Decimal):
            value = str(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        result[field] = value
    return result


class ProductMasterService:
    def __init__(self, persistence):
        self.persistence = persistence

    def create_supplier(
        self,
        tenant_id: str,
        code: str,
        name: str,
        adapter_key: str,
        adapter_config: Optional[Dict[str, Any]] = None,
        credential_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not all(isinstance(v, str) and v.strip() for v in (code, name, adapter_key)):
            raise ProductMasterError("supplier code, name and adapter_key are required")
        with self.persistence.session() as session:
            supplier = Supplier(
                tenant_id=tenant_id,
                code=code.strip(),
                name=name.strip(),
                adapter_key=adapter_key.strip(),
                adapter_config=deepcopy(adapter_config or {}),
                credential_ref=credential_ref,
            )
            session.add(supplier)
            session.flush()
            return _as_dict(supplier, [
                "id", "tenant_id", "code", "name", "adapter_key", "status",
                "adapter_config", "credential_ref",
            ])

    def upsert_product_master(
        self,
        tenant_id: str,
        master_sku: str,
        name: str,
        *,
        gtin: Optional[str] = None,
        brand: Optional[str] = None,
        manufacturer: Optional[str] = None,
        mpn: Optional[str] = None,
        description: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
        translations: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not master_sku or not master_sku.strip():
            raise ProductMasterError("master_sku is required")
        if not name or not name.strip():
            raise ProductMasterError("name is required")
        normalized_gtin = str(gtin).strip() if gtin else None
        if normalized_gtin and not _valid_gtin(normalized_gtin):
            raise ProductMasterError("gtin has an invalid length or check digit")

        with self.persistence.session() as session:
            product = session.scalar(select(ProductMaster).where(
                ProductMaster.tenant_id == tenant_id,
                ProductMaster.master_sku == master_sku.strip(),
            ))
            is_new = product is None
            if is_new:
                product = ProductMaster(
                    tenant_id=tenant_id,
                    master_sku=master_sku.strip(),
                    name=name.strip(),
                )
                session.add(product)
            product.name = name.strip()
            product.gtin = normalized_gtin
            product.brand = brand
            product.manufacturer = manufacturer
            product.mpn = mpn
            product.description = description
            if attributes is not None:
                product.attributes = deepcopy(attributes)
            if translations is not None:
                product.translations = deepcopy(translations)
            if not is_new:
                product.version += 1
            session.flush()
            return _as_dict(product, [
                "id", "tenant_id", "master_sku", "gtin", "brand",
                "manufacturer", "mpn", "name", "description", "attributes",
                "translations", "status", "version",
            ])

    def upsert_supplier_offer(
        self,
        supplier_id: str,
        supplier_sku: str,
        raw_data: Dict[str, Any],
        *,
        expected_tenant_id: Optional[str] = None,
        supplier_product_id: Optional[str] = None,
        normalized_data: Optional[Dict[str, Any]] = None,
        supplier_url: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        cost_price: Any = None,
        currency: Optional[str] = None,
        stock_quantity: Any = None,
        stock_status: Optional[str] = None,
        active: bool = True,
    ) -> Dict[str, Any]:
        if not supplier_sku or not supplier_sku.strip():
            raise ProductMasterError("supplier_sku is required")
        if not isinstance(raw_data, dict):
            raise ProductMasterError("raw_data must be an object")
        if normalized_data is not None and not isinstance(normalized_data, dict):
            raise ProductMasterError("normalized_data must be an object")
        if currency and (len(currency) != 3 or not currency.isalpha()):
            raise ProductMasterError("currency must be a three-letter code")
        price = _decimal(cost_price, "cost_price")
        quantity = _decimal(stock_quantity, "stock_quantity")
        raw_copy = deepcopy(raw_data)
        normalized_copy = deepcopy(normalized_data or {})

        with self.persistence.session() as session:
            supplier = session.get(Supplier, supplier_id)
            if supplier is None:
                raise ProductMasterError("supplier not found")
            if expected_tenant_id and supplier.tenant_id != expected_tenant_id:
                raise ProductMasterError("supplier does not belong to tenant")
            offer = session.scalar(select(SupplierOffer).where(
                SupplierOffer.supplier_id == supplier_id,
                SupplierOffer.supplier_sku == supplier_sku.strip(),
            ))
            now = datetime.now(timezone.utc)
            if offer is None:
                offer = SupplierOffer(
                    tenant_id=supplier.tenant_id,
                    supplier_id=supplier_id,
                    supplier_sku=supplier_sku.strip(),
                    first_seen_at=now,
                )
                session.add(offer)

            offer.supplier_product_id = supplier_product_id
            offer.supplier_url = supplier_url
            offer.title = title
            offer.description = description
            offer.raw_data = raw_copy
            offer.normalized_data = normalized_copy
            offer.cost_price = price
            offer.currency = currency.upper() if currency else None
            offer.stock_quantity = quantity
            offer.stock_status = stock_status
            offer.active = active
            offer.last_seen_at = now
            session.flush()

            session.add(SupplierOfferSnapshot(
                tenant_id=supplier.tenant_id,
                offer_id=offer.id,
                cost_price=price,
                currency=offer.currency,
                stock_quantity=quantity,
                stock_status=stock_status,
                raw_data=deepcopy(raw_copy),
            ))
            session.flush()
            return _as_dict(offer, [
                "id", "tenant_id", "supplier_id", "product_id", "supplier_sku",
                "supplier_product_id", "supplier_url", "title", "description",
                "raw_data", "normalized_data", "cost_price", "currency",
                "stock_quantity", "stock_status", "active",
            ])

    def match_offer_by_gtin(
        self, offer_id: str, gtin: Optional[str] = None, *,
        expected_tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        with self.persistence.session() as session:
            offer = session.get(SupplierOffer, offer_id)
            if offer is None:
                raise ProductMasterError("supplier offer not found")
            if expected_tenant_id and offer.tenant_id != expected_tenant_id:
                raise ProductMasterError("supplier offer does not belong to tenant")
            source_gtin = (
                (offer.normalized_data or {}).get("gtin")
                or (offer.normalized_data or {}).get("ean")
                or (offer.raw_data or {}).get("gtin")
                or (offer.raw_data or {}).get("ean")
            )
            normalized_gtin = str(source_gtin).strip() if source_gtin else ""
            if gtin is not None and str(gtin).strip() != normalized_gtin:
                raise ProductMasterError("provided GTIN does not match supplier source data")
            if not _valid_gtin(normalized_gtin):
                return None
            product = session.scalar(select(ProductMaster).where(
                ProductMaster.tenant_id == offer.tenant_id,
                ProductMaster.gtin == normalized_gtin,
            ))
            if product is None:
                return None
            offer.product_id = product.id
            match = ProductMatch(
                tenant_id=offer.tenant_id,
                offer_id=offer.id,
                product_id=product.id,
                method="gtin_exact",
                confidence=Decimal("1.0000"),
                status="confirmed",
                evidence={"gtin": normalized_gtin},
            )
            session.add(match)
            session.flush()
            return _as_dict(match, [
                "id", "tenant_id", "offer_id", "product_id",
                "method", "confidence", "status", "evidence",
            ])

    def suggest_brand_mpn_matches(
        self, offer_id: str, brand: Optional[str] = None, mpn: Optional[str] = None, *,
        expected_tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Propose matches using supplier source identity; never auto-link products."""
        with self.persistence.session() as session:
            offer = session.get(SupplierOffer, offer_id)
            if offer is None:
                raise ProductMasterError("supplier offer not found")
            if expected_tenant_id and offer.tenant_id != expected_tenant_id:
                raise ProductMasterError("supplier offer does not belong to tenant")
            normalized = offer.normalized_data or {}
            source = offer.raw_data or {}
            source_brand = normalized.get("brand") or source.get("brand")
            source_mpn = (
                normalized.get("mpn") or source.get("mpn")
                or source.get("manufacturer_part_number")
            )
            if brand and str(brand).strip().casefold() != str(source_brand or "").strip().casefold():
                raise ProductMasterError("provided brand does not match supplier source data")
            if mpn and str(mpn).strip().casefold() != str(source_mpn or "").strip().casefold():
                raise ProductMasterError("provided MPN does not match supplier source data")
            brand = str(source_brand or "").strip()
            mpn = str(source_mpn or "").strip()
            if not brand or not mpn:
                return []
            candidates = session.scalars(select(ProductMaster).where(
                ProductMaster.tenant_id == offer.tenant_id,
                ProductMaster.brand.ilike(brand),
                ProductMaster.mpn.ilike(mpn),
            )).all()
            proposals = []
            for product in candidates:
                match = ProductMatch(
                    tenant_id=offer.tenant_id,
                    offer_id=offer.id,
                    product_id=product.id,
                    method="brand_mpn_exact",
                    confidence=Decimal("0.9000"),
                    status="proposed",
                    evidence={"brand": brand, "mpn": mpn},
                )
                session.add(match)
                session.flush()
                proposals.append(_as_dict(match, [
                    "id", "tenant_id", "offer_id", "product_id",
                    "method", "confidence", "status", "evidence",
                ]))
            return proposals


    def confirm_match(
        self, match_id: str, *, expected_tenant_id: str, reviewer: str
    ) -> Dict[str, Any]:
        with self.persistence.session() as session:
            match = session.get(ProductMatch, match_id)
            if match is None or match.tenant_id != expected_tenant_id:
                raise ProductMasterError("product match not found")
            if match.status != "proposed":
                raise ProductMasterError("only proposed matches can be confirmed")
            offer = session.get(SupplierOffer, match.offer_id)
            if offer is None or offer.tenant_id != expected_tenant_id:
                raise ProductMasterError("supplier offer not found")
            match.status = "confirmed"
            match.reviewed_by = reviewer
            match.reviewed_at = datetime.now(timezone.utc)
            offer.product_id = match.product_id
            session.flush()
            return _as_dict(match, [
                "id", "tenant_id", "offer_id", "product_id", "method",
                "confidence", "status", "evidence", "reviewed_by", "reviewed_at",
            ])

    def reject_match(
        self, match_id: str, *, expected_tenant_id: str, reviewer: str
    ) -> Dict[str, Any]:
        with self.persistence.session() as session:
            match = session.get(ProductMatch, match_id)
            if match is None or match.tenant_id != expected_tenant_id:
                raise ProductMasterError("product match not found")
            if match.status != "proposed":
                raise ProductMasterError("only proposed matches can be rejected")
            match.status = "rejected"
            match.reviewed_by = reviewer
            match.reviewed_at = datetime.now(timezone.utc)
            session.flush()
            return _as_dict(match, [
                "id", "tenant_id", "offer_id", "product_id", "method",
                "confidence", "status", "evidence", "reviewed_by", "reviewed_at",
            ])
