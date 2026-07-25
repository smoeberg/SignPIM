import yaml, os, glob, logging
from typing import Dict, Any, Optional
from meta.schemas.models import EntitySchema, WorkflowSchema
from engine.types import TypeRegistry
from engine.operators.registry import RichOperatorRegistry
from engine.compiler import SchemaCompiler
from engine.planner import ExecutionPlanner

logger = logging.getLogger("SignalementEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

class SignalementEngine:
    def __init__(self, meta_dir: str, repository: Optional[Any] = None) -> None:
        self.repo = repository
        self.raw_meta: Dict[str, Any] = self._load_all(meta_dir)
        self.compiled_ast: Dict[str, Any] = self._compile_all()

    def _load_all(self, root: str) -> Dict[str, Any]:
        meta = {'entities': {}, 'rules': {}, 'workflow': {}}
        for cat in meta.keys():
            path = os.path.join(root, cat)
            if not os.path.exists(path): continue
            for f in glob.glob(os.path.join(path, "*.yaml")):
                try:
                    with open(f, 'r', encoding='utf-8') as s:
                        data = yaml.safe_load(s)
                        meta[cat][os.path.basename(f).replace('.yaml', '').lower()] = data
                except Exception as e:
                    logger.error(f"Failed to parse YAML file {f}: {e}")
        return meta

    def _compile_all(self) -> Dict[str, Any]:
        compiled = {'entities': {}, 'workflows': {}, 'graphs': {}}
        
        for name, raw in self.raw_meta['entities'].items():
            schema = EntitySchema(**raw)
            compiled['entities'][schema.name.lower()] = schema.model_dump()
            compiled['entities'][name.lower()] = schema.model_dump()

        for name, raw in self.raw_meta['workflow'].items():
            schema = WorkflowSchema(**raw)
            ast = SchemaCompiler.compile_workflow(schema, self.raw_meta['rules'])
            graph = ExecutionPlanner.build_execution_graph(ast)
            
            compiled['workflows'][ast.name.lower()] = ast
            compiled['workflows'][name.lower()] = ast
            compiled['graphs'][ast.name.lower()] = graph
            compiled['graphs'][name.lower()] = graph

        logger.info("Metadata successfully compiled into Execution Graphs.")
        return compiled

    def execute_workflow(self, workflow_name: str, data: Dict[str, Any], tenant_id: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not tenant_id: 
            raise PermissionError("Tenant ID required for isolation.")
        
        graph = self.compiled_ast['graphs'].get(workflow_name.lower())
        if not graph: 
            raise ValueError(f"Workflow '{workflow_name}' graph not found.")
        
        ast_wf = self.compiled_ast['workflows'].get(workflow_name.lower())
        entity_meta = self.compiled_ast['entities'].get(ast_wf.entity_name.lower())

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

        ctx: Dict[str, Any] = {"data": typed_payload, "violations": [], "tenant_id": tenant_id}

        for node in graph.nodes:
            logger.info(f"Node Execution [{node.node_id}] Action: {node.action}")

            if node.action == 'validate':
                for rid in node.rules:
                    rule_key = str(rid).lower()
                    rule = self.raw_meta['rules'].get(rule_key)
                    if rule:
                        op_def = RichOperatorRegistry.get(rule.get('operator'))
                        if op_def:
                            val = ctx['data'].get(rule.get('field'))
                            if op_def.fn(val, rule.get('value'), context):
                                msg = rule.get('message', 'Rule failed')
                                msg_str = msg.get('en', str(msg)) if isinstance(msg, dict) else str(msg)
                                logger.warning(f"Violation Detected [{rule_key}]: {msg_str}")
                                ctx['violations'].append(rule_key)

            elif node.action == 'persist':
                if self.repo and entity_meta:
                    self.repo.save(entity_meta, ctx['data'], tenant_id)

        return ctx
