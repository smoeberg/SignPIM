import os
from engine.runtime import SignalementEngine
from core.database import DBService

def main():
    print("========================================")
    print("   SIGNALEMENT v6.1 - COMPLETE & SAFE   ")
    print("========================================\n")
    
    # Initialize Core Services
    db = DBService("postgresql://db.internal.signalement.dk:5432/pim")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    engine = SignalementEngine(os.path.join(base_dir, 'meta'), db=db)
    
    # Test Product
    product = {
        "sku": "LAMP-V61-OK",
        "name": "desk lamp",
        "price": 100,
        "images": ["lamp.jpg"],
        "sot_timestamp": "2026-07-24"
    }
    
    # Execute full workflow
    engine.execute_workflow("Full_Sync", product, tenant_id="TENANT_PROD_1")

if __name__ == "__main__":
    main()
