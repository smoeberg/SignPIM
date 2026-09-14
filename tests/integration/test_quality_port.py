"""Integration tests for the native quality_score port (engine/quality.py + batch_scoring.py)."""
import pytest
from engine.quality import calculate_scores, score_completeness, score_consistency, score_accuracy
from engine.batch_scoring import QualityScoringService
from engine.operators.rules import evaluate_rule

RULES = [
    {'rule_type': 'missing_images', 'parameters': {}},
    {'rule_type': 'invalid_price', 'parameters': {}},
    {'rule_type': 'missing_ean', 'parameters': {}},
]
SOT = {'price': 'erp'}

class _FakeRepo:
    def __init__(self):
        self.saved = []
    def save_quality_score(self, tenant_id, sku, score):
        self.saved.append((tenant_id, sku, score))

def test_completeness_counts_semantically_empty_fields():
    products = [
        {'sku': 'A', 'name': '', 'price': 1.0, 'ean': '', 'images': [], 'description': {}, 'product_status': 'draft'},
        {'sku': 'B', 'name': 'B', 'price': 1.0, 'ean': '5901234123457', 'images': ['x.jpg'], 'description': 'long enough description here', 'product_status': 'ready'},
    ]
    s = score_completeness(products, ['sku', 'name', 'price', 'ean', 'images', 'description', 'product_status'])
    assert 40.0 < s < 75.0  # A has 2/7, B has 7/7

def test_consistency_rule_pass_ratio():
    good = {'sku': 'G', 'name': 'G', 'price': 10.0, 'ean': '5901234123457', 'images': ['i.jpg']}
    bad = {'sku': 'B', 'name': 'B', 'price': -1.0}
    s = score_consistency([good, bad], RULES)
    assert s == 50.0  # good passes all 3, bad passes none

def test_accuracy_pim_override_wins_over_erp():
    product = {
        'id': 'p1',
        'erp_price_updated_at': '2026-01-01T10:00:00+00:00',
        'pim_price_updated_at': '2026-01-02T10:00:00+00:00',  # newer PIM = manual override OK
    }
    s = score_accuracy([product], SOT)
    assert s == 100.0

def test_accuracy_flags_newer_external_system():
    product = {
        'id': 'p2',
        'erp_price_updated_at': '2026-01-01T10:00:00+00:00',
        'supplier_price_updated_at': '2026-01-05T10:00:00+00:00',  # newer external system = stale SoT
    }
    s = score_accuracy([product], SOT)
    assert s < 100.0

def test_empty_inputs_degrade_to_100():
    assert calculate_scores([]) is None
    assert score_completeness([], ['sku']) == 100.0
    assert score_consistency([{'sku': 'X'}], []) == 100.0
    assert score_accuracy([{'sku': 'X'}], {}) == 100.0

def test_batch_service_per_product_and_summary():
    svc = QualityScoringService()
    products = [
        {'sku': 'GOOD-1', 'name': 'N', 'price': 10.0, 'ean': '5901234123457', 'images': ['i.jpg'], 'description': 'long description here', 'product_status': 'ready'},
        {'sku': 'BAD-1', 'name': '', 'price': -5.0},
        {'sku': 'BAD-2', 'name': 'X', 'price': 5.0},
    ]
    result = svc.run(products, rules=RULES, tenant_id='T1')
    assert result['products_evaluated'] == 3
    assert len(result['per_product']) == 3
    by_sku = {p['sku']: p for p in result['per_product']}
    assert by_sku['GOOD-1']['quality_score'] > by_sku['BAD-1']['quality_score']
    assert by_sku['BAD-1']['quality_score'] < 50.0
    assert 'invalid_price' in by_sku['BAD-1']['failing_rules']

def test_batch_service_persists_scores():
    repo = _FakeRepo()
    svc = QualityScoringService(repository=repo)
    result = svc.run(
        [{'sku': 'P1', 'name': 'N', 'price': 1.0}, {'sku': 'P2', 'name': 'M', 'price': 2.0}],
        rules=RULES, tenant_id='T9',
    )
    assert len(repo.saved) == 2
    assert all(t == 'T9' for t, _, _ in repo.saved)
    skus = {s for _, s, _ in repo.saved}
    assert skus == {'P1', 'P2'}

def test_legacy_rule_semantics_preserved():
    """Ported rules must behave identically to the legacy dispatch."""
    assert evaluate_rule('missing_images', {'images': '["a.jpg"]'}, {}) is False   # stringified JSON list
    assert evaluate_rule('missing_images', {'images': ''}, {}) is True
    assert evaluate_rule('valid_ean_13', {'ean': '5901234123457'}, {}) is False    # valid checksum
    assert evaluate_rule('valid_ean_13', {'ean': '5901234123458'}, {}) is True     # bad checksum
    assert evaluate_rule('valid_ean_13', {'ean': '0'}, {}) is False                # delegated to missing_ean
    assert evaluate_rule('missing_ean', {'ean': '0'}, {}) is True
    assert evaluate_rule('missing_sku', {'sku': 'AB'}, {'min_sku_length': 3}) is True
    assert evaluate_rule('unsupported_currency', {'currency': 'USD'}, {'allowed_currencies': ['DKK']}) is True
    assert evaluate_rule('price_deviation', {'price': 200.0, 'source_price': 100.0}, {'threshold_percent': 10}) is True
    assert evaluate_rule('price_deviation', {'price': 105.0, 'source_price': 100.0}, {'threshold_percent': 10}) is False
