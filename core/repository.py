class Repository:
    def __init__(self, storage_adapter):
        self.adapter = storage_adapter

    def save(self, entity_meta, data, tenant_id):
        """Saves data strictly according to Entity Metadata. Completely domain-agnostic."""
        table_name = entity_meta['storage']['table_name']
        valid_fields = entity_meta['fields'].keys()
        
        # Filter payload to only include fields defined in metadata
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        
        return self.adapter.save_entity(table_name, filtered_data, tenant_id)
