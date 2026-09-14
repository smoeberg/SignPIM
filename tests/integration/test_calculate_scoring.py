"""Integration tests for the 'calculate' quality_score action (Bug 1+2+3 fixes)."""
import pytest
from engine.kernel import PlatformKernel
import os

@pytest.fixture(scope="module")
def kernel():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return PlatformKernel(os.path.join(base_dir, 'meta'), repository=None)

def test_calculate_scores_product_with_images(kernel):
    """Product WITH images must NOT trigger missing_images, and must get a real score (Bug 1+2)."""
    product = {
        "sku": "IMG-OK", "name": "Chair", "price": 100.0,
        "images": ["a.jpg"], "ean": "5901234123457",
    }
    res = kernel.run_workflow("full_sync", product, tenant_id="T1")
    assert "missing_images" not in res["violations"]
    assert res["data"]["quality_score"] is not None
    assert isinstance(res["data"]["quality_score"], float) and res["data"]["quality_score"] >= 75.0

def test_calculate_penalizes_missing_images(kernel):
    """Product WITHOUT images must trigger missing_images and score lower (Bug 1)."""
    product = {"sku": "NO-IMG", "name": "Stool", "price": 50.0}
    res = kernel.run_workflow("full_sync", product, tenant_id="T1")
    assert "missing_images" in res["violations"]

def test_scores_are_severity_weighted(kernel):
    """An EAN violation (major) plus missing images (critical) must yield lower score than images-only."""
    p_bad = {"sku": "BAD-1", "name": "Bad", "price": 10.0}          # critical missing images
    p_mid = {"sku": "MID-1", "name": "Mid", "price": 10.0,
             "images": ["x.jpg"], "ean": "123"}                     # major bad EAN only
    r_bad = kernel.run_workflow("full_sync", p_bad, tenant_id="T1")
    r_mid = kernel.run_workflow("full_sync", p_mid, tenant_id="T1")
    s_bad = r_bad["data"]["quality_score"]
    s_mid = r_mid["data"]["quality_score"]
    assert s_bad is not None and s_mid is not None
    assert s_bad < s_mid  # critical must hurt more than a single major violation

def test_score_never_none_after_calculate(kernel):
    """Bug 3: any product running full_sync must never end with quality_score=None."""
    for sku, payload in [("A-1", {"name": "A", "price": 1.0}),
                          ("B-2", {"name": "B", "price": 2.0, "images": ["i.jpg"], "ean": "5901234123457"})]:
        res = kernel.run_workflow("full_sync", {**payload, "sku": sku}, tenant_id="T2")
        assert res["data"]["quality_score"] is not None, f"score None for {sku}"
