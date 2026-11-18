"""Supplier adapter contract and the central Supplier Hub.

Vendor adapters translate transport/feed-specific records into one stable
SupplierRecord contract. They never create canonical products: imports create
or update SupplierOffer records and retain the exact source row in raw_data.
"""
from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from datetime import datetime, timezone

from sqlalchemy import select

from core.models import Supplier, SupplierOffer
from core.outbox import enqueue_in_session
from core.product_master import ProductMasterError, ProductMasterService


class SupplierAdapterError(ValueError):
    """Invalid supplier payload or adapter configuration."""


@dataclass(frozen=True)
class SupplierRecord:
    supplier_sku: str
    raw_data: Dict[str, Any]
    normalized_data: Dict[str, Any] = field(default_factory=dict)
    supplier_product_id: Optional[str] = None
    supplier_url: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    cost_price: Optional[str] = None
    currency: Optional[str] = None
    stock_quantity: Optional[str] = None
    stock_status: Optional[str] = None
    active: Optional[bool] = None
    rrp: Optional[str] = None
    rrp_currency: Optional[str] = None
    rrp_includes_vat: Optional[bool] = None


class SupplierAdapter(Protocol):
    """All supplier adapters must return records in the common contract."""

    adapter_key: str

    def parse(self, payload: str | bytes, config: Mapping[str, Any]) -> Sequence[SupplierRecord]:
        ...


def _decode(payload: str | bytes, encoding: str = "utf-8-sig") -> str:
    if isinstance(payload, bytes):
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise SupplierAdapterError(f"could not decode supplier payload: {exc}") from exc
    if not isinstance(payload, str):
        raise SupplierAdapterError("supplier payload must be text or bytes")
    return payload


def _decimal_text(value: Any, field_name: str) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    # Supplier feeds commonly use Danish decimal commas. Do not guess thousands
    # separators: mixed comma and dot is rejected rather than silently altered.
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise SupplierAdapterError(f"{field_name} is not a valid decimal") from exc
    if not number.is_finite():
        raise SupplierAdapterError(f"{field_name} must be finite")
    return format(number, "f")


def _record(raw: Mapping[str, Any], config: Mapping[str, Any]) -> SupplierRecord:
    field_map = config.get("field_map") or {}
    if not isinstance(field_map, Mapping):
        raise SupplierAdapterError("field_map must be an object")

    def get(name: str, *fallbacks: str) -> Any:
        source_name = field_map.get(name)
        if source_name:
            return raw.get(str(source_name))
        for candidate in (name, *fallbacks):
            if candidate in raw:
                return raw.get(candidate)
        return None

    sku = get("supplier_sku", "sku")
    if sku is None or not str(sku).strip():
        raise SupplierAdapterError("supplier row is missing supplier_sku")
    price = _decimal_text(get("cost_price", "price"), "cost_price")
    quantity = _decimal_text(get("stock_quantity", "quantity"), "stock_quantity")
    rrp = _decimal_text(get("rrp", "recommended_retail_price"), "rrp")
    rrp_vat = config.get("rrp_includes_vat")
    if rrp_vat is not None and not isinstance(rrp_vat, bool):
        raise SupplierAdapterError("rrp_includes_vat must be true or false")
    normalized = {
        key: _clean(get(key, *fallbacks))
        for key, fallbacks in {
            "gtin": ("ean",), "ean": ("gtin",), "brand": (),
            "mpn": ("manufacturer_part_number",),
            "manufacturer": (), "name": ("title",), "title": ("name",),
            "description": (), "currency": (), "supplier_url": ("url",),
        }.items()
    }
    return SupplierRecord(
        supplier_sku=str(sku).strip(),
        raw_data=deepcopy(dict(raw)),
        normalized_data={k: v for k, v in normalized.items() if v not in (None, "")},
        supplier_product_id=_optional_text(get("supplier_product_id", "product_id")),
        supplier_url=_optional_text(get("supplier_url", "url")),
        title=_optional_text(get("title", "name")),
        description=_optional_text(get("description")),
        cost_price=price,
        currency=_optional_text(get("currency")),
        stock_quantity=quantity,
        stock_status=_optional_text(get("stock_status")),
        rrp=rrp,
        rrp_currency=_optional_text(get("rrp_currency")) or (
            _optional_text(config.get("rrp_currency")) if rrp is not None else None
        ),
        rrp_includes_vat=rrp_vat if rrp is not None else None,
    )


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class CSVSupplierAdapter:
    adapter_key = "csv"

    def parse(self, payload: str | bytes, config: Mapping[str, Any]) -> Sequence[SupplierRecord]:
        content = _decode(payload, str(config.get("encoding", "utf-8-sig")))
        if not content.strip():
            raise SupplierAdapterError("supplier CSV is empty")
        delimiter = config.get("delimiter")
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(content[:8192], delimiters=",;\\t|").delimiter
            except csv.Error:
                delimiter = ","
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise SupplierAdapterError("CSV delimiter must be one character")
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        if not reader.fieldnames:
            raise SupplierAdapterError("supplier CSV has no header row")
        headers = [str(header).strip() for header in reader.fieldnames]
        if len(headers) != len(set(headers)):
            raise SupplierAdapterError("supplier CSV contains duplicate headers")
        records = []
        for row_number, row in enumerate(reader, start=2):
            # Header names are trimmed so field_map can address them; cell
            # values are kept exactly as received (raw_data invariant).
            raw = {
                str(key).strip(): value
                for key, value in row.items() if key is not None
            }
            if not any(
                isinstance(value, str) and value.strip() for value in raw.values()
            ):
                continue
            try:
                records.append(_record(raw, config))
            except SupplierAdapterError as exc:
                raise SupplierAdapterError(f"CSV row {row_number}: {exc}") from exc
        return records


