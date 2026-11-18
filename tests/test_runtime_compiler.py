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
    with pytest.raises(ValueError):
        handler.cast_and_validate("invalid_number", {"type": "number"})

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
                                operator_name="empty",
                                field="images",
                                target_value=None
                            ),
                            message="Missing images"
                        )
                    ]
                )
            )
        ]
    )
    res = runtime.execute(graph, {}, {"sku": "TEST-1", "images": []}, tenant_id="TENANT_ALPHA")
    assert res["tenant_id"] == "TENANT_ALPHA"
    assert "r1" in res["violations"]



def test_runtime_executes_ai_images_operator_and_passes_config():
    from engine.operators.registry import RichOperatorRegistry

    seen = {}

    def fake_image_operator(data, ctx):
        seen["config"] = dict(ctx["operator_config"])
        result = dict(data)
        result["image_proposal"] = "candidate"
        return result

    RichOperatorRegistry.register("test_image_operator", fake_image_operator, category="ai")
    graph = ExecutionGraph(
        workflow_name="ImageWorkflow",
        entity_name="Product",
        nodes=[GraphNode(
            node_id="image_1",
            action_node=ActionNode(
                action_type="ai_images",
                operator="test_image_operator",
                config={"mode": "match", "threshold": 0.95},
            ),
        )],
    )
    result = PureGraphRuntime().execute(
        graph, {}, {"sku": "P-1"}, tenant_id="tenant-1"
    )
    assert seen["config"] == {"mode": "match", "threshold": 0.95}
    assert result["data"]["image_proposal"] == "candidate"
