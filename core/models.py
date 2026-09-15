"""
SQLAlchemy ORM models — the physical realization of the Del-1 DDL:
tenants, products (JSONB data + quality_score), rules, normalization_mappings.

SQLite-compatible for tests (JSONB -> JSON via variant), PostgreSQL in production.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Text, Integer, Boolean, DateTime, Numeric,
    ForeignKey, UniqueConstraint, Index, JSON,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


from core._orm import _uuid, _now  # noqa: F401


class AppConfig(Base):
    """One config entry. tenant_id NULL = global. Secrets encrypted at rest."""
    __tablename__ = "app_config"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), nullable=True, index=True)  # NULL = global
    key = Column(String(128), nullable=False)
    value = Column(Text, nullable=False)          # JSON-encoded; ciphertext if secret
    is_secret = Column(Boolean, nullable=False, default=False)
    updated_by = Column(String(255), nullable=True)
    __table_args__ = (Index("idx_app_config", "tenant_id", "key", unique=True),)



class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(String(36), primary_key=True, default=_uuid)
    slug = Column(String(64), unique=True, nullable=False)
    settings = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    products = relationship("Product", back_populates="tenant", cascade="all, delete-orphan")
    rules = relationship("Rule", back_populates="tenant", cascade="all, delete-orphan")
    normalization_mappings = relationship(
        "NormalizationMapping", back_populates="tenant", cascade="all, delete-orphan"
    )


class Product(Base):
    __tablename__ = "products"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    sku = Column(String(128), nullable=False)
    data = Column(JSON, nullable=False, default=dict)          # felter fra meta/entities/*.yaml
    quality_score = Column(Numeric(5, 2))                       # beregnes af engine, aldrig NULL efter gem
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "sku", name="uq_products_tenant_sku"),
        Index("idx_products_tenant", "tenant_id"),
        Index("idx_products_quality", "tenant_id", "quality_score"),
    )

    tenant = relationship("Tenant", back_populates="products")


class Rule(Base):
    __tablename__ = "rules"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=True)  # NULL = global
    rule_id = Column(String(128), nullable=False)                            # matcher meta/rules/*.yaml id
    category = Column(String(32), nullable=False)     # completeness | consistency | accuracy
    severity = Column(String(16), nullable=False, default="info")
    parameters = Column(JSON, nullable=False, default=dict)
    active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("idx_rules_lookup", "tenant_id", "rule_id"),)

    tenant = relationship("Tenant", back_populates="rules")


class NormalizationMapping(Base):
    __tablename__ = "normalization_mappings"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    source_value = Column(String(255), nullable=False)   # f.eks. leverandørens "BLUE"
    normalized = Column(String(255), nullable=False)     # f.eks. "Blå"
    field = Column(String(128), nullable=False)          # f.eks. "color"

    tenant = relationship("Tenant", back_populates="normalization_mappings")


class LLMCall(Base):
    """Cost/audit log for AI operator calls. Prompt & response stored as hashes only (PII-safe)."""
    __tablename__ = "llm_calls"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    operator = Column(String(64), nullable=False)
    provider = Column(String(32), nullable=False)
    model = Column(String(128), nullable=False, default="n/a")
    prompt_hash = Column(String(64), nullable=False)
    response_hash = Column(String(64), nullable=True)
    tokens_in = Column(Integer, nullable=False, default=0)
    tokens_out = Column(Integer, nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)
    success = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("idx_llm_calls_tenant", "tenant_id", "created_at"),)
