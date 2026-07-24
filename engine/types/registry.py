class TypeRegistry:
    _types = {}

    @classmethod
    def register(cls, name, type_cls):
        cls._types[name] = type_cls

    @classmethod
    def get(cls, name):
        return cls._types.get(name)

class BaseType:
    def cast_and_validate(self, value, field_def=None):
        raise NotImplementedError
