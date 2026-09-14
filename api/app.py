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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.persistence import PersistenceService
from engine.kernel import PlatformKernel
from services.ingestion import CSVIngestionService, IngestionError

logger = logging.getLogger("signpim.api")

from fastapi import Header
from core.auth import AuthService, UserService, ROLE_SCOPES
from core.models import Tenant as _TenantModel  # noqa

_auth: AuthService = None
_users: "UserService" = None


def get_auth() -> AuthService:
    from fastapi import Request
    # placeholder replaced below
    raise NotImplementedError

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


def get_auth() -> AuthService:
    global _auth
    if _auth is None:
        _auth = AuthService(persistence)
    return _auth


def get_users() -> "UserService":
    global _users
    if _users is None:
        _users = UserService(persistence)
    return _users


def require_scope(scope: str):
    """FastAPI dependency: authenticate + enforce scope."""
    def dep(request: "Request", auth: AuthService = Depends(get_auth)):
        header = request.headers.get("Authorization", "")
        try:
            if "spimsess_" in header:
                info = get_users().authenticate_session(header.replace("Bearer ", ""))
            else:
                info = auth.authenticate(header)
        except PermissionError as e:
            raise HTTPException(status_code=401, detail=str(e))
        if scope not in info["scopes"]:
            raise HTTPException(status_code=403, detail=f"Missing scope: {scope}")
        request.state.auth = info
        return info
    return dep


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


# ---------- auth (users & sessions) ----------
@app.post("/auth/login", tags=["auth"])
def login(tenant: str = Query(...), email: str = Query(...), password: str = Query(...)):
    """Password login. Returns a session token (Bearer spimsess_...)."""
    try:
        return get_users().login(tenant, email, password)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/auth/logout", tags=["auth"])
def logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    ok = get_users().logout(token)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid session")
    return {"logged_out": True}


@app.get("/auth/me", tags=["auth"])
def me(request: Request, auth: AuthService = Depends(get_auth)):
    header = request.headers.get("Authorization", "")
    try:
        if "spimsess_" in header:
            return get_users().authenticate_session(header.replace("Bearer ", ""))
        return auth.authenticate(header)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/users", tags=["users"])
def create_user(email: str = Query(...), password: str = Query(...),
                role: str = Query("reader"), display_name: str = Query(None),
                tenant: str = "default",
                persistence: PersistenceService = Depends(get_persistence),
                info: dict = Depends(require_scope("keys:manage"))):
    """Create a user for the caller's tenant (admin only)."""
    _enforce_tenant(info, tenant)
    try:
        return get_users().create_user(persistence.get_or_create_tenant(tenant).id,
                                       email, password, role, display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/users", tags=["users"])
def list_users(tenant: str = "default",
               persistence: PersistenceService = Depends(get_persistence),
               info: dict = Depends(require_scope("keys:manage"))):
    _enforce_tenant(info, tenant)
    return get_users().list_users(persistence.get_or_create_tenant(tenant).id)


@app.patch("/users/{user_id}", tags=["users"])
def update_user(user_id: str, role: str = Query(None), active: bool = Query(None),
                tenant: str = "default",
                persistence: PersistenceService = Depends(get_persistence),
                info: dict = Depends(require_scope("keys:manage"))):
    _enforce_tenant(info, tenant)
    tid = persistence.get_or_create_tenant(tenant).id
    users = get_users()
    ok = True
    if role:
        try:
            ok = users.set_role(tid, user_id, role)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if active is False:
        ok = users.deactivate(tid, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"updated": user_id}


# ---------- dashboard ----------
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---------- health ----------
@app.get("/health", tags=["ops"])
def health():
    """Liveness/readiness probe used by Docker healthchecks & load balancers."""
    return {"status": "ok", "version": app.version}


# ---------- ingestion ----------
@app.post("/tenants/{slug}/ingest", tags=["ingestion"])
def ingest_csv(slug: str, body: str = Query(..., media_type="text/csv"),
               info: dict = Depends(require_scope("ingest:write"))):
    """Ingest a raw supplier CSV feed for a tenant (Feed-First front door)."""
    if slug != _slug_of(info):
        raise HTTPException(status_code=403, detail="Key not valid for this tenant")
    try:
        result = ingestion.ingest(body, slug)
    except IngestionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result


# ---------- products ----------
@app.post("/products", tags=["products"])
def upsert_product(product: ProductIn, tenant: str = "default",
                   persistence: PersistenceService = Depends(get_persistence),
                   info: dict = Depends(require_scope("products:write"))):
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
                persistence: PersistenceService = Depends(get_persistence),
                info: dict = Depends(require_scope("products:read"))):
    t = persistence.get_or_create_tenant(tenant)
    p = persistence.get_product(t.id, sku)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found")
    return p


