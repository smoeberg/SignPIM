from .registry import operator

@operator('ean13_check')
def ean13_check(val, target=None):
    if not val or len(str(val)) != 13 or not str(val).isdigit():
        return True # Violation
    digits = [int(d) for d in str(val)]
    checksum = sum(digits[:12:2]) + sum(d * 3 for d in digits[1:12:2])
    check_digit = (10 - (checksum % 10)) % 10
    return digits[12] != check_digit

@operator('price_deviation_check')
def price_deviation(val, target, context=None):
    # market_avg should come from context/dependency, not hardcoded
    market_avg = context.get('market_avg', 100) if context else 100
    if not val: return False
    deviation = abs(float(val) - market_avg) / market_avg
    return deviation > float(target)
