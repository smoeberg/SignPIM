import os
from engine.runtime import SignalementEngine
from core.repository import Repository
from core.database import GenericDBService

def main():
    print("==================================================")
    print("   SIGNALEMENT v6.2 - GENERATION 2 PLATFORM      ")
    print("==================================================\n")
    
    # 1. Fully Generic Infrastructure
    db = GenericDBService(os.getenv("DATABASE_URL"))
    repo = Repository(db)
    
    # 2. Pydantic-Validated Engine
    base_dir = os.path.dirname(os.path.abspath(__file__))
    engine = SignalementEngine(os.path.join(base_dir, 'meta'), repository=repo)
    
    # 3. Payload
    payload = {
        "sku": "GEN-2026-SOFA",
        "name": "Design Chair",
        "price": 1200.0,
        "images": None # Triggers violation
    }
    
    # 4. Execution
    engine.execute_workflow("Full_Sync", payload, tenant_id="TENANT_G2")

if __name__ == "__main__":
    main()
