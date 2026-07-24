from .registry import TypeRegistry, BaseType

class StringType(BaseType):
    def cast(self, value):
        return str(value) if value is not None else None

    def database_type(self):
        return "TEXT"

class NumberType(BaseType):
    def cast(self, value):
        if value is None: return None
        try:
            return float(value)
        except (ValueError, TypeError):
            raise ValueError(f"Cannot cast '{value}' to Number")

    def database_type(self):
        return "NUMERIC(15, 2)"

class BooleanType(BaseType):
    def cast(self, value):
        return bool(value) if value is not None else None

    def database_type(self):
        return "BOOLEAN"

class EnumType(BaseType):
    def validate(self, value, field_def=None):
        if value is None: return True
        allowed = field_def.get('values', []) if field_def else []
        if value not in allowed:
            raise ValueError(f"Value '{value}' not in allowed enum {allowed}")
        return True

    def database_type(self):
        return "VARCHAR(100)"

TypeRegistry.register('string', StringType())
TypeRegistry.register('number', NumberType())
TypeRegistry.register('boolean', BooleanType())
TypeRegistry.register('enum', EnumType())
