class TypeRegistry:
    _types = {}

    @classmethod
    def register(cls, name, type_cls):
        cls._types[name] = type_cls

    @classmethod
    def get(cls, name):
        return cls._types.get(name)

class BaseType:
    def cast(self, value):
        return value

    def validate(self, value, field_def=None):
        return True

    def serialize(self, value):
        return value

    def deserialize(self, value):
        return value

    def normalize(self, value):
        return str(value).strip() if isinstance(value, str) else value

    def database_type(self):
        return "VARCHAR(255)"

    def cast_and_validate(self, value, field_def=None):
        casted = self.cast(value)
        self.validate(casted, field_def)
        return self.normalize(casted)
