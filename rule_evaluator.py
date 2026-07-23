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
    """
    Evaluates a rule against a product.
    Returns True if the rule VIOLATES (there is an error).
    Returns False if the rule PASSES.
    """
    dispatch = {
        'missing_images': _rule_missing_images,
        'price_deviation': _rule_price_deviation,
        'missing_ean': _rule_missing_ean,
        'short_description': _rule_short_description,
        'invalid_price': _rule_invalid_price,
        'valid_ean_13': _rule_valid_ean_13,
        'unsupported_currency': _rule_unsupported_currency,
        'missing_sku': _rule_missing_sku,
        'invalid_image_url': _rule_invalid_image_url
    }

    rule_func = dispatch.get(rule_type)
    if not rule_func:
        logger.warning('Unknown rule type: %s', rule_type)
        return False

    return rule_func(product, parameters)


def _rule_missing_images(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    images = product.get('images', [])
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except (json.JSONDecodeError, TypeError):
            images = [images]
    if not isinstance(images, list):
        images = [images] if images else []
    return len(images) == 0


def _rule_price_deviation(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
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
        threshold = parameters.get('threshold_percent', 10)
        return abs(price - source_price) / source_price * 100 > threshold
    except (ValueError, TypeError):
        return False


def _rule_missing_ean(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    ean = product.get('ean')
    if ean is None:
        return True
    return str(ean).strip() in ('', '0')


def _rule_short_description(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    desc = product.get('description') or ''
    return len(str(desc).strip()) < parameters.get('min_length', 50)


def _rule_invalid_price(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    price = product.get('price')
    if price is None:
        return True
    try:
        return float(price) <= 0
    except (ValueError, TypeError):
        return True


def _rule_valid_ean_13(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    ean = str(product.get('ean') or '').strip()
    if len(ean) != 13 or not ean.isdigit():
        return True
    # EAN-13 Checksum
    digits = [int(d) for d in ean]
    checksum = sum(digits[i] * (3 if i % 2 else 1) for i in range(12))
    check_digit = (10 - (checksum % 10)) % 10
    return digits[12] != check_digit


def _rule_unsupported_currency(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    currency = product.get('currency', 'DKK')
    allowed = parameters.get('allowed_currencies', ['DKK', 'EUR', 'SEK', 'NOK'])
    return currency not in allowed


def _rule_missing_sku(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    sku = str(product.get('sku') or '').strip()
    return len(sku) < parameters.get('min_sku_length', 3)


def _rule_invalid_image_url(product: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    images = product.get('images', [])
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except:
            images = [images]
    if not isinstance(images, list):
        images = [images]

    for img in images:
        if not str(img).startswith(('http://', 'https://')):
            return True
    return False
