import yaml, os, glob
class SignalementEngine:
    def __init__(self, meta_dir):
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
    def execute_workflow(self, workflow_name, data, tenant_id=None):
        if not tenant_id: raise PermissionError("Tenant isolation required.")
        wf = self.meta['workflow'][workflow_name.lower()]
        ctx = {"data": data, "violations": [], "tenant_id": tenant_id}
        # Execution logic...
        return ctx
