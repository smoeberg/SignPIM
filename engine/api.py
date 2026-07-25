import os
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Dict, Any, Optional
from engine.kernel import PlatformKernel
from core.repository import Repository
from core.database import GenericDBService

app = FastAPI(title="Signalement Python Engine API", version="6.1.0")

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
meta_dir = os.path.join(base_dir, 'meta')
db = GenericDBService()
repo = Repository(db)
kernel = PlatformKernel(meta_dir, repository=repo)

class WorkflowExecutionRequest(BaseModel):
    workflow_name: str
    payload: Dict[str, Any]

@app.post("/api/v1/execute")
def execute_workflow(req: WorkflowExecutionRequest, x_tenant_id: Optional[str] = Header(None)):
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-ID header required")
    try:
        res = kernel.run_workflow(req.workflow_name, req.payload, tenant_id=x_tenant_id)
        return {"status": "success", "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
