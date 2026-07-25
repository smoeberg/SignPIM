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
