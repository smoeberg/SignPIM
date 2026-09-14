"""
API-key authentication for SignPIM.

- Keys are stored SHA-256 hashed (never plaintext in DB).
- Each key belongs to exactly one tenant and carries a role.
- Roles: admin | writer | reader  (cumulative permissions).
"""
import hashlib
import secrets
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import String, Column, Boolean, DateTime, ForeignKey, select, Index
from sqlalchemy.orm import relationship

from core.models import Base, Tenant
from core._orm import _uuid, _now  # shared column helpers

logger = logging.getLogger("signpim.auth")

ROLE_SCOPES: Dict[str, List[str]] = {
    "reader": ["products:read", "quality:read"],
    "writer": ["products:read", "products:write", "quality:read", "ingest:write"],
    "admin": ["products:read", "products:write", "quality:read", "ingest:write",
              "rules:write", "mappings:write", "keys:manage"],
}


class ApiKey(Base):
    __tablename__ = "api_keys"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    name = Column(String(128), nullable=False)
    key_prefix = Column(String(8), nullable=False)          # shown in UI (first 8 chars)
    key_hash = Column(String(64), nullable=False, unique=True)  # SHA-256 hex
    role = Column(String(16), nullable=False, default="reader")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_apikeys_hash", "key_hash"),)

    tenant = relationship("Tenant")


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key() -> tuple:
    """Returns (plaintext_key, prefix). Plaintext is shown exactly once at creation."""
    raw = "spim_" + secrets.token_urlsafe(32)
    return raw, raw[:13]


class AuthService:
    def __init__(self, persistence):
        self.persistence = persistence

    def create_key(self, tenant_id: str, name: str, role: str = "reader") -> Dict[str, Any]:
        if role not in ROLE_SCOPES:
            raise ValueError(f"Unknown role '{role}'. Allowed: {', '.join(ROLE_SCOPES)}")
        raw, prefix = generate_api_key()
        with self.persistence.session() as s:
            key = ApiKey(tenant_id=tenant_id, name=name, key_prefix=prefix,
                         key_hash=hash_key(raw), role=role)
            s.add(key)
            s.flush()
            return {
                "id": key.id, "name": name, "role": role,
                "key": raw,               # ← plaintext, shown ONCE
                "prefix": prefix,
                "scopes": ROLE_SCOPES[role],
                "warning": "Store this key now — it cannot be retrieved again.",
            }

    def authenticate(self, header_value: str) -> Dict[str, Any]:
        """Validate 'Bearer spim_...' or raw key. Returns {tenant_id, role, scopes, key_id}."""
        if not header_value:
            raise PermissionError("Missing Authorization header")
        token = header_value
        if token.startswith("Bearer "):
            token = token[7:]
        if not token.startswith("spim_"):
            raise PermissionError("Malformed API key")
        key_hash = hash_key(token)
        with self.persistence.session() as s:
            key = s.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.active == True))  # noqa: E712
            if key is None:
                raise PermissionError("Invalid or revoked API key")
            key.last_used_at = _now()
            return {
                "key_id": key.id,
                "tenant_id": key.tenant_id,
                "role": key.role,
                "scopes": ROLE_SCOPES[key.role],
            }

    def revoke_key(self, tenant_id: str, key_id: str) -> bool:
        with self.persistence.session() as s:
            key = s.get(ApiKey, key_id)
            if key is None or key.tenant_id != tenant_id:
                return False
            key.active = False
            return True

    def list_keys(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.persistence.session() as s:
            keys = s.scalars(select(ApiKey).where(ApiKey.tenant_id == tenant_id)).all()
            return [
                {"id": k.id, "name": k.name, "role": k.role, "prefix": k.key_prefix,
                 "active": k.active,
                 "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
                for k in keys
            ]
