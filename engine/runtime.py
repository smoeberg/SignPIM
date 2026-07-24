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

    def evaluate_rule(self, rule, data):
        """Generic, secure rule evaluator that dispatches operators without raw eval()."""
        field = rule.get('field')
        op = rule.get('operator')
        target_val = rule.get('value')
        val = data.get(field) if field else None

        # Custom logic dispatching based on rule structure
        if op == 'empty':
            return not bool(val)
        elif op == 'lte':
            return float(val or 0) <= float(target_val)
        elif op == 'min_length':
            return len(str(val or '')) < int(target_val)
        elif op == 'not_in':
            return val not in target_val if val else False
        elif op == 'ean13_check':
            if not val: return False
            val_str = str(val).strip()
            return not (len(val_str) == 13 and val_str.isdigit())
        
        # Fallback for legacy string conditions
        cond = rule.get('condition', '')
        if "quality_score" in cond and "< 90" in cond:
            return float(data.get('quality_score', 0)) < 90
            
        return False

    def execute_workflow(self, workflow_name, data, tenant_id=None):
        if not tenant_id:
            raise PermissionError("CRITICAL: Tenant isolation required.")
            
        wf_key = workflow_name.lower()
        wf = self.meta['workflow'].get(wf_key)
        if not wf:
            print(f"Error: Workflow '{workflow_name}' not found.")
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
                print(f"  - [Transform] Completed.")

            elif action == 'validate':
                for rid in step.get('rules', []):
                    rule = self.meta['rules'].get(rid.lower())
                    if rule and self.evaluate_rule(rule, ctx['data']):
                        print(f"  - [Violation Detected] {rule['id']}: {rule.get('message')}")
                        ctx['violations'].append(rule['id'])

            elif action == 'calculate_quality':
                q_cfg = self.meta['rules']['quality']['scoring_model']
                weights = q_cfg['weights']
                
                completeness = 100 - (len(ctx['violations']) * 15)
                consistency = 100
                accuracy = 100
                
                if not ctx['data'].get('sot_timestamp') and q_cfg['behavior']['missing_source_of_truth'] == "fail_accuracy":
                    print("  - [Quality Warning] Missing Source of Truth timestamp.")
                    accuracy = 0

                score = max(0, (completeness * weights['completeness'] + 
                                consistency * weights['consistency'] + 
                                accuracy * weights['accuracy']) / 100)
                ctx['quality_score'] = score
                ctx['data']['quality_score'] = score
                print(f"  - [Quality Score] {score}%")

            elif action == 'persist':
                if self.db:
                    self.db.save_product(ctx['data'], ctx['quality_score'], tenant_id)

        return ctx
