import logging
from typing import Dict, Any, Optional
from engine.loader import MetadataLoader
from engine.compiler import SchemaCompiler
from engine.planner import ExecutionPlanner, ExecutionGraph
from engine.runtime import PureGraphRuntime

logger = logging.getLogger("PlatformKernel")

class PlatformKernel:
    def __init__(self, meta_dir: str, repository: Optional[Any] = None):
        self.loader = MetadataLoader(meta_dir)
        self.raw_meta = self.loader.load()
        self.repository = repository
        self.runtime = PureGraphRuntime(repository)
        self.compiled_graphs: Dict[str, ExecutionGraph] = {}
        self.compiled_entities: Dict[str, Dict[str, Any]] = {}
        
        self.bootstrap()

    def bootstrap(self):
        logger.info("PlatformKernel: Bootstrapping and compiling metadata...")
        
        # Register Entities
        for k, v in self.raw_meta['entities'].items():
            self.compiled_entities[k.lower()] = v
            self.compiled_entities[v['name'].lower()] = v

        # Compile & Plan Workflows into Execution Graph Artifacts
        for k, raw_wf in self.raw_meta['workflow'].items():
            ast = SchemaCompiler.compile_workflow(raw_wf, self.raw_meta['rules'])
            graph = ExecutionPlanner.build_graph(ast)
            
            self.compiled_graphs[k.lower()] = graph
            self.compiled_graphs[ast.name.lower()] = graph

        logger.info(f"PlatformKernel: Successfully compiled {len(self.compiled_graphs)} Execution Graphs.")

    def run_workflow(self, workflow_name: str, payload: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
        graph = self.compiled_graphs.get(workflow_name.lower())
        if not graph:
            raise ValueError(f"ExecutionGraph for workflow '{workflow_name}' not found.")
        
        entity_meta = self.compiled_entities.get(graph.entity_name.lower())
        return self.runtime.execute(graph, entity_meta, payload, tenant_id)
