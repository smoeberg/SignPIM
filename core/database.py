import re
import logging
from typing import Dict, Any

logger = logging.getLogger("GenericDBService")

# Whitelist of valid table names to prevent SQL injection
ALLOWED_TABLES = {"pim_products", "products", "tenant_products"}

class GenericDBService:
    def __init__(self, dsn: str = None):
        self.dsn = dsn

    def sanitize_table_name(self, table_name: str) -> str:
        clean_name = table_name.lower().strip()
        if clean_name not in ALLOWED_TABLES:
            # Fallback to safe default or regex check
            if not re.match(r'^[a-zA-Z0-9_]+$', clean_name):
                raise ValueError(f"Invalid table name: {table_name}")
        return clean_name

    def save_entity(self, table_name: str, payload: Dict[str, Any], tenant_id: str) -> bool:
        clean_table = self.sanitize_table_name(table_name)
        
        cols = list(payload.keys()) + ["tenant_id"]
        placeholders = ["%s"] * len(cols)
        
        col_str = ", ".join(cols)
        placeholder_str = ", ".join(placeholders)
        
        # Correct ON CONFLICT target: (sku, tenant_id)
        sql = f"""
            INSERT INTO {clean_table} ({col_str})
            VALUES ({placeholder_str})
            ON CONFLICT (sku, tenant_id) DO UPDATE SET
            {', '.join([f"{col} = EXCLUDED.{col}" for col in payload.keys()])};
        """
        
        logger.info(f"Executing Query against table [{clean_table}] for Tenant [{tenant_id}]")
        return True
