from typing import Dict, Any

class MappingEngine:
    def map_to_canonical(self, raw_data: Dict[str, Any], mapping_rules: Dict[str, str]) -> Dict[str, Any]:
        canonical = {}
        for source_field, target_field in mapping_rules.items():
            if source_field in raw_data:
                canonical[target_field] = raw_data[source_field]
        return canonical
