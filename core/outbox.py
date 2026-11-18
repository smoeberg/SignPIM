"""Outbox for channel synchronisation (ADR-018).

Any change that affects what a shop should show (product data, media,
approved price, supplier stock, orders) enqueues one SyncJob per connected
channel and product. Jobs coalesce: at most one pending job per pair.
"""
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from sqlalchemy import select

from core.models import Channel, SyncJob


def enqueue_in_session(session, tenant_id: str, product_ids: Iterable[str], *,
                       reason: str, channel_id: Optional[str] = None,
                       force: bool = False) -> List[str]:
    """Enqueue inside an existing session. Returns ids of pending jobs."""
    query = select(Channel).where(
        Channel.tenant_id == tenant_id,
        Channel.status == "active",
        Channel.base_url.is_not(None),
    )
    if channel_id:
        query = query.where(Channel.id == channel_id)
    channels = session.scalars(query).all()
    job_ids = []
    now = datetime.now(timezone.utc)
    for product_id in {p for p in product_ids if p}:
        for channel in channels:
            job = session.scalar(select(SyncJob).where(
                SyncJob.channel_id == channel.id,
                SyncJob.product_id == product_id,
                SyncJob.status == "pending",
            ))
            if job is None:
                job = SyncJob(tenant_id=tenant_id, channel_id=channel.id,
                              product_id=product_id, reason=reason, force=force,
                              next_attempt_at=now)
                session.add(job)
            else:
                job.force = job.force or force
                job.next_attempt_at = min(_aware(job.next_attempt_at), now)
            session.flush()
            job_ids.append(job.id)
    return job_ids


def enqueue(persistence, tenant_id: str, product_ids: Iterable[str], *, reason: str,
            channel_id: Optional[str] = None, force: bool = False) -> List[str]:
    with persistence.session() as session:
        return enqueue_in_session(session, tenant_id, product_ids, reason=reason,
                                  channel_id=channel_id, force=force)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
