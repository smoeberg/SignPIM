import os
import pytest
from engine.kernel import PlatformKernel
from core.repository import Repository
from core.database import GenericDBService

def test_engine_full_workflow():
    db = GenericDBService()
    repo = Repository(db)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kernel = PlatformKernel(os.path.join(base_dir, 'meta'), repository=repo)
    
    payload = {
        "sku": "TEST-SKU-1",
        "name": "Test Sofa",
        "price": 99.0,
        "images": ["img1.jpg"]
    }
    
    res = kernel.run_workflow("Full_Sync", payload, tenant_id="TENANT_TEST")
    assert res is not None
    assert "data" in res
    assert res["tenant_id"] == "TENANT_TEST"
