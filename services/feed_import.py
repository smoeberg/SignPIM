"""
Supplier feed import: SFTP/FTP polling of large supplier feeds.

- Polls a remote directory, downloads new/changed CSV files (by filename cursor),
  runs each through CSVIngestionService, records import state per file.
- Designed for nightly cron / APScheduler: idempotent, per-tenant cursor.
"""
import logging
import io
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.persistence import PersistenceService
from services.ingestion import CSVIngestionService

logger = logging.getLogger("signpim.feed_import")


from sqlalchemy import Column, String, DateTime, UniqueConstraint
from core.models import Base
from core._orm import _uuid, _now


class FeedImportState(Base):
    __tablename__ = "feed_import_state"
    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), nullable=False)
    file_key = Column(String(512), nullable=False)          # e.g. 'feeds/acme/2026-09-14.csv'
    checksum = Column(String(64), nullable=False)           # sha256 of content
    imported_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    rows_ingested = Column(String(16), nullable=False, default="0")
    errors = Column(String(16), nullable=False, default="0")

    __table_args__ = (UniqueConstraint("tenant_id", "file_key", name="uq_feed_state"),)


class SFTPFeedImporter:
    """Polls an SFTP directory and ingests new/changed CSV feeds per tenant."""

    def __init__(self, persistence: PersistenceService, ingestion: CSVIngestionService):
        self.persistence = persistence
        self.ingestion = ingestion

    # ---------- transport ----------
    def _connect(self, host: str, port: int, username: str, password: str = None,
                 key_path: str = None):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {"hostname": host, "port": port, "username": username,
                  "timeout": 30, "allow_agent": False, "look_for_keys": False}
        if key_path:
            key = paramiko.RSAKey.from_private_key_file(key_path)
            kwargs["pkey"] = key
        else:
            kwargs["password"] = password
        client.connect(**kwargs)
        return client

    def list_remote(self, client, remote_dir: str) -> List[Dict[str, Any]]:
        sftp = client.open_sftp()
        try:
            out = []
            for attr in sftp.listdir_attr(remote_dir):
                if attr.filename.lower().endswith((".csv", ".txt")):
                    out.append({"name": attr.filename, "size": attr.st_size,
                                "mtime": attr.st_mtime,
                                "path": f"{remote_dir.rstrip('/')}/{attr.filename}"})
            return sorted(out, key=lambda x: x["mtime"])
        finally:
            sftp.close()

    def download(self, client, path: str) -> str:
        sftp = client.open_sftp()
        try:
            with sftp.open(path, "r") as f:
                return f.read().decode("utf-8-sig", errors="replace")
        finally:
            sftp.close()

    # ---------- state ----------
    def _seen(self, tenant_id: str, file_key: str) -> Optional[str]:
        from sqlalchemy import select
        with self.persistence.session() as s:
            st = s.scalar(select(FeedImportState).where(
                FeedImportState.tenant_id == tenant_id,
                FeedImportState.file_key == file_key))
            return st.checksum if st else None

    def _mark(self, tenant_id: str, file_key: str, checksum: str,
              rows: int, errors: int):
        with self.persistence.session() as s:
            from sqlalchemy import select
            st = s.scalar(select(FeedImportState).where(
                FeedImportState.tenant_id == tenant_id,
                FeedImportState.file_key == file_key))
            if st:
                st.checksum = checksum
                st.imported_at = _now()
                st.rows_ingested = str(rows)
                st.errors = str(errors)
            else:
                s.add(FeedImportState(
                    tenant_id=tenant_id, file_key=file_key, checksum=checksum,
                    rows_ingested=str(rows), errors=str(errors)))

    # ---------- main poll loop ----------
    def poll(self, tenant_slug: str, host: str, remote_dir: str, username: str,
             password: str = None, key_path: str = None, port: int = 22) -> Dict[str, Any]:
        """Poll once: list → download new/changed → ingest → mark state."""
        client = self._connect(host, port, username, password, key_path)
        results = []
        try:
            t = self.persistence.get_or_create_tenant(tenant_slug)
            tenant_id = t.id
            for f in self.list_remote(client, remote_dir):
                content = self.download(client, f["path"])
                checksum = hashlib.sha256(content.encode()).hexdigest()
                if self._seen(tenant_id, f["path"]) == checksum:
                    logger.info("Skipping unchanged: %s", f["path"])
                    results.append({"file": f["name"], "skipped": True})
                    continue
                r = self.ingestion.ingest(content, tenant_slug)
                self._mark(tenant_id, f["path"], checksum,
                           r["rows_ingested"], len(r["errors"]))
                results.append({
                    "file": f["name"], "skipped": False,
                    "rows_ingested": r["rows_ingested"], "errors": len(r["errors"]),
                    "summary": r["summary"],
                })
        finally:
            client.close()
        return {"tenant": tenant_slug, "files": results,
                "polled_at": datetime.now(timezone.utc).isoformat()}


class LocalFeedImporter(SFTPFeedImporter):
    """Same logic against the local filesystem — for tests and on-prem runs."""

    def poll_local(self, tenant_slug: str, directory: str) -> Dict[str, Any]:
        import os
        t = self.persistence.get_or_create_tenant(tenant_slug)
        results = []
        for name in sorted(os.listdir(directory)):
            if not name.lower().endswith((".csv", ".txt")):
                continue
            path = os.path.join(directory, name)
            with open(path, encoding="utf-8-sig") as fh:
                content = fh.read()
            checksum = hashlib.sha256(content.encode()).hexdigest()
            file_key = f"local://{path}"
            if self._seen(t.id, file_key) == checksum:
                results.append({"file": name, "skipped": True})
                continue
            try:
                r = self.ingestion.ingest(content, tenant_slug)
                self._mark(t.id, file_key, checksum, r["rows_ingested"], len(r["errors"]))
                results.append({"file": name, "skipped": False,
                                "rows_ingested": r["rows_ingested"],
                                "errors": len(r["errors"]), "summary": r["summary"]})
            except Exception as e:
                logger.warning("Feed file %s failed: %s", name, e)
                results.append({"file": name, "skipped": False, "error": str(e)})
        return {"tenant": tenant_slug, "files": results,
                "polled_at": datetime.now(timezone.utc).isoformat()}
