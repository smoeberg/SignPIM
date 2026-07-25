import os
from engine.kernel import PlatformKernel
from core.repository import Repository
from core.database import GenericDBService

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    meta_dir = os.path.join(base_dir, 'meta')

    db = GenericDBService()
    repo = Repository(db)

    # Initialize Platform Kernel
    kernel = PlatformKernel(meta_dir, repository=repo)

    test_product = {
        "sku": "TEST-100",
        "name": "Luxury Sofa",
        "price": 250.0,
        "images": ["sofa.jpg"]
    }

    result = kernel.run_workflow("Full_Sync", test_product, tenant_id="TENANT_ALPHA")
    print("\nWorkflow Execution Result:", result)

if __name__ == "__main__":
    main()
