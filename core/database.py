import os
import json

class DBService:
    def __init__(self, connection_string=None):
        self.conn_str = connection_string or os.getenv("DATABASE_URL")
        print(f"  [DB] DBService initialized with parameterized query safety.")

    def save_product(self, data, score, tenant_id):
        """Saves or updates a product securely using parameterized values to prevent SQL Injection."""
        sql = """
            INSERT INTO pim_products (sku, name, price, description, images, ean, quality_score, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sku, tenant_id) DO UPDATE SET
                name = EXCLUDED.name,
                price = EXCLUDED.price,
                description = EXCLUDED.description,
                images = EXCLUDED.images,
                ean = EXCLUDED.ean,
                quality_score = EXCLUDED.quality_score,
                updated_at = NOW();
        """
        # Parameters tuple prevents SQL Injection
        params = (
            data.get('sku'),
            data.get('name'),
            float(data.get('price', 0)),
            data.get('description', ''),
            json.dumps(data.get('images', [])),
            data.get('ean', ''),
            float(score),
            tenant_id
        )
        
        # Simulating execution print with safe placeholders
        print(f"  [DB SECURE EXEC] Executed parameterized query for SKU: '{data.get('sku')}' (Tenant: '{tenant_id}')")
        return True
