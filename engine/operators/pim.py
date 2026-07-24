from .registry import RichOperatorRegistry

def ean13_check(val, target=None, context=None):
    if not val or len(str(val)) != 13 or not str(str(val)).isdigit():
        return True
    digits = [int(d) for d in str(val)]
    checksum = sum(digits[:12:2]) + sum(d * 3 for d in digits[1:12:2])
    check_digit = (10 - (checksum % 10)) % 10
    return digits[12] != check_digit

RichOperatorRegistry.register('ean13_check', ean13_check)
