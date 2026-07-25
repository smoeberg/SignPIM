import os, glob, yaml, logging
from typing import Dict, Any
from meta.schemas.models import EntitySchema, WorkflowSchema

logger = logging.getLogger("MetadataLoader")

class MetadataLoader:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir

    def load(self) -> Dict[str, Any]:
        meta = {'entities': {}, 'rules': {}, 'workflow': {}}
        for cat in meta.keys():
            path = os.path.join(self.root_dir, cat)
            if not os.path.exists(path): continue
            for f in glob.glob(os.path.join(path, "*.yaml")):
                try:
                    with open(f, 'r', encoding='utf-8') as s:
                        data = yaml.safe_load(s)
                        key = os.path.basename(f).replace('.yaml', '').lower()
                        meta[cat][key] = data
                except Exception as e:
                    logger.error(f"Error loading {f}: {e}")
        return meta
