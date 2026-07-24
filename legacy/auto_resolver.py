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
    """
    if not already_passing or stability is None:
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
            # Ensure comparison is timezone-aware
            now = datetime.now(last_failure.tzinfo) if last_failure.tzinfo else datetime.now()
            if (now - last_failure).total_seconds() < 600:
                return False

    return True


def update_stability(product_id: str, rule_id: str, passed: bool, cur) -> Dict:
    """
    Upsert the stability record and RETURN the updated record to avoid N+1 queries.
    """
    if passed:
        cur.execute(
            """INSERT INTO product_stability
               (product_id, rule_id, consecutive_successes, last_failure_state, last_flapping_reset)
               VALUES (%s, %s, 1, FALSE, NOW())
               ON CONFLICT (product_id, rule_id) DO UPDATE
               SET consecutive_successes = product_stability.consecutive_successes + 1,
                   last_failure_state    = FALSE,
                   flapping_count        = CASE WHEN product_stability.last_failure_state = TRUE
                                                THEN product_stability.flapping_count + 1
                                                ELSE product_stability.flapping_count END
               RETURNING *""",
            [product_id, rule_id],
        )
    else:
        cur.execute(
            """INSERT INTO product_stability
               (product_id, rule_id, consecutive_successes, last_failure_state, last_failure_at, last_flapping_reset)
               VALUES (%s, %s, 0, TRUE, NOW(), NOW())
               ON CONFLICT (product_id, rule_id) DO UPDATE
               SET consecutive_successes = 0,
                   last_failure_state    = TRUE,
                   last_failure_at       = NOW(),
                   flapping_count        = CASE WHEN product_stability.last_failure_state = FALSE
                                                THEN product_stability.flapping_count + 1
                                                ELSE product_stability.flapping_count END
               RETURNING *""",
            [product_id, rule_id],
        )
    return cur.fetchone()


def auto_resolve_tasks(conn, cur) -> int:
    """
    Check all open tasks and auto-resolve those whose underlying data problem
    has been fixed and whose stability window is satisfied.
    """
    cur.execute("""
        SELECT
            t.id         AS task_id, t.tenant_id, t.product_id, t.rule_id, t.status,
            p.sku, p.price, p.ean, p.images, p.description, p.name,
            p.product_status, p.external_ids, p.raw_data,
            r.rule_type, r.parameters
        FROM tasks t
        JOIN products p ON t.product_id = p.id
        JOIN rules r    ON t.rule_id    = r.id
        WHERE t.status IN ('open', 'assigned', 'in_progress')
          AND t.resolution_code IS NULL
    """)
    rows = cur.fetchall()
    resolved_count = 0

    for row in rows:
        product = {
            'sku': row['sku'], 'price': row['price'], 'ean': row['ean'],
            'images': row['images'], 'description': row.get('description', ''),
            'name': row.get('name', ''), 'product_status': row['product_status'],
            'external_ids': row['external_ids'], 'raw_data': row['raw_data'],
        }
        rule = {'rule_type': row['rule_type'], 'parameters': row['parameters'] or {}}

        passed = not evaluate_rule(rule['rule_type'], product, rule['parameters'])
        
        # update_stability now returns the record directly, removing one query per row
        stability = update_stability(row['product_id'], row['rule_id'], passed, cur)

        if _check_flapping(stability, cur, row['product_id'], row['rule_id']):
            logger.warning('Flapping detected for product %s rule %s', row['product_id'], row['rule_id'])
            continue

        if should_auto_resolve(product, rule, stability, already_passing=passed):
            cur.execute(
                """UPDATE tasks
                   SET status = 'resolved', resolved_at = NOW(),
                       resolution_code = 'auto_resolved', updated_at = NOW()
                   WHERE id = %s""",
                [row['task_id']],
            )
            resolved_count += 1
            logger.info('Auto-resolved task %s', row['task_id'])

    conn.commit()
    return resolved_count