@app.get("/products", tags=["products"])
def list_products(tenant: str = "default", min_score: Optional[float] = Query(None, ge=0, le=100),
                  limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                  persistence: PersistenceService = Depends(get_persistence),
                  info: dict = Depends(require_scope("products:read"))):
    t = persistence.get_or_create_tenant(tenant)
    return persistence.list_products(t.id, min_score=min_score, limit=limit, offset=offset)


# ---------- key management ----------
@app.post("/keys", tags=["keys"])
def create_key(name: str = Query(...), role: str = Query("reader"),
               tenant: str = "default",
               persistence: PersistenceService = Depends(get_persistence),
               info: dict = Depends(require_scope("keys:manage"))):
    """Create an API key for the caller's tenant. Plaintext shown exactly once."""
    t = persistence.get_or_create_tenant(tenant)
    if info["tenant_id"] != t.id:
        raise HTTPException(status_code=403, detail="Key not valid for this tenant")
    return get_auth().create_key(t.id, name, role)


@app.get("/keys", tags=["keys"])
def list_keys(tenant: str = "default",
              persistence: PersistenceService = Depends(get_persistence),
              info: dict = Depends(require_scope("keys:manage"))):
    t = persistence.get_or_create_tenant(tenant)
    if info["tenant_id"] != t.id:
        raise HTTPException(status_code=403, detail="Key not valid for this tenant")
    return get_auth().list_keys(t.id)


@app.delete("/keys/{key_id}", tags=["keys"])
def revoke_key(key_id: str, tenant: str = "default",
               persistence: PersistenceService = Depends(get_persistence),
               info: dict = Depends(require_scope("keys:manage"))):
    t = persistence.get_or_create_tenant(tenant)
    if info["tenant_id"] != t.id:
        raise HTTPException(status_code=403, detail="Key not valid for this tenant")
    ok = get_auth().revoke_key(t.id, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"revoked": key_id}


def _enforce_tenant(info: dict, slug: str):
    if slug != _slug_of(info):
        raise HTTPException(status_code=403, detail="Key not valid for this tenant")


def _slug_of(info: dict) -> str:
    from core.models import Tenant as T
    from sqlalchemy import select
    with persistence.session() as s:
        t = s.get(T, info["tenant_id"])
        return t.slug if t else info["tenant_id"]


# ---------- feed import ----------
@app.post("/feeds/{slug}/poll", tags=["feeds"])
def poll_feed(slug: str, directory: str = Query(..., description="Local directory with CSV feeds"),
              persistence: PersistenceService = Depends(get_persistence),
              info: dict = Depends(require_scope("ingest:write"))):
    """Poll a local feed directory for new/changed supplier CSVs (SFTP variant available via services.feed_import.SFTPFeedImporter)."""
    _enforce_tenant(info, slug)
    from services.feed_import import LocalFeedImporter
    importer = LocalFeedImporter(persistence, ingestion)
    return importer.poll_local(slug, directory)


@app.get("/feeds/{slug}/state", tags=["feeds"])
def feed_state(slug: str,
               persistence: PersistenceService = Depends(get_persistence),
               info: dict = Depends(require_scope("products:read"))):
    """Import state per feed file (checksum-based idempotency)."""
    _enforce_tenant(info, slug)
    from sqlalchemy import select
    from services.feed_import import FeedImportState
    t = persistence.get_or_create_tenant(slug)
    with persistence.session() as s:
        states = s.scalars(select(FeedImportState).where(
            FeedImportState.tenant_id == t.id)).all()
        return [{"file": st.file_key, "checksum": st.checksum[:12],
                 "rows": st.rows_ingested, "errors": st.errors,
                 "imported_at": st.imported_at.isoformat()} for st in states]


# ---------- quality ----------
@app.get("/quality/{slug}", tags=["quality"])
def quality_summary(slug: str,
                    persistence: PersistenceService = Depends(get_persistence),
                    info: dict = Depends(require_scope("quality:read"))):
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
             persistence: PersistenceService = Depends(get_persistence),
             info: dict = Depends(require_scope("rules:write"))):
    t = persistence.get_or_create_tenant(tenant)
    return persistence.add_rule(
        None if rule.global_rule else t.id,
        rule.rule_id, rule.category, rule.severity, rule.parameters,
    )


@app.post("/mappings", tags=["governance"])
def add_mapping(mapping: MappingIn, tenant: str = "default",
                persistence: PersistenceService = Depends(get_persistence),
                info: dict = Depends(require_scope("mappings:write"))):
    t = persistence.get_or_create_tenant(tenant)
    return persistence.add_mapping(t.id, mapping.source_value, mapping.normalized, mapping.field)
