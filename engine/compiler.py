from pydantic import BaseModel
from typing import List, Dict, Any

class CompiledStepAST(BaseModel):
    action: str
    rules: List[Any] = []

class CompiledWorkflowAST(BaseModel):
    name: str
    entity_name: str
    execution_plan: List[CompiledStepAST]

class SchemaCompiler:
    @staticmethod
    def compile_workflow(workflow_schema, rules_meta) -> CompiledWorkflowAST:
        """Compiles raw WorkflowSchema into an optimized Execution AST."""
        plan = []
        for step in workflow_schema.steps:
            compiled_step = CompiledStepAST(
                action=step.action,
                rules=step.rules or []
            )
            plan.append(compiled_step)
            
        return CompiledWorkflowAST(
            name=workflow_schema.name,
            entity_name=workflow_schema.entity,
            execution_plan=plan
        )
