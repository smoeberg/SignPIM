# rule_evaluator.py
"""
Rule evaluation logic — shared between auto_resolver.py and quality_score.py.
Extracted to avoid circular imports and duplicate evaluate() calls.

Returns True  → rule VIOLATES (problem exists).
Returns False → rule PASSES  (no problem).
"""
import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def evaluate_rule(rule_type: str, product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    if rule_type == 'missing_images':
        images = product.get('images', [])
        if isinstance(images, str):
            try:
                images = json.loads(images)
            except (json.JSONDecodeError, TypeError):
                images = [images]
        if not isinstance(images, list):
            images = [images] if images else []
        return len(images) == 0

    if rule_type == 'price_deviation':
        price = product.get('price')
        if price is None:
            return True
        source_price = product.get('source_price')
        if source_price is None:
            return False
        try:
            price = float(price)
            source_price = float(source_price)
            if source_price == 0:
                return price != 0
            return abs(price - source_price) / source_price * 100 > parameters.get('threshold_percent', 10)
        except (ValueError, TypeError):
            return False

    if rule_type == 'missing_ean':
        ean = product.get('ean')
        if ean is None:
            return True
        return str(ean).strip() in ('', '0')

    if rule_type == 'short_description':
        desc = product.get('description') or ''
        return len(str(desc).strip()) < parameters.get('min_length', 50)

    if rule_type == 'invalid_price':
        price = product.get('price')
        if price is None:
            return True
        try:
            return float(price) <= 0
        except (ValueError, TypeError):
            return True

    logger.warning('Unknown rule type: %s', rule_type)
    return False
