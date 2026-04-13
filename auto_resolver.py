# auto_resolver.py
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from rule_evaluator import evaluate_rule

logger = logging.getLogger(__name__)


def should_auto_resolve(
    product: Dict[str, Any],
    rule: Dict[str, Any],
    stability: Optional[Dict],
    already_passing: bool,
) -> bool:
    """
    Determine if a task should be auto-resolved.

    `already_passing` is passed in from the caller so we avoid a second
    evaluate_rule() call — the caller computed it for update_stability already.

    All three conditions must be true:
    1. Rule no longer fires against current product data.
    2. At least 2 consecutive clean syncs.
    3. At least 10 minutes since last recorded failure.
    """
    if not already_passing:
        return False

    if stability is None:
        return False

    if stability.get('consecutive_successes', 0) < 2:
        return False

    last_failure = stability.get('last_failure_at')
    if last_failure:
        if isinstance(last_failure, str):
            try:
                last_failure = datetime.fromisoformat(last_failure.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                pass
        if isinstance(last_failure, datetime):
            if (datetime.now() - last_failure).total_seconds() < 600:
                return False

    return True


def _check_flapping(stability: Optional[Dict], cur, product_id: str, rule_id: str) -> bool:
    """
    Returns True if this product+rule pair is flapping
    (more than 3 state changes within the last 60 minutes).
    """
    if stability is None:
        return False

    last_reset = stability.get('last_flapping_reset')
    if last_reset:
        if isinstance(last_reset, str):
            try:
                last_reset = datetime.fromisoformat(last_reset.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                pass
        if isinstance(last_reset, datetime):
            if (datetime.now() - last_reset).total_seconds() > 3600:
                cur.execute(
                    """UPDATE product_stability
                       SET flapping_count = 0, last_flapping_reset = NOW()
                       WHERE product_id = %s AND rule_id = %s""",
                    [product_id, rule_id],
                )
                return False

    return stability.get('flapping_count', 0) >= 3


def update_stability(product_id: str, rule_id: str, passed: bool, cur) -> None:
    """
    Upsert the stability record for a product+rule pair after each sync.
    """
    cur.execute(
        'SELECT * FROM product_stability WHERE product_id = %s AND rule_id = %s',
        [product_id, rule_id],
    )
    existing = cur.fetchone()

    if existing is None:
        cur.execute(
            """INSERT INTO product_stability
               (product_id, rule_id, consecutive_successes, last_failure_state,
                last_failure_at, flapping_count, last_flapping_reset)
               VALUES (%s, %s, %s, %s, %s, 0, NOW())""",
            [product_id, rule_id, 1 if passed else 0, not passed, None if passed else datetime.now()],
        )
        return

    if passed:
        cur.execute(
            """UPDATE product_stability
               SET consecutive_successes = consecutive_successes + 1,
                   last_failure_state    = FALSE,
                   flapping_count        = CASE WHEN last_failure_state = TRUE
                                                THEN flapping_count + 1
                                                ELSE flapping_count END
               WHERE product_id = %s AND rule_id = %s""",
            [product_id, rule_id],
        )
    else:
        cur.execute(
            """UPDATE product_stability
               SET consecutive_successes = 0,
                   last_failure_state    = TRUE,
                   last_failure_at       = NOW(),
                   flapping_count        = CASE WHEN last_failure_state = FALSE
                                                THEN flapping_count + 1
                                                ELSE flapping_count END
               WHERE product_id = %s AND rule_id = %s""",
            [product_id, rule_id],
        )


def auto_resolve_tasks(conn, cur) -> int:
    """
    Check all open tasks and auto-resolve those whose underlying data problem
    has been fixed and whose stability window is satisfied.
    """
    cur.execute("""
        SELECT
            t.id         AS task_id,
            t.tenant_id,
            t.product_id,
            t.rule_id,
            t.status,
            p.sku, p.price, p.ean, p.images, p.description, p.name,
            p.product_status, p.external_ids, p.raw_data,
            r.rule_type,
            r.parameters
        FROM tasks t
        JOIN products p ON t.product_id = p.id
        JOIN rules r    ON t.rule_id    = r.id
        WHERE t.status IN ('open', 'assigned', 'in_progress')
          AND t.resolution_code IS NULL
    """)
    rows = cur.fetchall()

    # Batch-fetch all relevant stability records in one query to avoid N+1
    if rows:
        product_rule_pairs = list({(r['product_id'], r['rule_id']) for r in rows})
        cur.execute(
            """SELECT product_id, rule_id, consecutive_successes, last_failure_state,
                      last_failure_at, flapping_count, last_flapping_reset
               FROM product_stability
               WHERE (product_id, rule_id) = ANY(%s::uuid[])""",
            # Build a postgres array of composite values
            [[(p, r) for p, r in product_rule_pairs]],
        )
        stability_map = {
            (row['product_id'], row['rule_id']): dict(row)
            for row in cur.fetchall()
        }
    else:
        stability_map = {}

    resolved_count = 0

    for row in rows:
        product = {
            'sku': row['sku'],
            'price': row['price'],
            'ean': row['ean'],
            'images': row['images'],
            'description': row.get('description', ''),
            'name': row.get('name', ''),
            'product_status': row['product_status'],
            'external_ids': row['external_ids'],
            'raw_data': row['raw_data'],
        }
        rule = {
            'rule_type': row['rule_type'],
            'parameters': row['parameters'] or {},
        }

        # Single evaluate_rule call — result is reused for both stability
        # tracking and the auto-resolve decision.
        passed = not evaluate_rule(rule['rule_type'], product, rule['parameters'])
        update_stability(row['product_id'], row['rule_id'], passed, cur)

        # Re-fetch only this row's updated stability (targeted, not full batch)
        cur.execute(
            'SELECT * FROM product_stability WHERE product_id = %s AND rule_id = %s',
            [row['product_id'], row['rule_id']],
        )
        stability = cur.fetchone()

        if _check_flapping(stability, cur, row['product_id'], row['rule_id']):
            logger.warning(
                'Flapping detected for product %s rule %s — skipping',
                row['product_id'], row['rule_id'],
            )
            continue

        if should_auto_resolve(product, rule, stability, already_passing=passed):
            cur.execute(
                """UPDATE tasks
                   SET status          = 'resolved',
                       resolved_at     = NOW(),
                       resolution_code = 'auto_resolved',
                       updated_at      = NOW()
                   WHERE id = %s""",
                [row['task_id']],
            )
            resolved_count += 1
            logger.info('Auto-resolved task %s (product %s)', row['task_id'], row['product_id'])

    conn.commit()
    logger.info('Auto-resolve complete: %d tasks resolved', resolved_count)
    return resolved_count
