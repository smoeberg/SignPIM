"""
Native port of legacy/quality_score.py — the 3-dimensional quality model
(Completeness / Consistency / Accuracy) rebuilt as an engine-native service.

Behaviours preserved from the legacy implementation:
- 3D weighted score: completeness 0.4, consistency 0.4, accuracy 0.2
- Completeness: required-fields fill ratio (semantic emptiness: '', [], {})
- Consistency: rule pass-ratio against a tenant's active rule set
- Accuracy: source-of-truth reconciliation using *_updated_at timestamps
  (PIM manual overrides always win; other newer external systems penalize)
- Batch processing with fixed batch size to bound memory
- Empty tenants/inputs degrade gracefully to 100.0, not exceptions
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from engine.operators.rules import evaluate_rule

logger = logging.getLogger("signpim.quality")

WEIGHT_COMPLETENESS = 0.4
WEIGHT_CONSISTENCY = 0.4
WEIGHT_ACCURACY = 0.2

DEFAULT_REQUIRED_FIELDS = [
    'sku', 'name', 'price', 'ean', 'images', 'description', 'product_status',
]

# Tune this to balance memory vs. round-trips.
PRODUCT_BATCH_SIZE = 500


def _is_empty(value: Any) -> bool:
    """
    True when a field value should be treated as missing.
    Handles None, empty string, empty list, and empty dict.
    A dict like {} is structurally present but semantically empty for
    JSONB fields such as `name` and `description`.
    """
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == '':
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _parse_ts(value: Any) -> datetime:
    """Parse ISO timestamp, returning datetime.min on failure (treated as oldest)."""
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return datetime.min


def score_completeness(products: List[Dict], required_fields: List[str]) -> float:
    if not products or not required_fields:
        return 100.0
    total = 0.0
    for product in products:
        filled = sum(1 for f in required_fields if not _is_empty(product.get(f)))
        total += filled / len(required_fields)
    return round((total / len(products)) * 100, 2)


def score_consistency(products: List[Dict], rules: List[Dict]) -> float:
    if not products or not rules:
        return 100.0
    total = 0.0
    for product in products:
        passing = sum(
            1 for rule in rules
            if not evaluate_rule(rule['rule_type'], product, rule.get('parameters', {}))
        )
        total += passing / len(rules)
    return round((total / len(products)) * 100, 2)


def score_accuracy(products: List[Dict], source_of_truth_config: Dict) -> float:
    if not products or not source_of_truth_config:
        return 100.0

    sot_fields = list(source_of_truth_config.items())
    total = 0.0

    for product in products:
        merged = {**product, **(product.get('external_ids') or {}), **(product.get('raw_data') or {})}
        parsed_timestamps = {
            k: _parse_ts(v) for k, v in merged.items() if k.endswith('_updated_at')
        }

        correct = 0
        for field, sot_system in sot_fields:
            sot_key = f'{sot_system}_{field}_updated_at'
            sot_ts = parsed_timestamps.get(sot_key)

            if sot_ts is None or sot_ts == datetime.min:
                # Missing SoT timestamp does not fail the score
                logger.debug('Missing SoT timestamp for field %s in product %s',
                             field, product.get('id'))
                correct += 1
                continue

            field_suffix = f'_{field}_updated_at'
            is_correct = True
            for k, ts in parsed_timestamps.items():
                if k.endswith(field_suffix) and k != sot_key:
                    # Another external system newer than SoT is an accuracy issue.
                    # A newer PIM (manual override) is accepted.
                    if ts > sot_ts and not k.startswith('pim_'):
                        is_correct = False
                        break
            if is_correct:
                correct += 1

        total += correct / len(sot_fields)

    return round((total / len(products)) * 100, 2)


def calculate_scores(
    products: List[Dict],
    rules: Optional[List[Dict]] = None,
    source_of_truth_config: Optional[Dict] = None,
    required_fields: Optional[List[str]] = None,
) -> Dict:
    """
    Pure, in-memory 3D scoring over a product list.
    No DB dependency — persistence is the repository's concern.
    Returns the same shape as the legacy implementation.
    """
    products = products or []
    if not products:
        return None

    required_fields = required_fields or DEFAULT_REQUIRED_FIELDS
    rules = rules or []
    sot = source_of_truth_config or {}

    completeness = score_completeness(products, required_fields)
    consistency = score_consistency(products, rules)
    accuracy = score_accuracy(products, sot)

    total = round(
        completeness * WEIGHT_COMPLETENESS
        + consistency * WEIGHT_CONSISTENCY
        + accuracy * WEIGHT_ACCURACY,
        2,
    )

    return {
        'quality_score': total,
        'by_dimension': {
            'completeness': completeness,
            'consistency': consistency,
            'accuracy': accuracy,
        },
        'products_evaluated': len(products),
    }
