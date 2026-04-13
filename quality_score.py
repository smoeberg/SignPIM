# quality_score.py
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from rule_evaluator import evaluate_rule

logger = logging.getLogger(__name__)

WEIGHT_COMPLETENESS = 0.4
WEIGHT_CONSISTENCY  = 0.4
WEIGHT_ACCURACY     = 0.2

DEFAULT_REQUIRED_FIELDS = ['sku', 'name', 'price', 'ean', 'images', 'description', 'product_status']

# Tune this to balance memory vs. DB round-trips.
PRODUCT_BATCH_SIZE = 500


def _is_empty(value: Any) -> bool:
    """
    Return True when a field value should be treated as missing.
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


def _score_completeness(products: List[Dict], required_fields: List[str]) -> float:
    if not products or not required_fields:
        return 100.0

    total_score = 0.0
    for product in products:
        filled = sum(1 for f in required_fields if not _is_empty(product.get(f)))
        total_score += filled / len(required_fields)

    return round((total_score / len(products)) * 100, 2)


def _score_consistency(products: List[Dict], rules: List[Dict]) -> float:
    if not products or not rules:
        return 100.0

    total_score = 0.0
    for product in products:
        passing = sum(
            1 for rule in rules
            if not evaluate_rule(rule['rule_type'], product, rule.get('parameters', {}))
        )
        total_score += passing / len(rules)

    return round((total_score / len(products)) * 100, 2)


def _score_accuracy(products: List[Dict], source_of_truth_config: Dict) -> float:
    if not products or not source_of_truth_config:
        return 100.0

    sot_fields = list(source_of_truth_config.keys())
    if not sot_fields:
        return 100.0

    total_score = 0.0

    for product in products:
        # Merge once per product instead of per field
        merged = {**(product.get('external_ids') or {}), **(product.get('raw_data') or {})}
        correct = 0

        for field, sot_system in source_of_truth_config.items():
            sot_key = f'{sot_system}_{field}_updated_at'
            sot_ts_raw = merged.get(sot_key)

            if not sot_ts_raw:
                correct += 1  # Can't determine — treat as correct
                continue

            try:
                sot_ts = datetime.fromisoformat(str(sot_ts_raw).replace('Z', '+00:00'))
            except (ValueError, TypeError):
                correct += 1
                continue

            is_newest = all(
                _parse_ts(v) <= sot_ts
                for k, v in merged.items()
                if k.endswith(f'_{field}_updated_at') and k != sot_key
            )

            if is_newest:
                correct += 1

        total_score += correct / len(sot_fields)

    return round((total_score / len(products)) * 100, 2)


def _parse_ts(value: Any) -> datetime:
    """Parse ISO timestamp, returning datetime.min on failure (treated as oldest)."""
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return datetime.min


def _fetch_products_batched(cur, tenant_id: str) -> List[Dict]:
    """
    Stream products in fixed-size batches to avoid loading the full
    catalogue into memory for large tenants.
    Returns a list — callers should be aware this is still all-in-memory
    once assembled. For very large catalogues a streaming aggregation
    approach (accumulating scores across batches) would be more robust.
    """
    products: List[Dict] = []
    offset = 0

    while True:
        cur.execute(
            """SELECT * FROM products
               WHERE tenant_id = %s AND product_status = 'active'
               ORDER BY id
               LIMIT %s OFFSET %s""",
            [tenant_id, PRODUCT_BATCH_SIZE, offset],
        )
        batch = cur.fetchall()
        if not batch:
            break
        products.extend(batch)
        offset += len(batch)
        if len(batch) < PRODUCT_BATCH_SIZE:
            break

    return products


def calculate_quality_score(tenant_id: str, conn, cur) -> Optional[Dict]:
    """
    Calculate and persist the quality score snapshot for a tenant.
    Called after each nightly scan.

    Score = completeness × 0.4 + consistency × 0.4 + accuracy × 0.2

    Transaction ownership: this function commits its own INSERT so the
    snapshot is durable even when called as part of a larger job. Callers
    should not wrap this in their own transaction.
    """
    products = _fetch_products_batched(cur, tenant_id)

    if not products:
        logger.info('Tenant %s: no active products — skipping quality score', tenant_id)
        return None

    cur.execute(
        'SELECT rule_type, parameters FROM rules WHERE tenant_id = %s AND is_active = TRUE',
        [tenant_id],
    )
    rules = cur.fetchall()

    cur.execute(
        'SELECT source_of_truth FROM normalization_mappings WHERE tenant_id = %s LIMIT 1',
        [tenant_id],
    )
    norm_row = cur.fetchone()
    source_of_truth_config = norm_row['source_of_truth'] if norm_row else {}

    cur.execute('SELECT settings FROM tenants WHERE id = %s', [tenant_id])
    tenant_row = cur.fetchone()
    settings = (tenant_row.get('settings') or {}) if tenant_row else {}
    required_fields = settings.get('required_fields', DEFAULT_REQUIRED_FIELDS)

    completeness = _score_completeness(products, required_fields)
    consistency  = _score_consistency(products, rules)
    accuracy     = _score_accuracy(products, source_of_truth_config)

    total = round(
        completeness * WEIGHT_COMPLETENESS +
        consistency  * WEIGHT_CONSISTENCY  +
        accuracy     * WEIGHT_ACCURACY,
        2,
    )

    cur.execute(
        """INSERT INTO quality_snapshots
             (tenant_id, score_total, score_completeness, score_consistency,
              score_accuracy, products_evaluated, calculated_at)
           VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
        [tenant_id, total, completeness, consistency, accuracy, len(products)],
    )
    conn.commit()

    logger.info(
        'Quality score tenant=%s total=%s completeness=%s consistency=%s accuracy=%s products=%d',
        tenant_id, total, completeness, consistency, accuracy, len(products),
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
