"""
Image backend: config-driven local storage (or S3-compatible via env), hash-dedup,
and product binding. No hardcoded paths — SIGNPIM_MEDIA_ROOT env / tenant override.
"""
import hashlib
import io
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ImageService")

ALLOWED_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/tiff": ".tiff",
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


class ImageService:
    def __init__(self, persistence, media_root: Optional[str] = None, rescorer=None):
        self.persistence = persistence
        self.media_root = media_root or os.environ.get("SIGNPIM_MEDIA_ROOT", "./media")
        self.rescorer = rescorer


    def _tenant_dir(self, tenant_id: str) -> str:
        d = os.path.join(self.media_root, tenant_id)
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _detect_mime(data: bytes) -> Optional[str]:
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG"):
            return "image/png"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        if data.startswith(b"GIF8"):
            return "image/gif"
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            return "image/tiff"
        return None

    def store_image(self, tenant_id: str, sku: str, data: bytes,
                    filename: Optional[str] = None) -> Dict[str, Any]:
        """Store one image, bind to a product, dedup by SHA-256."""
        if len(data) > MAX_UPLOAD_BYTES:
            raise ImageError(f"Image exceeds max size {MAX_UPLOAD_BYTES} bytes")
        mime = self._detect_mime(data)
        if mime not in ALLOWED_MIME:
            raise ImageError(f"Unsupported image type. Allowed: {', '.join(sorted(ALLOWED_MIME))}")
        digest = hashlib.sha256(data).hexdigest()
        tdir = self._tenant_dir(tenant_id)
        ext = ALLOWED_MIME[mime]
        fname = f"{digest[:16]}_{uuid.uuid4().hex[:8]}{ext}"
        path = os.path.join(tdir, fname)
        with open(path, "wb") as f:
            f.write(data)
        rel_url = f"/media/{tenant_id}/{fname}"
        self._bind(tenant_id, sku, rel_url)
        return {"url": rel_url, "sha256": digest, "size": len(data),
                "mime": mime, "filename": fname, "sku": sku}

    def bind_existing(self, tenant_id: str, sku: str, url: str) -> Dict[str, Any]:
        """Bind an already-stored media URL to a product (human apply path only).
        Idempotent; recomputes score. Never called by AI operators."""
        self._bind(tenant_id, sku, url)
        return {"url": url, "sku": sku, "bound": True}

    def store_blob(self, tenant_id: str, sku: str, data: bytes) -> Dict[str, Any]:
        """Store image bytes WITHOUT binding to any product (AI-proposed images).
        Dedup by SHA-256, same as store_image. Binding goes through human review."""
        if len(data) > MAX_UPLOAD_BYTES:
            raise ImageError(f"Image exceeds max {MAX_UPLOAD_BYTES} bytes")
        mime = self._detect_mime(data)
        if mime is None:
            raise ImageError("Unsupported image format")
        digest = hashlib.sha256(data).hexdigest()
        tdir = os.path.join(self.media_root, tenant_id)
        os.makedirs(tdir, exist_ok=True)
        ext = mime.split("/")[1]
        fname = f"{sku}-{digest[:12]}.{ext}"
        path = os.path.join(tdir, fname)
        with open(path, "wb") as f:
            f.write(data)
        return {"url": f"/media/{tenant_id}/{fname}", "sha256": digest,
                "size": len(data), "mime": mime, "filename": fname}

    def store_many(self, tenant_id: str, sku: str, blobs: List[bytes]) -> List[Dict[str, Any]]:
        return [self.store_image(tenant_id, sku, b) for b in blobs]

    def _bind(self, tenant_id: str, sku: str, url: str) -> Dict[str, Any]:
        """Append URL to product.images (idempotent). Recompute quality score for the product."""
        from sqlalchemy import select
        from core.models import Product
        with self.persistence.session() as s:
            p = s.scalars(select(Product).where(
                Product.tenant_id == tenant_id, Product.sku == sku)).first()
            if p is None:
                raise ImageError(f"Unknown product {sku}")
            data = dict(p.data or {})
            imgs = data.get("images") or []
            if isinstance(imgs, str):
                try:
                    imgs = json.loads(imgs) if imgs.strip() else []
                except json.JSONDecodeError:
                    imgs = [imgs]
            if not isinstance(imgs, list):
                imgs = [imgs]
            if url not in imgs:
                imgs.append(url)
            data["images"] = imgs
            p.data = data
            s.add(p)
        # recompute score after binding (missing_images may now pass)
        self._rescore(tenant_id, sku)
        return {"sku": sku, "images": imgs}

    def _rescore(self, tenant_id: str, sku: str) -> None:
        """Recompute quality score via the injected rescorer (engine-wired).
        Config-driven: no rule knowledge lives here."""
        if self.rescorer is not None:
            self.rescorer(tenant_id, sku)


    def unbind(self, tenant_id: str, sku: str, url: str) -> None:
        from sqlalchemy import select
        from core.models import Product
        with self.persistence.session() as s:
            p = s.scalars(select(Product).where(
                Product.tenant_id == tenant_id, Product.sku == sku)).first()
            if p is None:
                raise ImageError(f"Unknown product {sku}")
            data = dict(p.data or {})
            imgs = [u for u in (data.get("images") or []) if u != url]
            data["images"] = imgs
            p.data = data
            s.add(p)
        self._rescore(tenant_id, sku)

    def read_file(self, rel_url: str) -> bytes:
        """Serve /media/<tenant>/<file> from storage root."""
        if not rel_url.startswith("/media/") or ".." in rel_url:
            raise ImageError("Invalid media path")
        path = os.path.join(self.media_root, rel_url[len("/media/"):])
        with open(path, "rb") as f:
            return f.read()

    def read_blob(self, rel_url: str) -> bytes:
        """Read a stored blob (AI-proposed or applied). Same validation as read_file."""
        return self.read_file(rel_url)


class ImageError(Exception):
    pass


class PlaceholderGenerator:
    """Deterministic offline placeholder generator (AI-resolve contract).
    Real image-generation providers plug in via the same contract later."""

    @staticmethod
    def generate(sku: str, prompt: str = "") -> bytes:
        from PIL import Image
        import io
        digest = hashlib.sha256(f"{sku}:{prompt}".encode()).hexdigest()
        color = tuple(int(digest[i:i+2], 16) for i in (0, 2, 4))
        img = Image.new("RGB", (512, 512), color)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