class JSONSupplierAdapter:
    adapter_key = "json"

    def parse(self, payload: str | bytes, config: Mapping[str, Any]) -> Sequence[SupplierRecord]:
        content = _decode(payload, str(config.get("encoding", "utf-8-sig")))
        try:
            document = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SupplierAdapterError(f"invalid supplier JSON: {exc.msg}") from exc
        records_path = config.get("records_path")
        rows: Any = document
        if records_path:
            if not isinstance(records_path, str):
                raise SupplierAdapterError("records_path must be a dotted string")
            for part in records_path.split("."):
                if not isinstance(rows, Mapping) or part not in rows:
                    raise SupplierAdapterError(f"records_path not found: {records_path}")
                rows = rows[part]
        if not isinstance(rows, list):
            raise SupplierAdapterError("supplier JSON must contain an array of records")
        records = []
        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                raise SupplierAdapterError(f"JSON record {row_number} must be an object")
            try:
                records.append(_record(row, config))
            except SupplierAdapterError as exc:
                raise SupplierAdapterError(f"JSON record {row_number}: {exc}") from exc
        return records


class SupplierAdapterRegistry:
    """Explicit adapter registry; duplicate keys are rejected."""

    def __init__(self, adapters: Optional[Sequence[SupplierAdapter]] = None):
        self._adapters: Dict[str, SupplierAdapter] = {}
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter: SupplierAdapter) -> None:
        key = getattr(adapter, "adapter_key", None)
        if not isinstance(key, str) or not key.strip():
            raise SupplierAdapterError("adapter must define a non-empty adapter_key")
        key = key.strip().casefold()
        if key in self._adapters:
            raise SupplierAdapterError(f"supplier adapter already registered: {key}")
        if not callable(getattr(adapter, "parse", None)):
            raise SupplierAdapterError(f"supplier adapter {key} has no parse method")
        self._adapters[key] = adapter

    def resolve(self, adapter_key: str) -> SupplierAdapter:
        adapter = self._adapters.get(str(adapter_key).strip().casefold())
        if adapter is None:
            raise SupplierAdapterError(f"unknown supplier adapter: {adapter_key}")
        return adapter

    @classmethod
    def with_builtins(cls) -> "SupplierAdapterRegistry":
        return cls((CSVSupplierAdapter(), JSONSupplierAdapter()))


