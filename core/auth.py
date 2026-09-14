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


class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    email = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(128), nullable=True)
    role = Column(String(16), nullable=False, default="reader")   # admin | writer | reader
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_users_tenant_email", "tenant_id", "email", unique=True),)


class Session(Base):
    __tablename__ = "sessions"
    id = Column(String(36), primary_key=True, default=_uuid)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    user = relationship("User")


def hash_password(password: str, salt: bytes = None) -> str:
    """PBKDF2-SHA256, 200k iterations. Format: pbkdf2$<salt>$<hash>."""
    import hashlib as _h
    salt = salt or secrets.token_bytes(16)
    dk = _h.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    import hashlib as _h
    try:
        _, salt_hex, hash_hex = stored.split("$")
        dk = _h.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


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


SESSION_TTL_HOURS = 72


class UserService:
    """Multi-user accounts per tenant with password login and sessions."""

    def __init__(self, persistence):
        self.persistence = persistence

    def create_user(self, tenant_id: str, email: str, password: str,
                    role: str = "reader", display_name: str = None) -> Dict[str, Any]:
        if role not in ROLE_SCOPES:
            raise ValueError(f"Unknown role '{role}'. Allowed: {', '.join(ROLE_SCOPES)}")
        if not password or len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        with self.persistence.session() as s:
            from sqlalchemy import select as _sel
            existing = s.scalar(_sel(User).where(User.tenant_id == tenant_id,
                                                 User.email == email.lower()))
            if existing:
                raise ValueError(f"User '{email}' already exists for this tenant")
            u = User(tenant_id=tenant_id, email=email.lower(),
                     password_hash=hash_password(password), role=role,
                     display_name=display_name or email.split("@")[0])
            s.add(u)
            s.flush()
            return {"id": u.id, "email": u.email, "role": u.role,
                    "display_name": u.display_name}

    def list_users(self, tenant_id: str) -> List[Dict[str, Any]]:
        from sqlalchemy import select as _sel
        with self.persistence.session() as s:
            users = s.scalars(_sel(User).where(User.tenant_id == tenant_id)).all()
            return [{"id": u.id, "email": u.email, "role": u.role,
                     "display_name": u.display_name, "active": u.active,
                     "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}
                    for u in users]

    def set_role(self, tenant_id: str, user_id: str, role: str) -> bool:
        if role not in ROLE_SCOPES:
            raise ValueError(f"Unknown role '{role}'")
        with self.persistence.session() as s:
            u = s.get(User, user_id)
            if u is None or u.tenant_id != tenant_id:
                return False
            u.role = role
            return True

    def deactivate(self, tenant_id: str, user_id: str) -> bool:
        with self.persistence.session() as s:
            u = s.get(User, user_id)
            if u is None or u.tenant_id != tenant_id:
                return False
            u.active = False
            return True

    # ---------- sessions ----------
    def login(self, tenant_slug: str, email: str, password: str) -> Dict[str, Any]:
        from sqlalchemy import select as _sel
        from datetime import timedelta
        with self.persistence.session() as s:
            tenant = s.scalar(_sel(Tenant).where(Tenant.slug == tenant_slug))
            if tenant is None:
                raise PermissionError("Unknown tenant")
            user = s.scalar(_sel(User).where(User.tenant_id == tenant.id,
                                             User.email == email.lower().strip()))
            if user is None or not user.active or not verify_password(password, user.password_hash):
                raise PermissionError("Invalid credentials")
            user.last_login_at = _now()
            token = "spimsess_" + secrets.token_urlsafe(32)
            sess = Session(token_hash=hash_key(token), user_id=user.id,
                           expires_at=_now() + timedelta(hours=SESSION_TTL_HOURS))
            s.add(sess)
            return {"token": token, "expires_at": sess.expires_at.isoformat(),
                    "user": {"id": user.id, "email": user.email, "role": user.role,
                             "display_name": user.display_name, "tenant": tenant_slug},
                    "scopes": ROLE_SCOPES[user.role]}

    def authenticate_session(self, token: str) -> Dict[str, Any]:
        """Validate a session token. Returns identity dict (same shape as API-key auth)."""
        from sqlalchemy import select as _sel
        if not token.startswith("spimsess_"):
            raise PermissionError("Malformed session token")
        with self.persistence.session() as s:
            sess = s.scalar(_sel(Session).where(Session.token_hash == hash_key(token)))
            if sess is None:
                raise PermissionError("Session expired or invalid")
            expires = sess.expires_at
            if expires.tzinfo is None:  # SQLite strips tzinfo; assume UTC
                from datetime import timezone as _tz
                expires = expires.replace(tzinfo=_tz.utc)
            if expires < _now():
                raise PermissionError("Session expired or invalid")
            user = s.get(User, sess.user_id)
            if user is None or not user.active:
                raise PermissionError("User is deactivated")
            return {
                "key_id": None,
                "user_id": user.id,
                "email": user.email,
                "tenant_id": user.tenant_id,
                "role": user.role,
                "scopes": ROLE_SCOPES[user.role],
            }

    def logout(self, token: str) -> bool:
        from sqlalchemy import select as _sel, delete as _del
        if not token.startswith("spimsess_"):
            return False
        with self.persistence.session() as s:
            res = s.execute(_del(Session).where(Session.token_hash == hash_key(token)))
            return res.rowcount > 0


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
