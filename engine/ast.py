from pydantic import BaseModel
from typing import List, Any, Optional

class ASTNode(BaseModel):
    node_type: str

class LiteralNode(ASTNode):
    node_type: str = "literal"
    value: Any

class OperatorNode(ASTNode):
    node_type: str = "operator"
    operator_name: str
    field: Optional[str] = None
    target_value: Optional[Any] = None

class ConditionNode(ASTNode):
    node_type: str = "condition"
    rule_id: str
    operator_node: OperatorNode
    message: Optional[str] = None

class ActionNode(ASTNode):
    node_type: str = "action"
    action_type: str
    conditions: List[ConditionNode] = []

class WorkflowAST(ASTNode):
    node_type: str = "workflow"
    name: str
    entity_name: str
    actions: List[ActionNode] = []
