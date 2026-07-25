import pytest
from engine.runtime import PureGraphRuntime
from engine.planner import ExecutionGraph, GraphNode
from engine.ast import ActionNode, ConditionNode, OperatorNode
from engine.types.standard import NumberType

def test_tenant_isolation_violation():
    runtime = PureGraphRuntime()
    graph = ExecutionGraph(
        workflow_name="Test",
        entity_name="Product",
        nodes=[]
    )
    with pytest.raises(PermissionError):
        runtime.execute(graph, {}, {}, tenant_id="")

def test_number_type_invalid_cast():
    handler = NumberType()
    result = handler.cast_and_validate("invalid_number", {"type": "number"})
    assert result == "invalid_number"  # Fallback gracefully

def test_pure_graph_runtime_execution():
    runtime = PureGraphRuntime()
    graph = ExecutionGraph(
        workflow_name="TestWF",
        entity_name="Product",
        nodes=[
            GraphNode(
                node_id="n1",
                action_node=ActionNode(
                    action_type="validate",
                    conditions=[
                        ConditionNode(
                            rule_id="r1",
                            operator_node=OperatorNode(
                                operator_name="equals",
                                field="sku",
                                target_value="TEST-1"
                            ),
                            message="SKU matches"
                        )
                    ]
                )
            )
        ]
    )
    res = runtime.execute(graph, {}, {"sku": "TEST-1"}, tenant_id="TENANT_ALPHA")
    assert res["tenant_id"] == "TENANT_ALPHA"
    assert "r1" in res["violations"]
