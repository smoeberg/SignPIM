from pydantic import BaseModel
from typing import Callable, Optional, List

class OperatorMetadata(BaseModel):
    name: str
    version: str = "1.0.0"
    category: str = "validation"
    is_deterministic: bool = True
    side_effects: bool = False
    transactional: bool = True
    is_async: bool = False
    security_level: str = "standard"
    timeout_ms: int = 5000
    required_context: List[str] = []

class OperatorDefinition:
    def __init__(self, metadata: OperatorMetadata, fn: Callable):
        self.meta = metadata
        self.fn = fn

class RichOperatorRegistry:
    _operators = {}

    @classmethod
    def register(cls, name: str, fn: Callable, **kwargs):
        meta = OperatorMetadata(name=name, **kwargs)
        cls._operators[name] = OperatorDefinition(meta, fn)

    @classmethod
    def get(cls, name: str) -> Optional[OperatorDefinition]:
        return cls._operators.get(name)
