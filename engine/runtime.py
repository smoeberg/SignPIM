import yaml, os, glob
from meta.schemas.models import EntitySchema, WorkflowSchema
from engine.types import TypeRegistry
from engine.operators.registry import RichOperatorRegistry
from engine.compiler import SchemaCompiler
from engine.planner import ExecutionPlanner

class SignalementEngine:
    def __init__(self, meta_dir, repository=None):
        self.repo = repository
        self.raw_meta = self._load_all(meta_dir)
        self.compiled_ast = self._compile_all()

    def _load_all(self, root):
        meta = {'entities': {}, 'rules': {}, 'workflow': {}}
        for cat in meta.keys():
            path = os.path.join(root, cat)
            if not os.path.exists(path): continue
            for f in glob.glob(os.path.join(path, "*.yaml")):
                with open(f, 'r') as s:
                    data = yaml.safe_load(s)
                    meta[cat][os.path.basename(f).replace('.yaml', '').lower()] = data
        return meta

    def _compile_all(self):
        compiled = {'entities': {}, 'workflows': {}, 'graphs': {}}
        
        for name, raw in self.raw_meta['entities'].items():
            schema = EntitySchema(**raw)
            compiled['entities'][schema.name.lower()] = schema.model_dump()

        for name, raw in self.raw_meta['workflow'].items():
            schema = WorkflowSchema(**raw)
            ast = SchemaCompiler.compile_workflow(schema, self.raw_meta['rules'])
            graph = ExecutionPlanner.build_execution_graph(ast)
            
            compiled['workflows'][ast.name.lower()] = ast
            compiled['graphs'][ast.name.lower()] = graph

        print("  [PLATFORM RUNTIME] Metadata compiled into Execution Graphs.")
        return compiled

    def execute_workflow(self, workflow_name, data, tenant_id=None, context=None):
        if not tenant_id: raise PermissionError("Tenant ID required.")
        
        graph = self.compiled_ast['graphs'].get(workflow_name.lower())
        if not graph: raise ValueError(f"Workflow '{workflow_name}' graph not found.")
        
        ast_wf = self.compiled_ast['workflows'].get(workflow_name.lower())
        entity_meta = self.compiled_ast['entities'].get(ast_wf.entity_name.lower())

        print(f"\n>>> [GRAPH EXECUTOR] Executing Graph '{graph.workflow_name}' ({len(graph.nodes)} Nodes)")

        # Step 1: TypeRegistry Casting & Normalization
        typed_payload = {}
        for f_name, f_def in entity_meta['fields'].items():
            val = data.get(f_name)
            t_handler = TypeRegistry.get(f_def['type'])
            if t_handler:
                typed_payload[f_name] = t_handler.cast_and_validate(val, f_def)
            else:
                typed_payload[f_name] = val

        ctx = {"data": typed_payload, "violations": [], "tenant_id": tenant_id}

        # Step 2: Node Graph Execution (Planner Layer)
        for node in graph.nodes:
            print(f"  - [Node: {node.node_id}] Action: {node.action}")

            if node.action == 'validate':
                for rid in node.rules:
                    rule_key = str(rid).lower()
                    rule = self.raw_meta['rules'].get(rule_key)
                    if rule:
                        op_def = RichOperatorRegistry.get(rule.get('operator'))
                        if op_def:
                            val = ctx['data'].get(rule.get('field'))
                            if op_def.fn(val, rule.get('value'), context):
                                print(f"    [Violation] {rule_key}: {rule.get('message', {}).get('en', 'Rule failed')}")
                                ctx['violations'].append(rule_key)

            elif node.action == 'persist':
                if self.repo:
                    self.repo.save(entity_meta, ctx['data'], tenant_id)

        return ctx
