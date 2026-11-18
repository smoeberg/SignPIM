"""Channel synchronisation engine (ADR-018).

SignPIM owns product data, media, price and stock. A SyncJob brings one
product up to date in one channel:

    recalculate price (ADR-017) -> product fields -> stock -> local fixed
    prices -> images

Each part is hashed; an unchanged part is not sent again. A job with
``force`` (shop drift) ignores hashes. Failures are retried with backoff and
end as ``dead`` after MAX_ATTEMPTS so they surface for review.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import func, or_, select

from core import secrets
from core.models import (
    Channel, ExternalRef, InboundEvent, PriceCalculation, ProductMaster,
    ProductMedia, StockReservation, SupplierOffer, SyncJob,
)
from core.outbox import enqueue_in_session
from core.pricing import PricingService
from core.product_master import normalize_gtin
from services.channels.prestashop import (
    ChannelClient, ChannelError, PrestaShopWebservice, content_hash,
    product_fields, specific_price_fields,
)

logger = logging.getLogger("ChannelSync")

MAX_ATTEMPTS = 8
CREATE_ONLY_FIELDS = ("link_rewrite", "associations")
EVENT_MAX_SKEW_SECONDS = 300
IN_STOCK = {"in_stock", "instock", "available", "on_stock", "på lager"}
ALLOWED_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_MEDIA_BYTES = 20 * 1024 * 1024
CONFIG_SECRET_TERMS = ("password", "secret", "token", "api_key", "apikey", "ws_key", "private_key")


class ChannelSyncError(ValueError):
    """Invalid request to the channel connector."""


class EventRejected(ChannelSyncError):
    """Inbound event failed authentication or validation."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def default_client_factory(channel: Channel) -> ChannelClient:
    if channel.api_mode != "webservice":
        raise ChannelError(f"api_mode {channel.api_mode} is not supported yet", retryable=False)
    return PrestaShopWebservice(channel.base_url, secrets.resolve(channel.credential_ref))


