from .registry import operator

@operator('empty')
def is_empty(val, target=None):
    return not bool(val)

@operator('lte')
def lte(val, target):
    return float(val or 0) <= float(target)

@operator('min_length')
def min_length(val, target):
    return len(str(val or '')) < int(target)

@operator('not_in')
def not_in(val, target):
    return val not in target if val else False
