from .registry import TypeRegistry, BaseType

class StringType(BaseType):
    def cast_and_validate(self, value, field_def=None):
        if value is None: return None
        return str(value)

class NumberType(BaseType):
    def cast_and_validate(self, value, field_def=None):
        if value is None: return None
        try:
            return float(value)
        except (ValueError, TypeError):
            raise ValueError(f"Value '{value}' cannot be cast to Number")

class BooleanType(BaseType):
    def cast_and_validate(self, value, field_def=None):
        if value is None: return None
        return bool(value)

class EnumType(BaseType):
    def cast_and_validate(self, value, field_def=None):
        if value is None: return None
        allowed = field_def.get('values', []) if field_def else []
        if value not in allowed:
            raise ValueError(f"Value '{value}' not in allowed enum {allowed}")
        return value

# Register Types
TypeRegistry.register('string', StringType())
TypeRegistry.register('number', NumberType())
TypeRegistry.register('boolean', BooleanType())
TypeRegistry.register('enum', EnumType())