def _detect_mime(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return None


class ChannelSyncService:
    def __init__(self, persistence, *, client_factory: Optional[Callable[[Channel], ChannelClient]] = None,
                 media_root: Optional[str] = None):
        self.persistence = persistence
        self.client_factory = client_factory or default_client_factory
        self.media_root = media_root or os.environ.get("SIGNPIM_MEDIA_ROOT", "./media")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _channel(self, session, tenant_id: str, channel_id: str) -> Channel:
        channel = session.get(Channel, channel_id)
        if channel is None or channel.tenant_id != tenant_id:
            raise ChannelSyncError("channel not found")
        return channel

    def connect(self, tenant_id: str, channel_id: str, *, base_url: str, credential_ref: str,
                webhook_secret_ref: Optional[str] = None,
                connector_config: Optional[Mapping[str, Any]] = None,
                api_mode: str = "webservice") -> Dict[str, Any]:
        if not base_url.startswith("https://") and not base_url.startswith("http://localhost"):
            raise ChannelSyncError("base_url must use https")
        if api_mode != "webservice":
            raise ChannelSyncError("only api_mode=webservice is supported in this version")
        config = dict(connector_config or {})
        if any(term in str(k).casefold() for k in _walk_keys(config) for term in CONFIG_SECRET_TERMS):
            raise ChannelSyncError("secrets must use credential_ref / webhook_secret_ref, not connector_config")
        try:
            secrets.validate_ref(credential_ref)
            if webhook_secret_ref:
                secrets.validate_ref(webhook_secret_ref)
        except secrets.SecretError as exc:
            raise ChannelSyncError(str(exc))
        languages = config.get("languages", {"da": 1})
        if not isinstance(languages, dict) or not languages:
            raise ChannelSyncError("connector_config.languages must map ISO codes to PrestaShop language ids")
        with self.persistence.session() as session:
            channel = self._channel(session, tenant_id, channel_id)
            channel.base_url = base_url.rstrip("/")
            channel.credential_ref = credential_ref
            channel.webhook_secret_ref = webhook_secret_ref
            channel.connector_config = config
            channel.api_mode = api_mode
            session.flush()
            return {
                "channel_id": channel.id, "base_url": channel.base_url, "api_mode": channel.api_mode,
                "credential_ref": channel.credential_ref, "webhook_secret_ref": channel.webhook_secret_ref,
                "connector_config": channel.connector_config,
            }

    def test_connection(self, tenant_id: str, channel_id: str) -> Dict[str, Any]:
        with self.persistence.session() as session:
            channel = self._channel(session, tenant_id, channel_id)
            if not channel.base_url:
                raise ChannelSyncError("channel is not connected")
            session.expunge(channel)
        try:
            return self.client_factory(channel).ping()
        except (ChannelError, secrets.SecretError) as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Media (SignPIM owns images)
    # ------------------------------------------------------------------
    def add_media(self, tenant_id: str, product_id: str, data: bytes, *,
                  alt_text: Optional[str] = None, position: Optional[int] = None) -> Dict[str, Any]:
        if len(data) > MAX_MEDIA_BYTES:
            raise ChannelSyncError("image exceeds 20 MB")
        mime = _detect_mime(data)
        if mime is None:
            raise ChannelSyncError("unsupported image type; use JPEG, PNG, WebP or GIF")
        digest = hashlib.sha256(data).hexdigest()
        with self.persistence.session() as session:
            product = session.get(ProductMaster, product_id)
            if product is None or product.tenant_id != tenant_id:
                raise ChannelSyncError("product not found")
            existing = session.scalar(select(ProductMedia).where(
                ProductMedia.product_id == product_id, ProductMedia.sha256 == digest
            ))
            if existing is not None:
                return dict(_media_dict(existing), duplicate=True)
            rel = os.path.join(tenant_id, "master", digest[:2], digest + ALLOWED_MIME[mime])
            path = os.path.join(self.media_root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
            if position is None:
                position = (session.scalar(select(func.max(ProductMedia.position)).where(
                    ProductMedia.product_id == product_id)) or -1) + 1
            media = ProductMedia(tenant_id=tenant_id, product_id=product_id, storage_path=rel,
                                 sha256=digest, mime=mime, position=position, alt_text=alt_text)
            session.add(media)
            session.flush()
            enqueue_in_session(session, tenant_id, [product_id], reason="media")
            return dict(_media_dict(media), duplicate=False)

    def list_media(self, tenant_id: str, product_id: str) -> List[Dict[str, Any]]:
        with self.persistence.session() as session:
            rows = session.scalars(select(ProductMedia).where(
                ProductMedia.tenant_id == tenant_id, ProductMedia.product_id == product_id
            ).order_by(ProductMedia.position, ProductMedia.created_at)).all()
            return [_media_dict(m) for m in rows]

    def delete_media(self, tenant_id: str, product_id: str, media_id: str) -> None:
        with self.persistence.session() as session:
            media = session.get(ProductMedia, media_id)
            if media is None or media.tenant_id != tenant_id or media.product_id != product_id:
                raise ChannelSyncError("media not found")
            session.delete(media)
            session.flush()
            enqueue_in_session(session, tenant_id, [product_id], reason="media")

    def reorder_media(self, tenant_id: str, product_id: str, media_ids: List[str]) -> List[Dict[str, Any]]:
        with self.persistence.session() as session:
            rows = {m.id: m for m in session.scalars(select(ProductMedia).where(
                ProductMedia.tenant_id == tenant_id, ProductMedia.product_id == product_id))}
            if set(media_ids) != set(rows):
                raise ChannelSyncError("media_ids must list every image of the product exactly once")
            for position, media_id in enumerate(media_ids):
                rows[media_id].position = position
            session.flush()
            enqueue_in_session(session, tenant_id, [product_id], reason="media")
        return self.list_media(tenant_id, product_id)

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    def available_quantity(self, session, product_id: str, config: Mapping[str, Any]) -> int:
        """Supplier stock minus channel sales the supplier feed has not reflected yet."""
        offers = session.scalars(select(SupplierOffer).where(
            SupplierOffer.product_id == product_id, SupplierOffer.active.is_(True)
        )).all()
        if not offers:
            return 0
        unknown_default = int(config.get("unknown_stock_quantity", 10))
        total = 0
        for offer in offers:
            if offer.stock_quantity is not None:
                total += max(int(Decimal(str(offer.stock_quantity))), 0)
            elif (offer.stock_status or "").strip().casefold() in IN_STOCK:
                total += unknown_default
        last_seen = max(_aware(o.last_seen_at) for o in offers if o.last_seen_at)
        reserved = sum(r.quantity for r in session.scalars(select(StockReservation).where(
            StockReservation.product_id == product_id,
        )).all() if _aware(r.created_at) > last_seen)
        return max(total - reserved, 0)

    # ------------------------------------------------------------------
    # Outbox processing
    # ------------------------------------------------------------------
    def process_due(self, *, tenant_id: Optional[str] = None, channel_id: Optional[str] = None,
                    limit: int = 50) -> Dict[str, int]:
        summary = {"processed": 0, "done": 0, "retry": 0, "dead": 0}
        with self.persistence.session() as session:
            query = select(SyncJob.id).where(
                SyncJob.status == "pending", SyncJob.next_attempt_at <= _now()
            ).order_by(SyncJob.next_attempt_at).limit(limit)
            if tenant_id:
                query = query.where(SyncJob.tenant_id == tenant_id)
            if channel_id:
                query = query.where(SyncJob.channel_id == channel_id)
            job_ids = session.scalars(query.with_for_update(skip_locked=True)).all()
        for job_id in job_ids:
            outcome = self.run_job(job_id)
            summary["processed"] += 1
            summary[outcome] += 1
        return summary

    def run_job(self, job_id: str) -> str:
        with self.persistence.session() as session:
            job = session.get(SyncJob, job_id)
            if job is None or job.status != "pending":
                return "done"
            channel = session.get(Channel, job.channel_id)
            session.expunge(channel)
            product_id, force = job.product_id, job.force
        try:
            client = self.client_factory(channel)
            result = self.sync_product(channel, product_id, client, force=force)
            status, error = "done", None
        except (ChannelError, secrets.SecretError) as exc:
            retryable = getattr(exc, "retryable", False)
            result, error = None, str(exc)
            status = "retry" if retryable else "dead"
        except Exception as exc:  # noqa: BLE001 - a job must never crash the worker
            logger.exception("sync job %s failed", job_id)
            result, error, status = None, f"{type(exc).__name__}: {exc}", "retry"
        with self.persistence.session() as session:
            job = session.get(SyncJob, job_id)
            job.attempts += 1
            job.last_error = error
            job.result = result
            if status == "done":
                job.status = "done"
            elif status == "dead" or job.attempts >= MAX_ATTEMPTS:
                job.status = "dead"
                status = "dead"
            else:
                job.next_attempt_at = _now() + timedelta(minutes=min(2 ** job.attempts, 60))
        return status

    def _ref(self, session, channel_id: str, entity: str, local_id: str) -> Optional[ExternalRef]:
        return session.scalar(select(ExternalRef).where(
            ExternalRef.channel_id == channel_id, ExternalRef.entity_type == entity,
            ExternalRef.local_id == local_id,
        ))

    def _save_ref(self, session, channel: Channel, entity: str, local_id: str,
                  external_id: str, digest: Optional[str]) -> None:
        ref = self._ref(session, channel.id, entity, local_id)
        if ref is None:
            ref = ExternalRef(tenant_id=channel.tenant_id, channel_id=channel.id,
                              entity_type=entity, local_id=local_id, external_id=str(external_id))
            session.add(ref)
        ref.external_id = str(external_id)
        ref.content_hash = digest
        ref.synced_at = _now()
        session.flush()

    def sync_product(self, channel: Channel, product_id: str, client: ChannelClient, *,
                     force: bool = False) -> Dict[str, Any]:
        config = channel.connector_config or {}
        pricing = PricingService(self.persistence)
        calc = pricing.calculate(channel.tenant_id, channel.id, product_id)
        result: Dict[str, Any] = {"price_status": calc["status"], "sent": []}

        with self.persistence.session() as session:
            product = session.get(ProductMaster, product_id)
            approved = session.scalar(
                select(PriceCalculation).where(
                    PriceCalculation.product_id == product_id,
                    PriceCalculation.channel_id == channel.id,
                    PriceCalculation.status == "approved",
                ).order_by(PriceCalculation.created_at.desc()).limit(1)
            )
            has_offer = session.scalar(select(func.count()).select_from(SupplierOffer).where(
                SupplierOffer.product_id == product_id, SupplierOffer.active.is_(True))) > 0
            quantity = self.available_quantity(session, product_id, config)
            product_ref = self._ref(session, channel.id, "product", product_id)
            media = session.scalars(select(ProductMedia).where(
                ProductMedia.product_id == product_id
            ).order_by(ProductMedia.position, ProductMedia.created_at)).all()
            product_data = {
                "master_sku": product.master_sku, "gtin": product.gtin, "mpn": product.mpn,
                "brand": product.brand, "name": product.name, "category": product.category,
                "description": product.description, "attributes": product.attributes or {},
            }
            price = None
            if approved is not None:
                price = {
                    "price_ex_vat": format(Decimal(str(approved.price_ex_vat)), "f"),
                    "cost_eur": format(Decimal(str(approved.cost_eur)), "f") if approved.cost_eur is not None else None,
                    "local_prices": (approved.breakdown or {}).get("local_prices", {}),
                }
            media_list = [(m.id, m.sha256, m.storage_path, m.mime) for m in media]
            product_external = product_ref.external_id if product_ref else None
            product_hash = product_ref.content_hash if product_ref else None

        active = price is not None and has_offer
        if product_external is None and not active:
            result["skipped"] = "no approved price or no active offer; product not created"
            return result

        # 1. Product fields. SEO URL and extra category placement belong to
        # the shop: they are only set when SignPIM creates the product.
        fields = product_fields(product_data, price, config, active=active)
        update_fields = {k: v for k, v in fields.items() if k not in CREATE_ONLY_FIELDS}
        digest = content_hash(update_fields)
        if product_external is None:
            product_external = client.create("products", fields)
            result["sent"].append("product:create")
            self._store(channel, "product", product_id, product_external, digest)
        elif force or digest != product_hash:
            client.update("products", product_external, update_fields)
            result["sent"].append("product:update")
            self._store(channel, "product", product_id, product_external, digest)
        result["external_id"] = product_external

        # 2. Stock
        self._sync_stock(channel, product_id, product_external, quantity, client, force, result)
        # 3. Fixed local prices (specific_price per currency)
        if price is not None:
            self._sync_local_prices(channel, product_id, product_external, price, client, force, result)
        # 4. Images
        self._sync_images(channel, product_id, product_external, media_list, client, force, result)
        result["quantity"] = quantity
        result["active"] = active
        return result

    def _store(self, channel, entity, local_id, external_id, digest):
        with self.persistence.session() as session:
            self._save_ref(session, channel, entity, local_id, external_id, digest)

    def _sync_stock(self, channel, product_id, product_external, quantity, client, force, result):
        with self.persistence.session() as session:
            ref = self._ref(session, channel.id, "stock", product_id)
            ref_ext, ref_hash = (ref.external_id, ref.content_hash) if ref else (None, None)
        digest = content_hash({"product": product_external, "quantity": quantity})
        if not force and digest == ref_hash:
            return
        if ref_ext is None:
            rows = client.find("stock_availables",
                               {"id_product": product_external, "id_product_attribute": 0},
                               ["id", "quantity"])
            if not rows:
                raise ChannelError("stock_available row not found for product", retryable=True)
            ref_ext = str(rows[0]["id"])
        client.update("stock_availables", ref_ext, {"quantity": quantity})
        result["sent"].append("stock")
        self._store(channel, "stock", product_id, ref_ext, digest)

    def _sync_local_prices(self, channel, product_id, product_external, price, client, force, result):
        currency_ids = (channel.connector_config or {}).get("currency_ids") or {}
        for currency, values in (price.get("local_prices") or {}).items():
            if currency == "EUR":
                continue
            if currency not in currency_ids:
                result.setdefault("warnings", []).append(
                    f"no PrestaShop currency id configured for {currency}; fixed price not sent")
                continue
            local_id = f"{product_id}:{currency}"
            fields = specific_price_fields(product_external, currency_ids[currency], values["ex_vat"])
            digest = content_hash(fields)
            with self.persistence.session() as session:
                ref = self._ref(session, channel.id, "specific_price", local_id)
                ref_ext, ref_hash = (ref.external_id, ref.content_hash) if ref else (None, None)
            if ref_ext is None:
                ref_ext = client.create("specific_prices", fields)
                result["sent"].append(f"specific_price:{currency}:create")
            elif force or digest != ref_hash:
                client.update("specific_prices", ref_ext, fields)
                result["sent"].append(f"specific_price:{currency}:update")
            else:
                continue
            self._store(channel, "specific_price", local_id, ref_ext, digest)

    def _sync_images(self, channel, product_id, product_external, media_list, client, force, result):
        """Images follow SignPIM's order (position 0 = cover).

        PrestaShop takes cover and positions from upload order, so removals and
        appended images are applied incrementally, while a reorder re-uploads
        the set. The previously pushed order is the upload order of the refs.
        """
        with self.persistence.session() as session:
            refs = session.scalars(select(ExternalRef).where(
                ExternalRef.channel_id == channel.id, ExternalRef.entity_type == "image",
                ExternalRef.local_id.like(f"{product_id}:%"),
            ).order_by(ExternalRef.synced_at)).all()
            previous = [(r.local_id, r.external_id) for r in refs]
            set_ref = self._ref(session, channel.id, "image_set", product_id)
            set_hash = set_ref.content_hash if set_ref else None
        wanted = [f"{product_id}:{sha}" for _, sha, _, _ in media_list]
        set_digest = content_hash({"product": product_external, "images": wanted})
        if not force and set_digest == set_hash:
            return
        ref_map = dict(previous)
        kept = [key for key, _ in previous if key in wanted]
        incremental = not force and wanted[:len(kept)] == kept
        to_delete = [k for k, _ in previous if k not in wanted] if incremental else [k for k, _ in previous]
        for key in to_delete:
            client.delete_product_image(product_external, ref_map.pop(key))
            self._drop_ref(channel.id, "image", key)
            result["sent"].append("image:delete")
        for _, sha, path, mime in media_list:
            key = f"{product_id}:{sha}"
            if key in ref_map:
                continue
            with open(os.path.join(self.media_root, path), "rb") as fh:
                data = fh.read()
            image_id = client.upload_product_image(product_external, os.path.basename(path), data, mime)
            self._store(channel, "image", key, image_id, sha)
            ref_map[key] = image_id
            result["sent"].append("image:upload")
        self._store(channel, "image_set", product_id, product_external, set_digest)

    def _drop_ref(self, channel_id, entity, local_id):
        with self.persistence.session() as session:
            ref = self._ref(session, channel_id, entity, local_id)
            if ref is not None:
                session.delete(ref)

    # ------------------------------------------------------------------
    # Initial catalogue import: link existing shop products to masters
    # ------------------------------------------------------------------
    def import_catalog(self, tenant_id: str, channel_id: str, *, page_size: int = 100,
                       client: Optional[ChannelClient] = None) -> Dict[str, Any]:
        with self.persistence.session() as session:
            channel = self._channel(session, tenant_id, channel_id)
            if not channel.base_url:
                raise ChannelSyncError("channel is not connected")
            session.expunge(channel)
        client = client or self.client_factory(channel)
        linked, already, unmatched, conflicts = 0, 0, [], []
        offset = 0
        seen_masters: Dict[str, str] = {}
        while True:
            rows = client.list_page("products", ["id", "reference", "ean13", "upc", "name"], offset, page_size)
            if not rows:
                break
            with self.persistence.session() as session:
                for row in rows:
                    ext_id = str(row["id"])
                    master = self._match_shop_product(session, tenant_id, row)
                    if master is None:
                        if len(unmatched) < 500:
                            unmatched.append({"id": ext_id, "reference": row.get("reference"),
                                              "ean13": row.get("ean13"), "name": _plain_name(row.get("name"))})
                        continue
                    if master.id in seen_masters and seen_masters[master.id] != ext_id:
                        conflicts.append({"master_sku": master.master_sku,
                                          "shop_ids": [seen_masters[master.id], ext_id]})
                        continue
                    seen_masters[master.id] = ext_id
                    ref = self._ref(session, channel_id, "product", master.id)
                    if ref is not None:
                        if ref.external_id != ext_id:
                            conflicts.append({"master_sku": master.master_sku,
                                              "shop_ids": [ref.external_id, ext_id]})
                        else:
                            already += 1
                        continue
                    # content_hash=None forces the first sync to send SignPIM's data.
                    self._save_ref(session, channel, "product", master.id, ext_id, None)
                    enqueue_in_session(session, tenant_id, [master.id], reason="catalog_import",
                                       channel_id=channel_id)
                    linked += 1
            if len(rows) < page_size:
                break
            offset += page_size
        return {"linked": linked, "already_linked": already, "unmatched": unmatched,
                "unmatched_count": len(unmatched), "conflicts": conflicts}

    def _match_shop_product(self, session, tenant_id: str, row: Mapping[str, Any]) -> Optional[ProductMaster]:
        reference = (row.get("reference") or "").strip()
        if reference:
            product = session.scalar(select(ProductMaster).where(
                ProductMaster.tenant_id == tenant_id, ProductMaster.master_sku == reference))
            if product is not None:
                return product
        for code in (row.get("ean13"), row.get("upc")):
            gtin14 = normalize_gtin(code) if code else None
            if gtin14:
                product = session.scalar(select(ProductMaster).where(
                    ProductMaster.tenant_id == tenant_id, ProductMaster.gtin14 == gtin14))
                if product is not None:
                    return product
        return None

    # ------------------------------------------------------------------
    # Inbound realtime events from the PrestaShop module
    # ------------------------------------------------------------------
    def handle_event(self, channel_id: str, body: bytes, timestamp: str, signature: str) -> Dict[str, Any]:
        with self.persistence.session() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or not channel.webhook_secret_ref:
                raise EventRejected("unknown channel or events not enabled")
            secret_ref, tenant_id = channel.webhook_secret_ref, channel.tenant_id
        try:
            secret = secrets.resolve(secret_ref)
        except secrets.SecretError as exc:
            raise EventRejected(str(exc))
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            raise EventRejected("invalid timestamp")
        if abs(time.time() - ts) > EVENT_MAX_SKEW_SECONDS:
            raise EventRejected("timestamp outside allowed window")
        expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, (signature or "").removeprefix("sha256=")):
            raise EventRejected("invalid signature")
        try:
            event = json.loads(body)
            event_id, event_type, data = str(event["event_id"]), str(event["type"]), event.get("data") or {}
        except (ValueError, KeyError, TypeError):
            raise EventRejected("body must be JSON with event_id, type and data")

        with self.persistence.session() as session:
            if session.scalar(select(InboundEvent).where(
                InboundEvent.channel_id == channel_id, InboundEvent.event_id == event_id
            )):
                return {"duplicate": True, "event_id": event_id}
            record = InboundEvent(tenant_id=tenant_id, channel_id=channel_id, event_id=event_id,
                                  event_type=event_type, payload=event)
            session.add(record)
            session.flush()
            outcome = self._apply_event(session, tenant_id, channel_id, event_type, data)
            record.outcome = outcome
            return {"duplicate": False, "event_id": event_id, **outcome}

    def _local_product(self, session, channel_id: str, external_product_id: Any) -> Optional[str]:
        ref = session.scalar(select(ExternalRef).where(
            ExternalRef.channel_id == channel_id, ExternalRef.entity_type == "product",
            ExternalRef.external_id == str(external_product_id),
        ))
        return ref.local_id if ref else None

    def _apply_event(self, session, tenant_id, channel_id, event_type, data) -> Dict[str, Any]:
        if event_type in ("order.created", "order.updated"):
            order_id = str(data.get("order_id") or "")
            if not order_id:
                raise EventRejected("order events need data.order_id")
            existing = session.scalars(select(StockReservation).where(
                StockReservation.channel_id == channel_id,
                StockReservation.external_order_id == order_id,
            )).all()
            affected = {r.product_id for r in existing}
            if data.get("cancelled"):
                for r in existing:
                    session.delete(r)
                session.flush()
                enqueue_in_session(session, tenant_id, affected, reason="order_cancelled")
                return {"reservations_released": len(existing), "products": len(affected)}
            created, unknown = 0, []
            by_product = {r.product_id: r for r in existing}
            for line in data.get("lines") or []:
                local = self._local_product(session, channel_id, line.get("product_id"))
                qty = int(line.get("quantity") or 0)
                if local is None or qty <= 0:
                    unknown.append(line.get("product_id"))
                    continue
                if local in by_product:
                    by_product[local].quantity = qty
                else:
                    reservation = StockReservation(tenant_id=tenant_id, channel_id=channel_id,
                                                   product_id=local, external_order_id=order_id,
                                                   quantity=qty)
                    session.add(reservation)
                    by_product[local] = reservation
                    created += 1
                affected.add(local)
            session.flush()
            # Every channel (not only the selling one) gets the lower quantity.
            enqueue_in_session(session, tenant_id, affected, reason="order")
            return {"reservations_created": created, "products": len(affected), "unknown_products": unknown}
        if event_type in ("product.updated", "stock.updated", "product.deleted"):
            local = self._local_product(session, channel_id, data.get("product_id"))
            if local is None:
                return {"ignored": "product not managed by SignPIM"}
            if event_type == "product.deleted":
                for ref in session.scalars(select(ExternalRef).where(
                    ExternalRef.channel_id == channel_id,
                    or_(ExternalRef.local_id == local, ExternalRef.local_id.like(f"{local}:%")),
                )).all():
                    session.delete(ref)
                session.flush()
            # SignPIM owns these fields: restore them (force ignores hashes).
            enqueue_in_session(session, tenant_id, [local], reason=f"shop_drift:{event_type}",
                               channel_id=channel_id, force=True)
            return {"resync": local}
        return {"ignored": f"unsupported event type {event_type}"}


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _walk_keys(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _plain_name(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0].get("value") if isinstance(value[0], dict) else value[0]
    return value


def _media_dict(media: ProductMedia) -> Dict[str, Any]:
    return {
        "id": media.id, "product_id": media.product_id, "sha256": media.sha256,
        "mime": media.mime, "position": media.position, "alt_text": media.alt_text,
        "url": "/media/" + media.storage_path.replace(os.sep, "/"),
    }
