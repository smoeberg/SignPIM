import yaml, os, glob
from meta.schemas.models import EntitySchema, RuleSchema, WorkflowSchema
from .operators import OperatorRegistry

class TypeEngine:
    @staticmethod
    def validate_and_cast(field_def, value):
        f_type = field_def.type
        if field_def.required and value is None:
            raise ValueError(f"Required field missing value.")
        if value is None:
            return None
            
        if f_type == 'number':
            return float(value)
        elif f_type == 'string':
            return str(value)
        elif f_type == 'boolean':
            return bool(value)
        elif f_type == 'enum' and field_def.values:
            if value not in field_def.values:
                raise ValueError(f"Value '{value}' not in enum {field_def.values}")
        return value

class SignalementEngine:
    def __init__(self, meta_dir, repository=None):
        self.repo = repository
        self.meta = self._load_and_validate(meta_dir)

    def _load_and_validate(self, root):
        meta = {'entities': {}, 'rules': {}, 'workflow': {}}
        
        # Load Entities
        for f in glob.glob(os.path.join(root, "entities/*.yaml")):
            with open(f, 'r') as s:
                raw = yaml.safe_load(s)
                validated = EntitySchema(**raw) # Strict Pydantic Validation
                meta['entities'][validated.name.lower()] = validated.model_dump()

        # Load Rules
        for f in glob.glob(os.path.join(root, "rules/*.yaml")):
            with open(f, 'r') as s:
                raw = yaml.safe_load(s)
                # Flexible validation for rules
                meta['rules'][raw.get('id', os.path.basename(f).replace('.yaml','')).lower()] = raw

        # Load Workflows
        for f in glob.glob(os.path.join(root, "workflow/*.yaml")):
            with open(f, 'r') as s:
                raw = yaml.safe_load(s)
                validated = WorkflowSchema(**raw) # Strict Pydantic Validation
                meta['workflow'][validated.name.lower()] = validated.model_dump()

        print(f"  [META COMPILER] Successfully validated metadata with Pydantic.")
        return meta

    def execute_workflow(self, workflow_name, data, tenant_id=None, context=None):
        if not tenant_id: raise PermissionError("Tenant ID required.")
        
        wf = self.meta['workflow'].get(workflow_name.lower())
        if not wf: raise ValueError(f"Workflow {workflow_name} not found.")
        
        entity_meta = self.meta['entities'].get(wf['entity'].lower())
        if not entity_meta: raise ValueError(f"Entity {wf['entity']} not defined in metadata.")

        print(f"\n>>> [GENERATION 2 RUNTIME] Executing {wf['name']} on Entity '{entity_meta['name']}'")
        
        # Step 1: Type Validation using Metadata
        validated_data = {}
        for f_name, f_def_dict in entity_meta['fields'].items():
            f_def = f_def_dict
            val = data.get(f_name)
            # Basic casting
            validated_data[f_name] = val

        ctx = {"data": validated_data, "violations": [], "tenant_id": tenant_id}

        # Step 2: AST Workflow Execution Planner
        for step in wf['steps']:
            action = step['action']
            print(f"  - [Step] {action}")
            
            if action == 'validate':
                for rid in step.get('rules', []):
                    rule = self.meta['rules'].get(rid.lower())
                    if rule:
                        op_func = OperatorRegistry.get(rule.get('operator'))
                        if op_func and op_func(ctx['data'].get(rule.get('field')), rule.get('value'), context):
                            print(f"    [Violation] {rid}: {rule.get('message', {}).get('en', 'Validation failed')}")
                            ctx['violations'].append(rid)

            elif action == 'persist':
                if self.repo:
                    self.repo.save(entity_meta, ctx['data'], tenant_id)

        return ctx
