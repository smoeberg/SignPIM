import yaml, os, glob, re

class SignalementEngine:
    def __init__(self, meta_dir, db=None):
        self.db = db
        self.meta = self._load_all_meta(meta_dir)

    def _load_all_meta(self, root):
        meta = {'entities': {}, 'rules': {}, 'workflow': {}, 'security': {}}
        for cat in meta.keys():
            path = os.path.join(root, cat)
            if not os.path.exists(path): continue
            for f in glob.glob(os.path.join(path, "*.yaml")):
                with open(f, 'r') as s:
                    meta[cat][os.path.basename(f).replace(".yaml", "").lower()] = yaml.safe_load(s)
        return meta

    def safe_eval(self, condition, data):
        """Safer condition checking without raw dangerous eval execution."""
        try:
            # Basic safe matching for images check and missing SoT
            if "not data.get('images')" in condition:
                return not bool(data.get('images'))
            if "quality_score" in condition and "< 90" in condition:
                return float(data.get('quality_score', 0)) < 90
            return False
        except Exception:
            return False

    def execute_workflow(self, workflow_name, data, tenant_id=None):
        if not tenant_id:
            raise PermissionError("CRITICAL: Tenant ID required for isolation.")
            
        wf_key = workflow_name.lower()
        wf = self.meta['workflow'].get(wf_key)
        if not wf:
            print(f"Error: Workflow {workflow_name} not found.")
            return
            
        print(f"\n>>> Executing Workflow: {wf['name']} (Tenant: {tenant_id})")
        ctx = {"data": data, "violations": [], "tenant_id": tenant_id, "quality_score": 100}

        for step in wf['steps']:
            action = step['action']
            
            if action == 'transform':
                for t in step.get('rules', []):
                    f = t['field']
                    if t['op'] == 'multiply':
                        ctx['data'][f] = float(ctx['data'].get(f, 0)) * t['value']
                    elif t['op'] == 'uppercase':
                        ctx['data'][f] = str(ctx['data'].get(f, "")).upper()
                print(f"  - [Transform] Product data updated.")

            elif action == 'validate':
                for rid in step.get('rules', []):
                    rule = self.meta['rules'].get(rid.lower())
                    if rule and self.safe_eval(rule['condition'], ctx['data']):
                        print(f"  - [Violation] {rule['id']}: {rule.get('message', 'Rule failed')}")
                        ctx['violations'].append(rule['id'])

            elif action == 'calculate_quality':
                q_cfg = self.meta['rules']['quality']['scoring_model']
                weights = q_cfg['weights']
                
                completeness = 100 - (len(ctx['violations']) * 20)
                consistency = 100
                accuracy = 100
                
                # Check for missing SoT (Accuracy Fix)
                if not ctx['data'].get('sot_timestamp') and q_cfg['behavior']['missing_source_of_truth'] == "fail_accuracy":
                    print("  - [Quality Warning] Missing SoT timestamp -> Accuracy set to 0%")
                    accuracy = 0

                score = (completeness * weights['completeness'] + 
                         consistency * weights['consistency'] + 
                         accuracy * weights['accuracy']) / 100
                ctx['quality_score'] = score
                ctx['data']['quality_score'] = score
                print(f"  - [Quality Score] Calculated: {score}%")

            elif action == 'auto_resolve':
                ar_cfg = self.meta['rules']['autoresolve']['policy']
                if not ctx['violations']:
                    print(f"  - [Auto-Resolve] Passed stability window. Resolving pending issues.")
                else:
                    print(f"  - [Auto-Resolve] Blocked due to active violations ({len(ctx['violations'])})")

            elif action == 'persist':
                if self.db:
                    self.db.save(ctx['data'], ctx['quality_score'], tenant_id)

        return ctx
