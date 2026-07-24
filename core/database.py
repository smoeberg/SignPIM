import os
import json

class GenericDBService:
    def __init__(self, connection_string=None):
        self.conn_str = connection_string or os.getenv("DATABASE_URL")
        print("  [DB] Generic Storage Adapter initialized.")

    def save_entity(self, table_name, fields_data, tenant_id):
        """Dynamic, fully metadata-driven SQL generator. Knows NO domain entities."""
        columns = list(fields_data.keys()) + ['tenant_id']
        placeholders = ['%s'] * len(columns)
        
        col_names = ", ".join(columns)
        col_placeholders = ", ".join(placeholders)
        
        sql = f"INSERT INTO {table_name} ({col_names}) VALUES ({col_placeholders}) " \
              f"ON CONFLICT (tenant_id) DO NOTHING;"
              
        values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields_data.values()]
        values.append(tenant_id)
        
        print(f"  [STORAGE ADAPTER] Dynamic SQL Generated for Table '{table_name}':")
        print(f"    Columns: {columns}")
        print(f"    Tenant: {tenant_id}")
        return True
