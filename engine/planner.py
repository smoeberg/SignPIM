from pydantic import BaseModel
from typing import List, Any, Dict

class ExecutionNode(BaseModel):
    node_id: str
    action: str
    rules: List[Any] = []
    depends_on: List[str] = []

class ExecutionGraph(BaseModel):
    workflow_name: str
    nodes: List[ExecutionNode]

class ExecutionPlanner:
    @staticmethod
    def build_execution_graph(compiled_ast) -> ExecutionGraph:
        """Converts Compiled Workflow AST into an executable Node Graph with explicit ordering."""
        nodes = []
        for idx, step in enumerate(compiled_ast.execution_plan):
            node_id = f"node_{idx+1}_{step.action}"
            depends = [f"node_{idx}_{compiled_ast.execution_plan[idx-1].action}"] if idx > 0 else []
            
            node = ExecutionNode(
                node_id=node_id,
                action=step.action,
                rules=step.rules,
                depends_on=depends
            )
            nodes.append(node)
            
        return ExecutionGraph(
            workflow_name=compiled_ast.name,
            nodes=nodes
        )
