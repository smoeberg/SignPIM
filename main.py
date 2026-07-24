import os
from engine.runtime import SignalementEngine
from core.database import DBService
def main():
    print("SIGNALEMENT v6.1 - FUNCTIONAL CORE\n")
    db = DBService()
    engine = SignalementEngine(os.path.join(os.path.dirname(__file__), 'meta'), db_service=db)
    product = {"sku": "V61-001", "images": None, "sot_timestamp": None}
    engine.execute_workflow("Import", product, tenant_id="ALPHA-1")
if __name__ == "__main__":
    main()
