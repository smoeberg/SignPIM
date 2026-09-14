"""
Quality-drop webhooks: notify subscribers when a tenant's quality score drops.

- WebhookSubscription: per-tenant URL + min_score threshold + secret (HMAC signing).
- Triggered after ingest: if the new tenant score < subscription.min_score, fire POST.
- Delivery: HMAC-SHA256 signature header (X-Signature), retries with backoff,
  delivery log persisted.
"""
import hashlib
import hmac as hmac_mod
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.request import Request, urlopen

from sqlalchemy import Column, String, DateTime, Integer, Boolean
from core.models import Base
from core._orm import _uuid, _now

logger = logging.getLogger("signpim.webhooks")

RETRY_DELAYS = [1, 5, 30]  # seconds; overridden in tests for speed


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), nullable=False)
    url = Column(String(1024), nullable=False)
    secret = Column(String(128), nullable=False)
    min_score = Column(Integer, nullable=False, default=75)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id = Column(String(36), primary_key=True, default=_uuid)
    subscription_id = Column(String(36), nullable=False)
    tenant_id = Column(String(36), nullable=False)
    event_type = Column(String(64), nullable=False)
    payload = Column(String(4096), nullable=False)
    signature = Column(String(128), nullable=False)
    status_code = Column(Integer, nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(String(512), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


def sign_payload(secret: str, body: str) -> str:
    return "sha256=" + hmac_mod.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


class WebhookService:
    def __init__(self, persistence):
        self.persistence = persistence
        self._poster = None  # injectable for tests
        self.retry_delays = RETRY_DELAYS

    def subscribe(self, tenant_id: str, url: str, min_score: int = 75,
                  secret: str = None) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            raise ValueError("URL must be http(s)")
        if not 0 <= min_score <= 100:
            raise ValueError("min_score must be 0-100")
        secret = secret or ("whsec_" + hashlib.sha256(
            f"{tenant_id}{url}{time.time()}".encode()).hexdigest()[:32])
        with self.persistence.session() as s:
            sub = WebhookSubscription(tenant_id=tenant_id, url=url,
                                      min_score=min_score, secret=secret)
            s.add(sub)
            s.flush()
            return {"id": sub.id, "url": url, "min_score": min_score,
                    "secret": secret,
                    "warning": "Store this secret now — used to verify X-Signature."}

    def list_subscriptions(self, tenant_id: str) -> List[Dict[str, Any]]:
        from sqlalchemy import select
        with self.persistence.session() as s:
            subs = s.scalars(select(WebhookSubscription).where(
                WebhookSubscription.tenant_id == tenant_id)).all()
            return [{"id": w.id, "url": w.url, "min_score": w.min_score,
                     "active": w.active,
                     "created_at": w.created_at.isoformat()} for w in subs]

    def unsubscribe(self, tenant_id: str, sub_id: str) -> bool:
        with self.persistence.session() as s:
            sub = s.get(WebhookSubscription, sub_id)
            if sub is None or sub.tenant_id != tenant_id:
                return False
            sub.active = False
            return True

    def check_and_notify(self, tenant_slug: str, tenant_id: str,
                         summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fire webhooks for subscriptions whose threshold is now breached."""
        from sqlalchemy import select
        score = summary.get("quality_score")
        if score is None:
            return []
        results = []
        with self.persistence.session() as s:
            subs = s.scalars(select(WebhookSubscription).where(
                WebhookSubscription.tenant_id == tenant_id,
                WebhookSubscription.active == True)).all()  # noqa: E712
            for sub in subs:
                if score < sub.min_score:
                    payload = {
                        "event": "quality.drop",
                        "tenant": tenant_slug,
                        "quality_score": score,
                        "threshold": sub.min_score,
                        "by_dimension": summary.get("by_dimension", {}),
                        "triggered_at": datetime.now(timezone.utc).isoformat(),
                    }
                    body = json.dumps(payload, ensure_ascii=False)
                    sig = sign_payload(sub.secret, body)
                    outcome = self._deliver(sub, "quality.drop", body, sig)
                    results.append({"subscription_id": sub.id, "url": sub.url, **outcome})
        return results

    def _deliver(self, sub, event_type: str, body: str, signature: str) -> Dict[str, Any]:
        attempts = 0
        last_error = None
        status_code = None
        for i, delay in enumerate([0] + self.retry_delays):
            if delay:
                time.sleep(delay)
            attempts += 1
            try:
                poster = self._poster or _http_post
                status_code = poster(sub.url, body, signature)
                last_error = None
                break
            except Exception as e:  # noqa: BLE001
                last_error = str(e)[:500]
                logger.warning("Webhook attempt %d to %s failed: %s", attempts, sub.url, e)
        success = last_error is None
        self._log(sub, event_type, body, signature, status_code, success, attempts, last_error)
        return {"success": success, "attempts": attempts,
                "status_code": status_code, "error": last_error}

    def _log(self, sub, event_type, body, signature, status_code, success, attempts, error):
        with self.persistence.session() as s:
            s.add(WebhookDelivery(
                subscription_id=sub.id, tenant_id=sub.tenant_id,
                event_type=event_type, payload=body[:4000], signature=signature,
                status_code=status_code, success=success,
                attempts=attempts, error=error,
                delivered_at=_now() if success else None))

    def deliveries(self, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        from sqlalchemy import select
        with self.persistence.session() as s:
            rows = s.scalars(select(WebhookDelivery).where(
                WebhookDelivery.tenant_id == tenant_id).order_by(
                WebhookDelivery.created_at.desc()).limit(limit)).all()
            return [{"id": d.id, "event": d.event_type, "success": d.success,
                     "attempts": d.attempts, "status_code": d.status_code,
                     "error": d.error,
                     "created_at": d.created_at.isoformat()} for d in rows]


def _http_post(url: str, body: str, signature: str) -> int:
    req = Request(url, data=body.encode(), method="POST", headers={
        "Content-Type": "application/json",
        "X-Signature": signature,
        "User-Agent": "SignPIM-Webhook/1.0",
    })
    with urlopen(req, timeout=10) as resp:
        return resp.status
