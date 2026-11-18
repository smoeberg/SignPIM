from engine.compiler import SchemaCompiler

def test_inline_rule_compilation():
    raw_wf = {
        "name": "TestInline",
        "entity": "Product",
        "steps": [
            {
                "action": "transform",
                "rules": [{"field": "price", "op": "multiply", "value": 1.25}]
            }
        ]
    }
    raw_rules = {}
    ast = SchemaCompiler.compile_workflow(raw_wf, raw_rules)
    assert ast is not None
    assert len(ast.actions) == 1
    assert len(ast.actions[0].conditions) == 1
    assert ast.actions[0].conditions[0].operator_node.operator_name == "multiply"



def test_operator_and_config_survive_workflow_compilation():
    raw_wf = {
        "name": "ConfiguredOperator",
        "entity": "Product",
        "steps": [{
            "action": "ai_images",
            "operator": "ai_resolve_images",
            "config": {"mode": "generate", "allowed": ["image"]},
        }],
    }
    ast = SchemaCompiler.compile_workflow(raw_wf, {})
    action = ast.actions[0]
    assert action.action_type == "ai_images"
    assert action.operator == "ai_resolve_images"
    assert action.config == {"mode": "generate", "allowed": ["image"]}


def test_enrich_sync_yaml_has_distinct_operator_configs():
    import yaml
    with open("meta/workflow/enrich_sync.yaml", encoding="utf-8") as stream:
        workflow = yaml.safe_load(stream)
    enrich, images = workflow["steps"][1:3]
    assert enrich["operator"] == "llm_enrich"
    assert enrich["config"]["allowed_fields"]
    assert images["operator"] == "ai_resolve_images"
    assert images["config"] == {"mode": "generate"}
