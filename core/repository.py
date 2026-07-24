class Repository:
    def __init__(self, db_service):
        self.db = db_service

    def save(self, entity_name, data, tenant_id):
        # Maps entity to table based on metadata would go here
        print(f"    [Repository] Persisting {entity_name} for tenant {tenant_id}")
        return self.db.save_product(data, data.get('quality_score', 0), tenant_id)
