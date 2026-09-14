import logging
from typing import Dict, Any, Optional
from engine.planner import ExecutionGraph
from engine.types import TypeRegistry
from engine.operators.registry import RichOperatorRegistry

logger = logging.getLogger("PureGraphRuntime")

class PureGraphRuntime:
    def __init__(self, repository: Optional[Any] = None) -> None:
        self.repo = repository

    def execute(self, graph: ExecutionGraph, entity_meta: Dict[str, Any], data: Dict[str, Any], tenant_id: str, scoring: Optional[Dict[str, float]] = None, tenant_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not tenant_id:
            raise PermissionError("Tenant ID is strictly required for isolation.")

        logger.info(f"Executing Graph '{graph.workflow_name}' ({len(graph.nodes)} Nodes) for Tenant: {tenant_id}")

        # Fix Bug 1: preserve ALL input fields (undeclared fields like 'images'
        # must survive into the pipeline), then apply declared type-casts on top.
        typed_payload: Dict[str, Any] = dict(data)
        if entity_meta and 'fields' in entity_meta:
            for f_name, f_def in entity_meta['fields'].items():
                t_handler = TypeRegistry.get(f_def['type'])
                if t_handler:
                    typed_payload[f_name] = t_handler.cast_and_validate(data.get(f_name), f_def)
                else:
                    typed_payload[f_name] = data.get(f_name)

        ctx = {"data": typed_payload, "violations": [], "tenant_id": tenant_id,
               "tenant_settings": tenant_settings or {}}

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

            elif action.action_type == 'calculate':
                # Fix Bug 2: implement the scoring step.
                w = scoring or {'completeness': 0.4, 'consistency': 0.4, 'accuracy': 0.2}
                if sum(w.values()) > 0:
                    w = {k: v / sum(w.values()) for k, v in w.items()}
                # Severity-based deductions against a perfect 100 score.
                SEVERITY_DEDUCTION = {'critical': 25.0, 'major': 15.0, 'minor': 5.0, 'info': 1.0}
                deduction = 0.0
                seen = set()
                for v in ctx['violations']:
                    rule_id = v["rule_id"] if isinstance(v, dict) else v
                    if rule_id in seen:      # re-validation of AI output must not double-punish
                        continue
                    seen.add(rule_id)
                    rule_meta = (entity_meta or {}).get('_rule_meta', {}).get(rule_id, {})
                    deduction += SEVERITY_DEDUCTION.get(rule_meta.get('severity', 'major'), 15.0)
                ctx['data']['quality_score'] = round(max(0.0, 100.0 - deduction), 2)
                logger.info(f"Calculated quality_score: {ctx['data']['quality_score']} (violations: {ctx['violations']})")

            elif action.action_type == 'ai_enrich':
                op_name = getattr(action, 'operator', None) or 'llm_enrich'
                op_def = RichOperatorRegistry.get(op_name)
                if op_def is None:
                    raise ValueError(f"Unknown AI operator: {op_name}")
                cfg = getattr(action, 'config', None) or {}
                ctx['operator_config'] = cfg
                ctx['data'] = op_def.fn(ctx['data'], ctx)

            elif action.action_type == 'persist':
                if self.repo and entity_meta:
                    self.repo.save(entity_meta, ctx['data'], tenant_id)

        return ctx
