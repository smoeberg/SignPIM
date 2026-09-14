"""
SignPIM REST API — the headless surface of the engine.

Endpoints:
  POST /tenants/{slug}/ingest        CSV feed ingestion (Feed-First)
  POST /products                     upsert a single product (runs full_sync)
  GET  /products/{sku}               fetch one product (with quality_score)
  GET  /products?min_score=75        list products filtered by quality
  GET  /quality/{tenant}             tenant-level 3D quality summary
  POST /rules                        add a tenant rule
  POST /mappings                     add a normalization mapping
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from core.persistence import PersistenceService
from engine.kernel import PlatformKernel
from services.ingestion import CSVIngestionService, IngestionError

logger = logging.getLogger("signpim.api")

app = FastAPI(title="SignPIM", version="0.1.0",
              description="Feed-First & Headless PIM for complex supplier data")

# --- singletons (swap DSN via env in prod) ---
import os
_DSN = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL") or "sqlite:///:memory:"
persistence = PersistenceService(dsn=_DSN)
kernel = PlatformKernel(meta_dir="meta")
kernel.bootstrap()
ingestion = CSVIngestionService(persistence, kernel)


def get_persistence() -> PersistenceService:
    return persistence


# ---------- schemas ----------
class ProductIn(BaseModel):
    sku: str = Field(min_length=1)
    name: str = Field(min_length=1)
    price: float = Field(gt=0)
    ean: Optional[str] = None
    images: Optional[List[str]] = None
    description: Optional[str] = None
    currency: Optional[str] = "DKK"

    model_config = {"extra": "allow"}  # allow undeclared fields (e.g. images variations)


class RuleIn(BaseModel):
    rule_id: str
    category: str = "consistency"
    severity: str = "info"
    parameters: Dict[str, Any] = {}
    global_rule: bool = False


class MappingIn(BaseModel):
    source_value: str
    normalized: str
    field: str


# ---------- health ----------
@app.get("/health", tags=["ops"])
def health():
    """Liveness/readiness probe used by Docker healthchecks & load balancers."""
    return {"status": "ok", "version": app.version}


# ---------- ingestion ----------
@app.post("/tenants/{slug}/ingest", tags=["ingestion"])
def ingest_csv(slug: str, body: str = Query(..., media_type="text/csv")):
    """Ingest a raw supplier CSV feed for a tenant (Feed-First front door)."""
    try:
        result = ingestion.ingest(body, slug)
    except IngestionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result


# ---------- products ----------
@app.post("/products", tags=["products"])
def upsert_product(product: ProductIn, tenant: str = "default",
                   persistence: PersistenceService = Depends(get_persistence)):
    """Upsert a single product through the full_sync engine workflow."""
    data = product.model_dump(exclude_none=True)
    result = kernel.run_workflow("full_sync", data, tenant_id=tenant)
    tenant_obj = persistence.get_or_create_tenant(tenant)
    saved = persistence.upsert_product(
        tenant_obj.id, product.sku, result["data"],
        quality_score=result["data"].get("quality_score"),
    )
    return {"saved": saved, "violations": result.get("violations", [])}


@app.get("/products/{sku}", tags=["products"])
def get_product(sku: str, tenant: str = "default",
                persistence: PersistenceService = Depends(get_persistence)):
    t = persistence.get_or_create_tenant(tenant)
    p = persistence.get_product(t.id, sku)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found")
    return p


@app.get("/products", tags=["products"])
def list_products(tenant: str = "default", min_score: Optional[float] = Query(None, ge=0, le=100),
                  limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                  persistence: PersistenceService = Depends(get_persistence)):
    t = persistence.get_or_create_tenant(tenant)
    return persistence.list_products(t.id, min_score=min_score, limit=limit, offset=offset)


# ---------- quality ----------
@app.get("/quality/{slug}", tags=["quality"])
def quality_summary(slug: str,
                    persistence: PersistenceService = Depends(get_persistence)):
    """Tenant-level 3D quality summary: completeness / consistency / accuracy."""
    t = persistence.get_or_create_tenant(slug)
    products = persistence.list_products(t.id, limit=100000)
    if not products:
        raise HTTPException(status_code=404, detail=f"No products for tenant {slug}")
    summary = ingestion.scorer.run(
        [dict(p["data"], sku=p["sku"]) for p in products],
        rules=persistence.active_rules(t.id),
        tenant_id=t.id,
    )
    summary.pop("per_product", None)
    return summary


# ---------- rules & mappings ----------
@app.post("/rules", tags=["governance"])
def add_rule(rule: RuleIn, tenant: str = "default",
             persistence: PersistenceService = Depends(get_persistence)):
    t = persistence.get_or_create_tenant(tenant)
    return persistence.add_rule(
        None if rule.global_rule else t.id,
        rule.rule_id, rule.category, rule.severity, rule.parameters,
    )


@app.post("/mappings", tags=["governance"])
def add_mapping(mapping: MappingIn, tenant: str = "default",
                persistence: PersistenceService = Depends(get_persistence)):
    t = persistence.get_or_create_tenant(tenant)
    return persistence.add_mapping(t.id, mapping.source_value, mapping.normalized, mapping.field)
