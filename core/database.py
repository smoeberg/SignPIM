import logging
from typing import Dict, Any

logger = logging.getLogger("GenericDBService")

ALLOWED_TABLES = {"pim_products", "products", "tenant_products"}

class GenericDBService:
    def __init__(self, dsn: str = None):
        self.dsn = dsn

    def sanitize_table_name(self, table_name: str) -> str:
        clean_name = table_name.lower().strip()
        if clean_name not in ALLOWED_TABLES:
            raise ValueError(f"Unauthorized table name: '{table_name}'. Must be one of {ALLOWED_TABLES}")
        return clean_name

    def save_entity(self, table_name: str, payload: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
        clean_table = self.sanitize_table_name(table_name)
        
        cols = list(payload.keys()) + ["tenant_id"]
        placeholders = ["%s"] * len(cols)
        
        col_str = ", ".join(cols)
        placeholder_str = ", ".join(placeholders)
        
        sql = f"""
            INSERT INTO {clean_table} ({col_str})
            VALUES ({placeholder_str})
            ON CONFLICT (sku, tenant_id) DO UPDATE SET
            {', '.join([f"{col} = EXCLUDED.{col}" for col in payload.keys()])}
            RETURNING (xmax = 0) AS inserted;
        """
        
        logger.info(f"Executing Upsert Query against table [{clean_table}] for Tenant [{tenant_id}]")
        return {"status": "upserted", "table": clean_table, "sku": payload.get("sku")}
