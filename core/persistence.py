"""
SQLAlchemy-backed persistence for products, rules and quality scores.
Uses the same session factory everywhere; tests use SQLite, prod uses PostgreSQL.
"""
import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, select, update, delete
from sqlalchemy.orm import Session, sessionmaker

from core.models import Base, Tenant, Product, Rule, NormalizationMapping

logger = logging.getLogger("signpim.persistence")


class PersistenceService:
    def __init__(self, dsn: str = "sqlite:///:memory:"):
        kwargs = {}
        if dsn.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool          # one shared in-memory DB
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(dsn, future=True, **kwargs)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self):
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ---------- tenants ----------
    def get_or_create_tenant(self, slug: str, settings: Optional[Dict] = None) -> Tenant:
        with self.session() as s:
            t = s.scalar(select(Tenant).where(Tenant.slug == slug))
            if t is None:
                t = Tenant(slug=slug, settings=settings or {})
                s.add(t)
                s.flush()
            return t

    # ---------- products ----------
    def upsert_product(self, tenant_id: str, sku: str, data: Dict[str, Any],
                       quality_score: Optional[float] = None) -> Dict[str, Any]:
        with self.session() as s:
            p = s.scalar(
                select(Product).where(Product.tenant_id == tenant_id, Product.sku == sku)
            )
            if p is None:
                p = Product(tenant_id=tenant_id, sku=sku, data=data,
                            quality_score=quality_score)
                s.add(p)
                s.flush()
                inserted = True
            else:
                p.data = data
                if quality_score is not None:
                    p.quality_score = quality_score
                p.version += 1
                inserted = False
            return {
                "sku": sku, "tenant_id": tenant_id,
                "version": p.version, "inserted": inserted,
                "quality_score": float(p.quality_score) if p.quality_score is not None else None,
            }

    def get_product(self, tenant_id: str, sku: str) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            p = s.scalar(select(Product).where(Product.tenant_id == tenant_id, Product.sku == sku))
            if p is None:
                return None
            return {
                "id": p.id, "sku": p.sku, "tenant_id": p.tenant_id,
                "data": p.data, "quality_score": float(p.quality_score) if p.quality_score is not None else None,
                "version": p.version, "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }

    def list_products(self, tenant_id: str, min_score: Optional[float] = None,
                      limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        with self.session() as s:
            q = select(Product).where(Product.tenant_id == tenant_id)
            if min_score is not None:
                q = q.where(Product.quality_score >= min_score)
            q = q.order_by(Product.sku).limit(limit).offset(offset)
            return [
                {
                    "sku": p.sku, "data": p.data,
                    "quality_score": float(p.quality_score) if p.quality_score is not None else None,
                    "version": p.version,
                }
                for p in s.scalars(q)
            ]

    def save_quality_score(self, tenant_id: str, sku: str, score: float) -> Dict[str, Any]:
        """Persist a computed per-product quality score (used by QualityScoringService)."""
        with self.session() as s:
            p = s.scalar(select(Product).where(Product.tenant_id == tenant_id, Product.sku == sku))
            if p is None:
                p = Product(tenant_id=tenant_id, sku=sku, data={}, quality_score=score)
                s.add(p)
            else:
                p.quality_score = score
            return {"sku": sku, "tenant_id": tenant_id, "quality_score": float(score)}

    # ---------- rules ----------
    def add_rule(self, tenant_id: Optional[str], rule_id: str, category: str,
                 severity: str = "info", parameters: Optional[Dict] = None,
                 active: bool = True) -> Dict[str, Any]:
        with self.session() as s:
            r = Rule(tenant_id=tenant_id, rule_id=rule_id, category=category,
                     severity=severity, parameters=parameters or {}, active=active)
            s.add(r)
            s.flush()
            return {"id": r.id, "rule_id": r.rule_id, "category": r.category}

    def active_rules(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Tenant rules + global rules (tenant_id IS NULL), active only."""
        with self.session() as s:
            q = select(Rule).where(
                Rule.active == True,  # noqa: E712
                (Rule.tenant_id == tenant_id) | (Rule.tenant_id.is_(None)),
            )
            return [
                {"rule_id": r.rule_id, "category": r.category,
                 "severity": r.severity, "parameters": r.parameters}
                for r in s.scalars(q)
            ]

    # ---------- normalization ----------
    def add_mapping(self, tenant_id: str, source_value: str, normalized: str, field: str) -> Dict:
        with self.session() as s:
            m = NormalizationMapping(tenant_id=tenant_id, source_value=source_value,
                                     normalized=normalized, field=field)
            s.add(m)
            s.flush()
            return {"id": m.id, "source_value": m.source_value, "normalized": m.normalized}

    def apply_mappings(self, tenant_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes field values through tenant mappings (e.g. supplier 'BLUE' → 'Blå')."""
        with self.session() as s:
            maps = s.scalars(
                select(NormalizationMapping).where(NormalizationMapping.tenant_id == tenant_id)
            ).all()
        out = dict(data)
        for m in maps:
            key = m.field
            if key in out and isinstance(out[key], str) and out[key].strip() == m.source_value:
                out[key] = m.normalized
        return out