class SupplierHubService:
    """Coordinates supplier adapter execution and ProductMaster offer upserts."""

    def __init__(self, persistence, registry: Optional[SupplierAdapterRegistry] = None):
        self.persistence = persistence
        self.registry = registry or SupplierAdapterRegistry.with_builtins()
        self.products = ProductMasterService(persistence)

    def import_payload(
        self, supplier_id: str, payload: str | bytes, *,
        expected_tenant_id: str, content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.persistence.session() as session:
            supplier = session.get(Supplier, supplier_id)
            if supplier is None or supplier.tenant_id != expected_tenant_id:
                raise ProductMasterError("supplier not found")
            if supplier.status != "active":
                raise ProductMasterError("supplier is not active")
            adapter_key = supplier.adapter_key
            config = deepcopy(supplier.adapter_config or {})

        adapter = self.registry.resolve(adapter_key)
        allowed_types = {adapter_key.casefold()}
        allowed_types |= {
            "csv": {"text/csv"}, "json": {"application/json"},
        }.get(adapter_key.casefold(), set())
        if content_type and content_type.casefold() not in allowed_types:
            raise SupplierAdapterError("content_type does not match supplier adapter")
        feed_mode = str(config.get("feed_mode", "full")).casefold()
        if feed_mode not in ("full", "delta"):
            raise SupplierAdapterError("feed_mode must be 'full' or 'delta'")
        records = adapter.parse(payload, config)
        imported, matched, errors = 0, 0, []
        for index, record in enumerate(records, start=1):
            try:
                offer = self.products.upsert_supplier_offer(
                    supplier_id,
                    record.supplier_sku,
                    record.raw_data,
                    expected_tenant_id=expected_tenant_id,
                    supplier_product_id=record.supplier_product_id,
                    normalized_data=record.normalized_data,
                    supplier_url=record.supplier_url,
                    title=record.title,
                    description=record.description,
                    cost_price=record.cost_price,
                    currency=record.currency,
                    stock_quantity=record.stock_quantity,
                    stock_status=record.stock_status,
                    # A record present in the feed is offered again unless the
                    # supplier explicitly marks it inactive.
                    active=True if record.active is None else record.active,
                    rrp=record.rrp,
                    rrp_currency=record.rrp_currency,
                    rrp_includes_vat=record.rrp_includes_vat,
                )
                imported += 1
                if (record.normalized_data.get("gtin")
                        or record.normalized_data.get("ean")):
                    match = self.products.match_offer_by_gtin(
                        offer["id"], expected_tenant_id=expected_tenant_id
                    )
                    if match:
                        matched += 1
            except (ProductMasterError, SupplierAdapterError) as exc:
                errors.append({"record": index, "supplier_sku": record.supplier_sku, "error": str(exc)})
        delisting = self._deactivate_missing_offers(
            supplier_id, {r.supplier_sku for r in records}, feed_mode,
            errors=len(errors), max_pct=config.get("max_delist_pct", 50),
        )
        return {
            "supplier_id": supplier_id,
            "adapter": adapter_key,
            "feed_mode": feed_mode,
            "records_seen": len(records),
            "offers_imported": imported,
            "gtin_matches": matched,
            "offers_deactivated": delisting["deactivated"],
            "delisting_skipped": delisting["skipped"],
            "errors": errors,
        }

    def _deactivate_missing_offers(
        self, supplier_id: str, seen_skus: set, feed_mode: str, *,
        errors: int, max_pct: Any,
    ) -> Dict[str, Any]:
        """In a full feed, offers absent from the payload are no longer offered.

        Guard rails: never delist on a delta feed, an empty feed, a feed with
        row errors, or when more than ``max_delist_pct`` of active offers would
        disappear at once (typically a truncated or broken feed).
        """
        if feed_mode != "full":
            return {"deactivated": 0, "skipped": None}
        if not seen_skus:
            return {"deactivated": 0, "skipped": "empty feed"}
        if errors:
            return {"deactivated": 0, "skipped": "feed contained row errors"}
        try:
            limit = Decimal(str(max_pct))
        except InvalidOperation as exc:
            raise SupplierAdapterError("max_delist_pct must be a number") from exc
        with self.persistence.session() as session:
            active = session.scalars(select(SupplierOffer).where(
                SupplierOffer.supplier_id == supplier_id,
                SupplierOffer.active.is_(True),
            )).all()
            missing = [o for o in active if o.supplier_sku not in seen_skus]
            if not missing:
                return {"deactivated": 0, "skipped": None}
            share = Decimal(len(missing)) * 100 / Decimal(len(active))
            if share > limit:
                return {
                    "deactivated": 0,
                    "skipped": f"{len(missing)} of {len(active)} offers missing "
                               f"exceeds max_delist_pct={limit}",
                }
            now = datetime.now(timezone.utc)
            for offer in missing:
                offer.active = False
                offer.stock_status = "delisted"
                offer.updated_at = now
            session.flush()
            enqueue_in_session(
                session, missing[0].tenant_id,
                [o.product_id for o in missing if o.product_id], reason="delisted",
            )
            return {"deactivated": len(missing), "skipped": None}
