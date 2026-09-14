"""DB-backed configuration with encrypted secrets.

Resolution chain: tenant DB config -> global DB config -> env var -> default.
Secrets are stored encrypted (Fernet, master key = SIGNPIM_SECRET_KEY) and are
never returned in cleartext via the API (only masked).

Env vars remain a fallback so existing deployments never break, but the DB is
the source of truth going forward.
"""
import base64
import os
from typing import Any, Dict, Optional

from sqlalchemy import Column, String, Text, Boolean, Index, or_

from core.models import Base


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    key = os.environ.get("SIGNPIM_SECRET_KEY", "")
    if not key:
        return None
    if not key.startswith("gAAA"):  # raw passphrase -> derive deterministic key
        key = base64.urlsafe_b64encode(
            (b"signpim-config-" + key.encode().ljust(32, b"-")[:32])[:32])
    return Fernet(key)


class AppConfig(Base):
    """One config entry. tenant_id NULL = global. Secrets encrypted at rest."""
    __tablename__ = "app_config"
    id = Column(String(36), primary_key=True, default=lambda: __import__("uuid").uuid4().hex)
    tenant_id = Column(String(36), nullable=True, index=True)  # NULL = global
    key = Column(String(128), nullable=False)
    value = Column(Text, nullable=False)          # JSON-encoded; ciphertext if secret
    is_secret = Column(Boolean, nullable=False, default=False)
    updated_by = Column(String(255), nullable=True)
    __table_args__ = (Index("idx_app_config", "tenant_id", "key", unique=True),)


class ConfigService:
    # env fallback map: config key -> env var name
    ENV_MAP = {
        "llm.provider": "SIGNPIM_LLM_PROVIDER",
        "llm.api_key_anthropic": "ANTHROPIC_API_KEY",
        "llm.api_key_openai": "OPENAI_API_KEY",
        "llm.ollama_host": "OLLAMA_HOST",
        "llm.model": None,
        "sftp.host": "SIGNPIM_SFTP_HOST",
        "sftp.port": "SIGNPIM_SFTP_PORT",
        "sftp.user": "SIGNPIM_SFTP_USER",
        "sftp.password": "SIGNPIM_SFTP_PASSWORD",
        "sftp.remote_path": "SIGNPIM_SFTP_REMOTE_PATH",
        "sftp.known_hosts": "SIGNPIM_SFTP_KNOWN_HOSTS",
    }
    SECRET_KEYS = {"llm.api_key_anthropic", "llm.api_key_openai", "sftp.password"}

    def __init__(self, persistence):
        self.persistence = persistence

    # ---- write ----
    def set(self, key: str, value: Any, *, tenant_id: Optional[str] = None,
            updated_by: Optional[str] = None) -> Dict[str, Any]:
        if key not in self.ENV_MAP:
            raise ValueError(f"Unknown config key '{key}'. "
                             f"Allowed: {', '.join(sorted(self.ENV_MAP))}")
        is_secret = key in self.SECRET_KEYS
        stored = str(value)
        if is_secret:
            f = _fernet()
            if f is None:
                import cryptography  # noqa: F401 — for a precise error message
                raise RuntimeError(
                    "Cannot store secrets: SIGNPIM_SECRET_KEY not set "
                    "(or the 'cryptography' package is missing)")
            stored = f.encrypt(stored.encode()).decode()
        import json as _json
        payload = _json.dumps({"v": value}) if not is_secret else stored
        from uuid import uuid4
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        with self.persistence.session() as s:
            existing = s.query(AppConfig).filter_by(tenant_id=tenant_id, key=key).first()
            if existing:
                existing.value = payload
                existing.updated_by = updated_by
            else:
                s.add(AppConfig(tenant_id=tenant_id, key=key, value=payload,
                                is_secret=is_secret, updated_by=updated_by))
            s.commit()
        return {"key": key, "tenant_id": tenant_id, "is_secret": is_secret}

    # ---- read (masked) ----
    def list_keys(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Return set keys, secrets masked."""
        out: Dict[str, Any] = {}
        with self.persistence.session() as s:
            if tenant_id:
                flt = or_(AppConfig.tenant_id == tenant_id, AppConfig.tenant_id.is_(None))
            else:
                flt = AppConfig.tenant_id.is_(None)
            rows = s.query(AppConfig).filter(flt).all()
        for r in rows:
            if r.is_secret:
                out[r.key] = "••••••••"
            else:
                import json as _json
                try:
                    out[r.key] = _json.loads(r.value)["v"]
                except Exception:  # noqa: BLE001
                    out[r.key] = r.value
        return out

    def delete(self, key: str, tenant_id: Optional[str] = None) -> bool:
        with self.persistence.session() as s:
            n = s.query(AppConfig).filter_by(tenant_id=tenant_id, key=key).delete()
            s.commit()
        return n > 0

    # ---- resolved view ----
    def effective(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Full resolution chain for every known key: tenant DB > global DB > env."""
        import json as _json
        set_rows: Dict[str, Dict[str, Any]] = {}
        with self.persistence.session() as s:
            if tenant_id:
                flt = or_(AppConfig.tenant_id == tenant_id, AppConfig.tenant_id.is_(None))
            else:
                flt = AppConfig.tenant_id.is_(None)
            rows = (s.query(AppConfig).filter(flt)
                    .order_by(AppConfig.tenant_id.isnot(None).desc())  # tenant wins
                    .all())
        for r in rows:
            if r.key not in set_rows:  # first hit = tenant row beats global row
                set_rows[r.key] = {"value": r.value, "secret": r.is_secret}

        out = {}
        for key in self.ENV_MAP:
            env_var = self.ENV_MAP[key]
            entry: Dict[str, Any] = {"source": "default", "value": None}
            if key in set_rows:
                raw, secret = set_rows[key]["value"], set_rows[key]["secret"]
                if secret:
                    f = _fernet()
                    val = f.decrypt(raw.encode()).decode() if f else None
                    entry = {"source": "db", "value": "••••••••" if val else None}
                else:
                    entry = {"source": "db", "value": _json.loads(raw)["v"]}
            elif env_var and os.environ.get(env_var):
                entry = {"source": "env", "value": "••••••••" if key in self.SECRET_KEYS
                         else os.environ[env_var]}
            out[key] = entry
        return out

    def resolve(self, key: str, tenant_id: Optional[str] = None,
                default: Any = None) -> Any:
        """Programmatic resolution — returns real secret values (internal use only)."""
        import json as _json
        with self.persistence.session() as s:
            q = s.query(AppConfig).filter(AppConfig.key == key)
            if tenant_id:
                q = q.filter(or_(AppConfig.tenant_id == tenant_id,
                                 AppConfig.tenant_id.is_(None)))
            else:
                q = q.filter(AppConfig.tenant_id.is_(None))
            row = q.order_by(AppConfig.tenant_id.isnot(None).desc()).first()
        if row:
            if row.is_secret:
                f = _fernet()
                return f.decrypt(row.value.encode()).decode() if f else None
            return _json.loads(row.value)["v"]
        env_var = self.ENV_MAP.get(key)
        if env_var and os.environ.get(env_var):
            return os.environ[env_var]
        return default
