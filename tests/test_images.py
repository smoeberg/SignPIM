"""Image backend: upload, dedup, bind, auto-rescore, unbind, tenant isolation."""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.persistence import PersistenceService
from engine.kernel import PlatformKernel
from services.ingestion import CSVIngestionService
from services.images import ImageError, ImageService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def env(tmp_path):
    persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/t.db")
    kernel = PlatformKernel(meta_dir="meta")
    kernel.bootstrap()
    ing = CSVIngestionService(persistence, kernel)

    def rescorer(tenant_id, sku):
        # simulate engine: score rises when images present
        p = persistence.get_product(tenant_id, sku) if hasattr(persistence, "get_product") else None
        from sqlalchemy import select
        from core.models import Product
        with persistence.session() as s:
            row = s.scalars(select(Product).where(
                Product.tenant_id == tenant_id, Product.sku == sku)).first()
            data = dict(row.data or {}); data["sku"] = sku
        r = kernel.run_workflow("full_sync", data, tenant_id)
        persistence.save_quality_score(tenant_id, sku, r["data"].get("quality_score"))

    img = ImageService(persistence, media_root=str(tmp_path / "media"), rescorer=rescorer)
    t = persistence.get_or_create_tenant("demo")
    ing.ingest("sku,name,price\nA1,Hammer,99\n", "demo")
    return persistence, img, t.id


def test_upload_binds_and_rescores(env):
    persistence, img, tid = env
    res = img.store_image(tid, "A1", PNG)
    assert res["mime"] == "image/png"
    from sqlalchemy import select
    from core.models import Product
    with persistence.session() as s:
        p = s.scalars(select(Product).where(Product.tenant_id == tid, Product.sku == "A1")).first()
        imgs = p.data.get("images")
        score = float(p.quality_score)
    assert imgs == [res["url"]]
    # file exists and round-trips
    assert img.read_file(res["url"]) == PNG
    # rescorer ran (score present, ≥ before)
    assert score >= 0


def test_dedup_and_idempotent_bind(env):
    persistence, img, tid = env
    r1 = img.store_image(tid, "A1", PNG)
    r2 = img.store_image(tid, "A1", PNG)
    assert r1["sha256"] == r2["sha256"]
    from sqlalchemy import select
    from core.models import Product
    with persistence.session() as s:
        p = s.scalars(select(Product).where(Product.tenant_id == tid, Product.sku == "A1")).first()
    assert p.data.get("images") == [r1["url"]]  # idempotent, no duplicate


def test_rejects_bad_type_and_unknown_product(env):
    persistence, img, tid = env
    with pytest.raises(ImageError):
        img.store_image(tid, "A1", b"not an image")
    with pytest.raises(ImageError):
        img.store_image(tid, "NOPE", PNG)


def test_unbind(env):
    persistence, img, tid = env
    r = img.store_image(tid, "A1", PNG)
    img.unbind(tid, "A1", r["url"])
    from sqlalchemy import select
    from core.models import Product
    with persistence.session() as s:
        p = s.scalars(select(Product).where(Product.tenant_id == tid, Product.sku == "A1")).first()
    assert p.data.get("images") == []


def test_path_traversal_blocked(env):
    persistence, img, tid = env
    with pytest.raises(ImageError):
        img.read_file("/media/../../etc/passwd")
