from pydantic import BaseModel
from typing import List, Dict, Any
from engine.ast import WorkflowAST, ActionNode

class GraphNode(BaseModel):
    node_id: str
    action_node: ActionNode
    dependencies: List[str] = []

class ExecutionGraph(BaseModel):
    workflow_name: str
    entity_name: str
    nodes: List[GraphNode]

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "ExecutionGraph":
        return cls.model_validate_json(json_str)

class ExecutionPlanner:
    @staticmethod
    def build_graph(ast: WorkflowAST) -> ExecutionGraph:
        graph_nodes = []
        for idx, action in enumerate(ast.actions):
            node_id = f"node_{idx+1}_{action.action_type}"
            deps = [graph_nodes[-1].node_id] if graph_nodes else []
            
            graph_nodes.append(GraphNode(
                node_id=node_id,
                action_node=action,
                dependencies=deps
            ))
            
        return ExecutionGraph(
            workflow_name=ast.name,
            entity_name=ast.entity_name,
            nodes=graph_nodes
        )
