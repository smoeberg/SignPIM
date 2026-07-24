import os
from engine.runtime import SignalementEngine
from core.database import DBService

def main():
    print("========================================")
    print("   SIGNALEMENT v6.1 - PRODUCTION READY ")
    print("========================================\n")
    
    # Externalize DSN to environment variable (Fallback for local dev)
    db_url = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/pim_db")
    db = DBService(db_url)
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    engine = SignalementEngine(os.path.join(base_dir, 'meta'), db=db)
    
    # Test product with multiple validation triggers
    product = {
        "sku": "PROD-2026-X",
        "name": "enterprise server rack",
        "price": 0, # Should trigger invalid_price rule
        "description": "Short", # Should trigger short_description rule
        "images": [], # Should trigger missing_images rule
        "ean": "12345", # Should trigger valid_ean_13 rule
        "sot_timestamp": "2026-07-24"
    }
    
    # Execute Full Sync Workflow
    engine.execute_workflow("Full_Sync", product, tenant_id="TENANT_ENTERPRISE_1")

if __name__ == "__main__":
    main()
