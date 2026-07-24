from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

class FieldDefinition(BaseModel):
    type: str
    required: bool = False
    unique: bool = False
    values: Optional[List[str]] = None

class StorageConfig(BaseModel):
    table_name: str
    audit_enabled: bool = True

class EntitySchema(BaseModel):
    name: str
    storage: StorageConfig
    fields: Dict[str, FieldDefinition]

class RuleSchema(BaseModel):
    id: str
    field: Optional[str] = None
    operator: str
    value: Optional[Any] = None
    category: Optional[str] = "general"
    severity: Optional[str] = "warning"
    message: Optional[Dict[str, str]] = None

class WorkflowStep(BaseModel):
    action: str
    rules: Optional[List[Any]] = None
    using: Optional[str] = None

class WorkflowSchema(BaseModel):
    name: str
    entity: str
    steps: List[WorkflowStep]
