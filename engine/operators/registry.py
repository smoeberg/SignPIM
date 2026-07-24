from pydantic import BaseModel
from typing import Callable, Optional, Any

class OperatorMetadata(BaseModel):
    name: str
    version: str = "1.0.0"
    is_deterministic: bool = True
    input_type: Optional[str] = "any"
    output_type: str = "boolean"

class OperatorDefinition:
    def __init__(self, metadata: OperatorMetadata, fn: Callable):
        self.meta = metadata
        self.fn = fn

class RichOperatorRegistry:
    _operators = {}

    @classmethod
    def register(cls, name: str, fn: Callable, version: str = "1.0.0", is_deterministic: bool = True):
        meta = OperatorMetadata(name=name, version=version, is_deterministic=is_deterministic)
        cls._operators[name] = OperatorDefinition(meta, fn)

    @classmethod
    def get(cls, name: str) -> Optional[OperatorDefinition]:
        return cls._operators.get(name)
