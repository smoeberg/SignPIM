"""SaaS identity and authorization primitives.

This module deliberately uses new tables alongside the legacy tenant-bound users.
It enables a safe, incremental migration without changing existing customer logins.
"""
import hashlib
import secrets
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, select

from core._orm import _now, _uuid
from core.auth import ROLE_SCOPES, hash_password, verify_password
from core.models import Base, Tenant


PLATFORM_ROLES = {"platform_owner", "platform_admin", "support", "operations"}


class Account(Base):
    __tablename__ = "accounts"
    id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(128), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    id = Column(String(36), primary_key=True, default=_uuid)
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=False)
    organization_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    role = Column(String(32), nullable=False, default="reader")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        Index("uq_membership_account_org", "account_id", "organization_id", unique=True),
        Index("idx_membership_org", "organization_id"),
    )


class PlatformGrant(Base):
    __tablename__ = "platform_grants"
    id = Column(String(36), primary_key=True, default=_uuid)
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=False)
    role = Column(String(32), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (Index("uq_platform_account_role", "account_id", "role", unique=True),)


class SaaSSession(Base):
    __tablename__ = "saas_sessions"
    id = Column(String(36), primary_key=True, default=_uuid)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=False)
    active_organization_id = Column(String(36), ForeignKey("tenants.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(String(36), primary_key=True, default=_uuid)
    actor_account_id = Column(String(36), ForeignKey("accounts.id"), nullable=True)
    organization_id = Column(String(36), ForeignKey("tenants.id"), nullable=True)
    action = Column(String(128), nullable=False)
    target = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (Index("idx_audit_org_time", "organization_id", "created_at"),)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SaaSAuthService:
    def __init__(self, persistence):
        self.persistence = persistence

    def create_account(self, email: str, password: str, display_name: str = None) -> Account:
        email = email.lower().strip()
        if len(password or "") < 8:
            raise ValueError("Password must be at least 8 characters")
        with self.persistence.session() as s:
            account = s.scalar(select(Account).where(Account.email == email))
            if account:
                if not verify_password(password, account.password_hash):
                    raise ValueError("Account already exists; password does not match")
                return account
            account = Account(email=email, password_hash=hash_password(password),
                              display_name=display_name or email.split("@")[0])
            s.add(account); s.flush()
            return account

    def add_membership(self, account_id: str, organization_id: str,
                       role: str = "reader") -> Dict[str, Any]:
        if role not in ROLE_SCOPES:
            raise ValueError(f"Unknown organization role '{role}'")
        with self.persistence.session() as s:
            row = s.scalar(select(OrganizationMembership).where(
                OrganizationMembership.account_id == account_id,
                OrganizationMembership.organization_id == organization_id))
            if row:
                row.role = role; row.active = True
            else:
                row = OrganizationMembership(account_id=account_id,
                    organization_id=organization_id, role=role)
                s.add(row); s.flush()
            return {"id": row.id, "account_id": account_id,
                    "organization_id": organization_id, "role": role}

    def grant_platform_role(self, account_id: str, role: str) -> Dict[str, Any]:
        if role not in PLATFORM_ROLES:
            raise ValueError(f"Unknown platform role '{role}'")
        with self.persistence.session() as s:
            row = s.scalar(select(PlatformGrant).where(
                PlatformGrant.account_id == account_id, PlatformGrant.role == role))
            if not row:
                row = PlatformGrant(account_id=account_id, role=role)
                s.add(row); s.flush()
            else:
                row.active = True
            return {"account_id": account_id, "role": role}

    def login(self, email: str, password: str, organization_slug: str = None) -> Dict[str, Any]:
        with self.persistence.session() as s:
            account = s.scalar(select(Account).where(Account.email == email.lower().strip()))
            if not account or not account.active or not verify_password(password, account.password_hash):
                raise PermissionError("Invalid credentials")
            memberships = s.execute(select(OrganizationMembership, Tenant).join(
                Tenant, Tenant.id == OrganizationMembership.organization_id).where(
                OrganizationMembership.account_id == account.id,
                OrganizationMembership.active == True)).all()  # noqa: E712
            selected = None
            if organization_slug:
                selected = next((m for m, o in memberships if o.slug == organization_slug), None)
                if not selected:
                    raise PermissionError("Account is not a member of this organization")
            elif len(memberships) == 1:
                selected = memberships[0][0]
            grants = s.scalars(select(PlatformGrant).where(
                PlatformGrant.account_id == account.id, PlatformGrant.active == True)).all()  # noqa: E712
            if not selected and not grants and memberships:
                return {"organization_required": True,
                        "organizations": [{"slug": o.slug, "role": m.role} for m, o in memberships]}
            if not selected and not grants:
                raise PermissionError("Account has no active access")
            token = "spimsaas_" + secrets.token_urlsafe(32)
            session = SaaSSession(token_hash=_hash_token(token), account_id=account.id,
                active_organization_id=selected.organization_id if selected else None,
                expires_at=_now() + timedelta(hours=72))
            s.add(session)
            return {"token": token, "account": {"id": account.id, "email": account.email},
                    "organization_id": session.active_organization_id,
                    "organization_role": selected.role if selected else None,
                    "platform_roles": [g.role for g in grants]}

    def authenticate(self, token: str) -> Dict[str, Any]:
        if not token.startswith("spimsaas_"):
            raise PermissionError("Malformed SaaS session token")
        with self.persistence.session() as s:
            session = s.scalar(select(SaaSSession).where(SaaSSession.token_hash == _hash_token(token)))
            if not session or session.expires_at.replace(tzinfo=session.expires_at.tzinfo or _now().tzinfo) < _now():
                raise PermissionError("Session expired or invalid")
            account = s.get(Account, session.account_id)
            if not account or not account.active:
                raise PermissionError("Account is inactive")
            membership = None
            if session.active_organization_id:
                membership = s.scalar(select(OrganizationMembership).where(
                    OrganizationMembership.account_id == account.id,
                    OrganizationMembership.organization_id == session.active_organization_id,
                    OrganizationMembership.active == True))  # noqa: E712
                if not membership:
                    raise PermissionError("Organization access revoked")
            grants = s.scalars(select(PlatformGrant).where(
                PlatformGrant.account_id == account.id, PlatformGrant.active == True)).all()  # noqa: E712
            return {"account_id": account.id, "email": account.email,
                    "tenant_id": session.active_organization_id,
                    "organization_id": session.active_organization_id,
                    "role": membership.role if membership else None,
                    "scopes": ROLE_SCOPES[membership.role] if membership else [],
                    "platform_roles": [g.role for g in grants]}

    def audit(self, actor_id: str, action: str, organization_id: str = None,
              target: str = None) -> None:
        with self.persistence.session() as s:
            s.add(AuditEvent(actor_account_id=actor_id, organization_id=organization_id,
                             action=action, target=target))
