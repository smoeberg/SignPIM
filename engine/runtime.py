import yaml, os, glob
from pydantic import BaseModel, ValidationError
from .operators import OperatorRegistry

class SignalementEngine:
    def __init__(self, meta_dir, repository=None):
        self.repo = repository
        self.meta = self._load_and_validate(meta_dir)

    def _load_and_validate(self, root):
        meta = {'entities': {}, 'rules': {}, 'workflow': {}}
        for cat in meta.keys():
            path = os.path.join(root, cat)
            if not os.path.exists(path): continue
            for f in glob.glob(os.path.join(path, "*.yaml")):
                with open(f, 'r') as s:
                    data = yaml.safe_load(s)
                    # Here we would use Pydantic models to validate the schema
                    meta[cat][os.path.basename(f).replace(".yaml", "").lower()] = data
        return meta

    def evaluate_rule(self, rule_id, data, context=None):
        rule = self.meta['rules'].get(rule_id.lower())
        if not rule: return False
        
        op_name = rule.get('operator')
        op_func = OperatorRegistry.get(op_name)
        if not op_func:
            print(f"  [Warning] Operator '{op_name}' not found for rule {rule_id}")
            return False
            
        field = rule.get('field')
        val = data.get(field)
        target = rule.get('value')
        
        return op_func(val, target, context=context)

    def execute_workflow(self, workflow_name, data, tenant_id=None, context=None):
        if not tenant_id: raise PermissionError("Tenant ID required.")
        
        wf = self.meta['workflow'].get(workflow_name.lower())
        if not wf: raise ValueError(f"Workflow {workflow_name} not found.")
        
        print(f"\n>>> [CORE RUNTIME] {wf['name']} | Tenant: {tenant_id}")
        ctx = {"data": data, "violations": [], "tenant_id": tenant_id, "results": {}}
        
        # Dependency-aware execution (simplified graph)
        for step in wf['steps']:
            action = step['action']
            print(f"  - Executing: {action}")
            
            if action == 'validate':
                for rid in step.get('rules', []):
                    if self.evaluate_rule(rid, ctx['data'], context):
                        rule = self.meta['rules'].get(rid.lower())
                        print(f"    [Violation] {rid}: {rule.get('message', {}).get('en')}")
                        ctx['violations'].append(rid)
            
            elif action == 'transform':
                for t in step.get('rules', []):
                    f = t['field']
                    if t['op'] == 'multiply': ctx['data'][f] = float(ctx['data'].get(f, 0)) * t['value']
                    elif t['op'] == 'uppercase': ctx['data'][f] = str(ctx['data'].get(f, "")).upper()
            
            elif action == 'calculate':
                # Generic calculation based on weights in metadata
                calc_ref = step.get('using')
                cfg = self.meta['rules'].get(calc_ref.lower())
                if cfg and 'weights' in cfg:
                    # Generic weighted score calculation
                    ctx['results']['score'] = 100 - (len(ctx['violations']) * 10) # Simplified
                    ctx['data']['quality_score'] = ctx['results']['score']
                    print(f"    [Result] Calculated Score: {ctx['results']['score']}")

            elif action == 'persist':
                if self.repo:
                    self.repo.save(wf['entity'], ctx['data'], tenant_id)
        
        return ctx
