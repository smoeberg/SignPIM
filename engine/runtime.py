import logging
from typing import Dict, Any, Optional
from engine.planner import ExecutionGraph
from engine.types import TypeRegistry
from engine.operators.registry import RichOperatorRegistry

logger = logging.getLogger("PureGraphRuntime")

class PureGraphRuntime:
    def __init__(self, repository: Optional[Any] = None) -> None:
        self.repo = repository

    def execute(self, graph: ExecutionGraph, entity_meta: Dict[str, Any], data: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
        if not tenant_id:
            raise PermissionError("Tenant ID is strictly required for isolation.")

        logger.info(f"Executing Graph '{graph.workflow_name}' ({len(graph.nodes)} Nodes) for Tenant: {tenant_id}")

        typed_payload: Dict[str, Any] = {}
        if entity_meta and 'fields' in entity_meta:
            for f_name, f_def in entity_meta['fields'].items():
                val = data.get(f_name)
                t_handler = TypeRegistry.get(f_def['type'])
                if t_handler:
                    typed_payload[f_name] = t_handler.cast_and_validate(val, f_def)
                else:
                    typed_payload[f_name] = val
        else:
            typed_payload = data

        ctx = {"data": typed_payload, "violations": [], "tenant_id": tenant_id}

        for node in graph.nodes:
            action = node.action_node
            logger.info(f"Graph Node Executing [{node.node_id}] Action: {action.action_type}")

            if action.action_type == 'validate':
                for cond in action.conditions:
                    op = cond.operator_node
                    op_def = RichOperatorRegistry.get(op.operator_name)
                    if op_def:
                        val = ctx['data'].get(op.field)
                        if op_def.fn(val, op.target_value, None):
                            logger.warning(f"Violation Detected [{cond.rule_id}]: {cond.message}")
                            ctx['violations'].append(cond.rule_id)

            elif action.action_type == 'transform':
                for cond in action.conditions:
                    op = cond.operator_node
                    if op.operator_name == 'multiply' and op.field in ctx['data']:
                        ctx['data'][op.field] = ctx['data'][op.field] * float(op.target_value)
                        logger.info(f"Transformed field [{op.field}] using operator multiply: {ctx['data'][op.field]}")

            elif action.action_type == 'persist':
                if self.repo and entity_meta:
                    self.repo.save(entity_meta, ctx['data'], tenant_id)

        return ctx
