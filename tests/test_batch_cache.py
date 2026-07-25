import pytest
from engine.batch import BatchRuleEvaluator
from engine.cache import QualityScoreCache
from core.errors import ValidationError, TenantIsolationError

def test_batch_evaluator():
    evaluator = BatchRuleEvaluator(max_workers=2)
    items = [1, 2, 3, 4]
    results = evaluator.evaluate_batch(items, lambda x: x * 2)
    assert results == [2, 4, 6, 8]

def test_quality_cache():
    score1 = QualityScoreCache.get_score(["missing_images"], 10)
    score2 = QualityScoreCache.get_score(["missing_images"], 10)
    assert score1 == 90.0
    assert score1 == score2

def test_app_errors():
    err = ValidationError("Invalid SKU")
    assert err.status_code == 400
    assert err.error_code == "VALIDATION_ERROR"
