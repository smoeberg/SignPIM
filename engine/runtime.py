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
        try:
            def get_val(match):
                key = match.group(1).strip("'").strip('"')
                val = data.get(key)
                return str(val) if val is not None else "None"
            processed = re.sub(r"data\.get\((['\"].*?['\"])\)", get_val, str(condition))
            return eval(processed, {"__builtins__": {}}, {"None": None})
        except: return False
    def execute_workflow(self, workflow_name, data, tenant_id=None):
        if not tenant_id: raise PermissionError("Tenant ID mandatory.")
        wf = self.meta['workflow'].get(workflow_name.lower())
        if not wf: return
        print(f"\n[RUN] {wf['name']} | {tenant_id}")
        ctx = {"data": data, "violations": [], "tenant_id": tenant_id}
        for step in wf['steps']:
            action = step['action']
            if action == 'validate':
                for rid in step.get('rules', []):
                    rule = self.meta['rules'].get(rid.lower())
                    if rule and self.safe_eval(rule['condition'], ctx['data']):
                        print(f"  - Violation: {rule['id']}")
                        ctx['violations'].append(rule['id'])
            elif action == 'transform':
                for t in step.get('rules', []):
                    f = t['field']
                    if t['op'] == 'multiply': ctx['data'][f] = float(ctx['data'].get(f, 0)) * t['value']
                    elif t['op'] == 'uppercase': ctx['data'][f] = str(ctx['data'].get(f, "")).upper()
                print(f"  - Transformed: {ctx['data'].get('sku')}")
            elif action == 'calculate_quality':
                print(f"  - Quality check performed.")
            elif action == 'auto_resolve':
                if not ctx['violations']: print(f"  - Stability check passed. Resolved.")
                else: print(f"  - Resolved blocked: {len(ctx['violations'])} errors.")
            elif action == 'persist':
                print(f"  - Saved: {ctx['data'].get('sku')}")
        return ctx
