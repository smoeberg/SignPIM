class OperatorRegistry:
    _operators = {}

    @classmethod
    def register(cls, name, func):
        cls._operators[name] = func

    @classmethod
    def get(cls, name):
        return cls._operators.get(name)

def operator(name):
    def decorator(func):
        OperatorRegistry.register(name, func)
        return func
    return decorator
