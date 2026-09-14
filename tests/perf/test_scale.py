"""Scale benchmark: 50k products through ingest → score → export."""
import io
import resource
import time
import csv as csv_mod

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="perf benchmark excluded from CI — runs on demand via make benchmark",
)

from core.persistence import PersistenceService
from services.ingestion import CSVIngestionService
from services.exporters import ExportService
from engine.kernel import PlatformKernel

N = 50_000
INGEST_BUDGET_S = 120
EXPORT_BUDGET_S = 30
MEMORY_BUDGET_MB = 600


def _feed(n):
    buf = io.StringIO()
    buf.write("sku,name,price,ean,images\n")
    for i in range(n):
        ean = f"59012341{100000 + i:06d}"
        buf.write(f"SKU-{i},Produkt {i},{100 + i % 900}.0,{ean},img-{i}.jpg\n")
    return buf.getvalue()


@pytest.fixture(scope="module")
def env():
    p = PersistenceService()
    kernel = PlatformKernel(meta_dir="meta")
    kernel.bootstrap()
    ing = CSVIngestionService(p, kernel)
    return p, ing


def test_scale_50k_products(env):
    p, ing = env
    feed = _feed(N)

    t0 = time.perf_counter()
    result = ing.ingest(feed, "scale")
    t_ingest = time.perf_counter() - t0
    print(f"\n[ingest]   {result['rows_ingested']} rows in {t_ingest:.2f}s "
          f"({N / t_ingest:.0f} rows/s)")
    assert result["rows_ingested"] == N
    assert t_ingest < INGEST_BUDGET_S

    t0 = time.perf_counter()
    out = ExportService(p).export("scale", fmt="json")
    t_export = time.perf_counter() - t0
    print(f"[export]   {out['count']} products JSON in {t_export:.2f}s "
          f"({len(out['content']) / 1024 / 1024:.1f} MB)")
    assert out["count"] == N
    assert t_export < EXPORT_BUDGET_S

    t0 = time.perf_counter()
    out_csv = ExportService(p).export("scale", fmt="csv")
    t_csv = time.perf_counter() - t0
    print(f"[export]   {out_csv['count']} products CSV in {t_csv:.2f}s")
    assert out_csv["count"] == N

    mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"[memory]   peak RSS: {mb:.0f} MB")
    assert mb < MEMORY_BUDGET_MB
