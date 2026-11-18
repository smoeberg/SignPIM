from typing import Dict, Any
from meta.schemas.models import WorkflowSchema
from engine.ast import WorkflowAST, ActionNode, ConditionNode, OperatorNode


class SchemaCompiler:
    @staticmethod
    def compile_workflow(raw_wf: Dict[str, Any], raw_rules: Dict[str, Any]) -> WorkflowAST:
        wf_schema = WorkflowSchema.model_validate(raw_wf)
        actions = []
        for step in wf_schema.steps:
            conditions = []
            if step.rules:
                for rid in step.rules:
                    if isinstance(rid, dict):
                        op_node = OperatorNode(
                            operator_name=rid.get("op", "equals"),
                            field=rid.get("field"),
                            target_value=rid.get("value"),
                        )
                        rule_key = f"inline_{rid.get('field')}_{rid.get('op')}"
                        msg_str = f"Inline rule on {rid.get('field')} failed"
                    else:
                        rule_key = str(rid).lower()
                        rule = raw_rules.get(rule_key, {})
                        op_node = OperatorNode(
                            operator_name=rule.get("operator", "equals"),
                            field=rule.get("field"),
                            target_value=rule.get("value"),
                        )
                        msg = rule.get("message", "")
                        msg_str = msg.get("en", str(msg)) if isinstance(msg, dict) else str(msg)

                    conditions.append(ConditionNode(
                        rule_id=rule_key,
                        operator_node=op_node,
                        message=msg_str,
                    ))

            actions.append(ActionNode(
                action_type=step.action,
                conditions=conditions,
                operator=step.operator,
                config=step.config,
            ))

        return WorkflowAST(
            name=wf_schema.name,
            entity_name=wf_schema.entity or "Product",
            actions=actions,
        )
