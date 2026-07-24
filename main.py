import os
from engine.runtime import SignalementEngine
from core.repository import Repository
from core.database import DBService

def main():
    print("========================================")
    print("   SIGNALEMENT v6.2 - META RUNTIME     ")
    print("========================================\n")
    
    # 1. Setup Infrastructure
    db = DBService(os.getenv("DATABASE_URL"))
    repo = Repository(db)
    
    # 2. Initialize Generic Engine
    base_dir = os.path.dirname(os.path.abspath(__file__))
    engine = SignalementEngine(os.path.join(base_dir, 'meta'), repository=repo)
    
    # 3. Data with Context
    product = {
        "sku": "V62-META-99",
        "name": "Generic Sofa",
        "price": 500,
        "images": None, # Violation
        "ean": "12345" # Violation
    }
    context = {"market_avg": 450}
    
    # 4. Run Workflow
    try:
        engine.execute_workflow("Full_Sync", product, tenant_id="T_GENERIC", context=context)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
