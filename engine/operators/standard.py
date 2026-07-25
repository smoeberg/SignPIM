from .registry import RichOperatorRegistry

def is_empty(val, target=None, context=None):
    return not bool(val)

def lte(val, target, context=None):
    return float(val or 0) <= float(target)

def min_length(val, target, context=None):
    return len(str(val or '')) < int(target)

# Register
RichOperatorRegistry.register('empty', is_empty)
RichOperatorRegistry.register('lte', lte)
RichOperatorRegistry.register('min_length', min_length)
