class DBService:
    def __init__(self, connection_string=None):
        self.conn = connection_string
        print("  [DB] DBService initialized")

    def save(self, data, score, tenant_id):
        sql = f"INSERT INTO pim_products (sku, name, price, quality_score, tenant_id) " \
              f"VALUES ('{data.get('sku')}', '{data.get('name')}', {data.get('price', 0)}, {score}, '{tenant_id}') " \
              f"ON CONFLICT (sku, tenant_id) DO UPDATE SET quality_score = EXCLUDED.quality_score;"
        print(f"  [DB PERSIST] Executed: {sql}")
        return True
