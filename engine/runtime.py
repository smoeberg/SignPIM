import yaml, os, glob, re

class SignalementEngine:
    def __init__(self, meta_dir, db_service=None):
        self.db = db_service
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
        try:
            # Safer regex-based evaluation bridge
            def get_val(match):
                key = match.group(1).strip("'").strip('"')
                val = data.get(key)
                return str(val) if val is not None else "None"
            processed = re.sub(r"data\.get\((['\"].*?['\"])\)", get_val, condition)
            return eval(processed, {"__builtins__": {}}, {"None": None})
        except: return False

    def execute_workflow(self, workflow_name, data, tenant_id=None):
        if not tenant_id: raise PermissionError("Tenant isolation required.")
        wf = self.meta['workflow'][workflow_name.lower()]
        print(f"\n>>> Executing Workflow: {wf['name']} (Tenant: {tenant_id})")
        ctx = {"data": data, "violations": [], "tenant_id": tenant_id}
        for step in wf['steps']:
            action = step['action']
            if action == 'validate':
                for rid in step.get('rules', []):
                    rule = self.meta['rules'].get(rid.lower())
                    if rule and self.safe_eval(rule['condition'], ctx['data']):
                        print(f"    [FAIL] {rule['id']}")
                        ctx['violations'].append(rule)
            elif action == 'calculate_quality':
                q_cfg = self.meta['rules']['quality']['scoring_model']
                weights = q_cfg['weights']
                comp = 100 - (len(ctx['violations']) * 20)
                acc = 0 if (not ctx['data'].get('sot_timestamp') and q_cfg['behavior']['missing_source_of_truth'] == "fail_accuracy") else 100
                score = (comp * weights['completeness'] + 100 * weights['consistency'] + acc * weights['accuracy']) / 100
                ctx['quality_score'] = score
                print(f"    [Score] {score}%")
            elif action == 'persist' and self.db:
                self.db.save(ctx['data'], ctx['quality_score'], tenant_id)
        return ctx
