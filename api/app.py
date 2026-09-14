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

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.persistence import PersistenceService
from engine.kernel import PlatformKernel
from services.ingestion import CSVIngestionService, IngestionError
from services.images import ImageService, ImageError

logger = logging.getLogger("signpim.api")

from fastapi import Header
from core.auth import AuthService, UserService, ROLE_SCOPES
from core.models import Tenant as _TenantModel  # noqa

_auth: AuthService = None
_users: "UserService" = None
_webhooks = None


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

from engine.operators.llm_ops import LLMCallLogger
LLMCallLogger.bind(persistence)
kernel = PlatformKernel(meta_dir="meta")
kernel.bootstrap()
ingestion = CSVIngestionService(persistence, kernel)


def _rescore_product(tenant_id: str, sku: str) -> None:
    """Re-run the quality workflow for one product after a data change (e.g. image bind)."""
    from sqlalchemy import select
    from core.models import Product
    with persistence.session() as s:
        p = s.scalars(select(Product).where(
            Product.tenant_id == tenant_id, Product.sku == sku)).first()
        if p is None:
            return
        stored = dict(p.data or {}); stored["sku"] = sku
    result = kernel.run_workflow("full_sync", stored, tenant_id)
    persistence.save_quality_score(tenant_id, sku,
                                   result["data"].get("quality_score"))


image_service = ImageService(persistence, rescorer=_rescore_product)
media_root = image_service.media_root
os.makedirs(media_root, exist_ok=True)


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


def get_webhooks():
    global _webhooks
    if _webhooks is None:
        from services.webhooks import WebhookService
        _webhooks = WebhookService(persistence)
    return _webhooks


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


# ---------- export ----------
@app.get("/export/{slug}", tags=["export"])
def export_products(slug: str, fmt: str = Query("csv", pattern="^(csv|json|xml)$"),
                    min_score: Optional[float] = Query(None, ge=0, le=100),
                    limit: int = Query(10000, ge=1, le=100000),
                    delimiter: str = Query(",", max_length=1),
                    persistence: PersistenceService = Depends(get_persistence),
                    info: dict = Depends(require_scope("products:read"))):
    """Batch export to webshop/ERP format (CSV/JSON/XML)."""
    _enforce_tenant(info, slug)
    from services.exporters import ExportService, ExportError
    try:
        result = ExportService(persistence).export(slug, fmt, min_score, limit, delimiter)
    except ExportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from fastapi.responses import Response
    media = {"csv": "text/csv", "json": "application/json", "xml": "application/xml"}[fmt]
    return Response(content=result["content"], media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"'})


# ---------- webhooks ----------
def _tenant_id_of(info: dict) -> str:
    return info["tenant_id"]


@app.post("/webhooks", tags=["webhooks"])
def create_webhook(url: str = Query(...), min_score: int = Query(75, ge=0, le=100),
                   tenant: str = "default",
                   persistence: PersistenceService = Depends(get_persistence),
                   info: dict = Depends(require_scope("keys:manage"))):
    """Subscribe to quality-drop notifications for this tenant."""
    _enforce_tenant(info, tenant)
    try:
        return get_webhooks().subscribe(persistence.get_or_create_tenant(tenant).id,
                                        url, min_score)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/webhooks", tags=["webhooks"])
def list_webhooks(tenant: str = "default",
                  persistence: PersistenceService = Depends(get_persistence),
                  info: dict = Depends(require_scope("keys:manage"))):
    _enforce_tenant(info, tenant)
    return get_webhooks().list_subscriptions(persistence.get_or_create_tenant(tenant).id)


@app.delete("/webhooks/{webhook_id}", tags=["webhooks"])
def delete_webhook(webhook_id: str, tenant: str = "default",
                   persistence: PersistenceService = Depends(get_persistence),
                   info: dict = Depends(require_scope("keys:manage"))):
    _enforce_tenant(info, tenant)
    ok = get_webhooks().unsubscribe(persistence.get_or_create_tenant(tenant).id, webhook_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"unsubscribed": webhook_id}


@app.get("/webhooks/{webhook_id}/deliveries", tags=["webhooks"])
def webhook_deliveries(webhook_id: str, tenant: str = "default",
                       persistence: PersistenceService = Depends(get_persistence),
                       info: dict = Depends(require_scope("keys:manage"))):
    _enforce_tenant(info, tenant)
    from sqlalchemy import select
    from services.webhooks import WebhookDelivery
    t = persistence.get_or_create_tenant(tenant)
    with persistence.session() as s:
        from services.webhooks import WebhookSubscription
        sub = s.get(WebhookSubscription, webhook_id)
        if sub is None or sub.tenant_id != t.id:
            raise HTTPException(status_code=404, detail="Webhook not found")
        rows = s.scalars(select(WebhookDelivery).where(
            WebhookDelivery.tenant_id == t.id,
            WebhookDelivery.subscription_id == webhook_id).limit(50)).all()
        return [{"event": d.event_type, "success": d.success, "attempts": d.attempts,
                 "status_code": d.status_code, "error": d.error,
                 "created_at": d.created_at.isoformat()} for d in rows]


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

@app.get("/products/{sku}/images/proposals", tags=["images"])
def list_image_proposals(sku: str, tenant: str = "default",
                         persistence: PersistenceService = Depends(get_persistence),
                         info: dict = Depends(require_scope("products:read"))):
    """List AI-proposed images awaiting human review. Readers allowed; apply is not."""
    _enforce_tenant(info, tenant)
    t = persistence.get_or_create_tenant(tenant)
    p = persistence.get_product(t.id, sku)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found")
    proposals = (((p.get("data") or {}) if isinstance(p, dict) else (p.data or {}))
                 .get("_ai_meta") or {}).get("images_proposed") or []
    if isinstance(proposals, dict):
        candidates = [proposals]
    else:
        candidates = [x for x in proposals if isinstance(x, dict)]
    return {"sku": sku, "proposals": candidates}


@app.post("/products/{sku}/images/apply", tags=["images"])
def apply_image_proposal(sku: str, tenant: str = "default",
                         body: dict = Body(default={}),
                         persistence: PersistenceService = Depends(get_persistence),
                         info: dict = Depends(require_scope("products:write"))):
    """Human review endpoint: bind a proposed (or uploaded) image to the product.
    Only this path commits images to product.images — AI operators never bind."""
    _enforce_tenant(info, tenant)
    url = body.get("url")
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=400, detail="Missing 'url'")
    t = persistence.get_or_create_tenant(tenant)
    p = persistence.get_product(t.id, sku)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found")
    # Validate the blob exists in our media store (no external URLs)
    try:
        image_service.read_blob(url)
    except (ImageError, OSError):
        raise HTTPException(status_code=404, detail="Unknown proposal for this product")
    proposals = (((p.get("data") or {}) if isinstance(p, dict) else (p.data or {}))
                 .get("_ai_meta") or {}).get("images_proposed") or []
    if isinstance(proposals, dict):
        candidates = [proposals]
    else:
        candidates = [x for x in proposals if isinstance(x, dict)]
    match = next((c for c in candidates if c.get("url") == url), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Unknown proposal for this product")
    stored = image_service.bind_existing(t.id, sku, url)
    fresh = persistence.get_product(t.id, sku)
    data = dict((fresh.get("data") or {}) if isinstance(fresh, dict) else (fresh.data or {}))
    ai = dict(data.get("_ai_meta") or {})
    raw = ai.get("images_proposed")
    if isinstance(raw, dict):
        remaining = raw if raw.get("url") != url else None
        if remaining:
            ai["images_proposed"] = remaining
        else:
            ai.pop("images_proposed", None)
    elif isinstance(raw, list):
        remaining = [x for x in raw if not (isinstance(x, dict) and x.get("url") == url)]
        if remaining:
            ai["images_proposed"] = remaining
        else:
            ai.pop("images_proposed", None)
    data["_ai_meta"] = ai
    persistence.upsert_product(t.id, sku, data)
    return {"status": "applied", "sku": sku, **stored}


@app.post("/products/{sku}/images", tags=["images"])
async def upload_image(sku: str, request: Request, tenant: str = "default",
                       info: dict = Depends(require_scope("products:write"))):
    """Upload one image for a product (raw bytes or multipart). Score recomputed."""
    _enforce_tenant(info, tenant)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body — send raw image bytes")
    t = persistence.get_or_create_tenant(tenant)
    try:
        stored = image_service.store_image(t.id, sku, body)
    except ImageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "stored", **stored}


@app.get("/products/{sku}/images", tags=["images"])
def list_images(sku: str, tenant: str = "default",
                info: dict = Depends(require_scope("products:read"))):
    _enforce_tenant(info, tenant)
    t = persistence.get_or_create_tenant(tenant)
    from sqlalchemy import select
    from core.models import Product
    with persistence.session() as s:
        p = s.scalars(select(Product).where(
            Product.tenant_id == t.id, Product.sku == sku)).first()
        if p is None:
            raise HTTPException(status_code=404, detail="Unknown product")
        return {"sku": sku, "images": (p.data or {}).get("images") or []}


@app.delete("/products/{sku}/images", tags=["images"])
def remove_image(sku: str, url: str = Query(...), tenant: str = "default",
                 info: dict = Depends(require_scope("products:write"))):
    _enforce_tenant(info, tenant)
    t = persistence.get_or_create_tenant(tenant)
    image_service.unbind(t.id, sku, url)
    return {"status": "removed", "sku": sku, "url": url}


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
app.mount("/media", StaticFiles(directory=media_root, check_dir=False), name="media")


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
        try:
            fired = get_webhooks().check_and_notify(
                slug, _tenant_id_of(info), result.get("summary", {}))
            result["webhooks_fired"] = len(fired)
        except Exception as e:  # webhook failure must never break ingest
            logger.warning("Webhook dispatch failed: %s", e)
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


@app.get("/llm/usage", tags=["ai"])
def llm_usage(tenant: str = "default",
              info: dict = Depends(require_scope("quality:read")),
              persistence: PersistenceService = Depends(get_persistence)):
    _enforce_tenant(info, tenant)
    t = persistence.get_or_create_tenant(tenant)
    return persistence.llm_usage(t.id)


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
def poll_feed(slug: str, mode: str = Query("local", pattern="^(local|sftp)$"),
              directory: str = Query(None, description="Local directory (mode=local)"),
              persistence: PersistenceService = Depends(get_persistence),
              info: dict = Depends(require_scope("ingest:write"))):
    """Poll a feed directory for new/changed supplier CSVs.

    mode=local: directory from query param.
    mode=sftp: host/port/username/credentials come from environment
    (SIGNPIM_SFTP_HOST, SIGNPIM_SFTP_PORT, SIGNPIM_SFTP_USER,
    SIGNPIM_SFTP_PASSWORD, SIGNPIM_SFTP_KEY_PATH) — never from the request body."""
    _enforce_tenant(info, slug)
    from services.feed_import import LocalFeedImporter, SFTPFeedImporter
    if mode == "sftp":
        import os
        host = os.environ.get("SIGNPIM_SFTP_HOST")
        if not host:
            raise HTTPException(status_code=503,
                                detail="SFTP not configured: set SIGNPIM_SFTP_HOST")
        remote_dir = os.environ.get("SIGNPIM_SFTP_REMOTE_DIR", "/out")
        user = os.environ.get("SIGNPIM_SFTP_USER", "signpim")
        password = os.environ.get("SIGNPIM_SFTP_PASSWORD")
        key_path = os.environ.get("SIGNPIM_SFTP_KEY_PATH")
        if not password and not key_path:
            raise HTTPException(status_code=503,
                                detail="SFTP needs SIGNPIM_SFTP_PASSWORD or SIGNPIM_SFTP_KEY_PATH")
        importer = SFTPFeedImporter(persistence, ingestion)
        return importer.poll(slug, host=host, remote_dir=remote_dir, username=user,
                             password=password, key_path=key_path,
                             port=int(os.environ.get("SIGNPIM_SFTP_PORT", "22")))
    if not directory:
        raise HTTPException(status_code=400, detail="mode=local requires 'directory'")
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
    _enforce_tenant(info, slug)
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


# ============ Admin config backend ============
from core.config_service import ConfigService as _ConfigService


def _cfg_service(persistence: PersistenceService) -> _ConfigService:
    return _ConfigService(persistence)


def _require_admin(info: dict):
    if info.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


@app.get("/admin/config", tags=["admin"])
def admin_list_config(tenant: str = "default",
                      persistence: PersistenceService = Depends(get_persistence),
                      info: dict = Depends(require_scope("keys:manage"))):
    """List all config entries for a tenant. Secrets masked."""
    _require_admin(info)
    t = persistence.get_or_create_tenant(tenant)
    return _cfg_service(persistence).list_keys(t.id)


@app.get("/admin/config/effective", tags=["admin"])
def admin_effective_config(tenant: str = "default",
                           persistence: PersistenceService = Depends(get_persistence),
                           info: dict = Depends(require_scope("keys:manage"))):
    """Full resolution view: tenant DB > global DB > env, with sources shown."""
    _require_admin(info)
    t = persistence.get_or_create_tenant(tenant)
    return _cfg_service(persistence).effective(t.id)


@app.put("/admin/config/{key}", tags=["admin"])
def admin_set_config(key: str, body: dict, tenant: str = "default",
                     persistence: PersistenceService = Depends(get_persistence),
                     info: dict = Depends(require_scope("keys:manage"))):
    """Set a config key. Secrets are encrypted at rest (requires SIGNPIM_SECRET_KEY)."""
    _require_admin(info)
    if "value" not in body:
        raise HTTPException(status_code=422, detail="body must contain 'value'")
    t = persistence.get_or_create_tenant(tenant)
    try:
        return _cfg_service(persistence).set(
            key, body["value"], tenant_id=body.get("global") and None or t.id,
            updated_by=info.get("user", {}).get("email"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.delete("/admin/config/{key}", tags=["admin"])
def admin_delete_config(key: str, tenant: str = "default",
                        persistence: PersistenceService = Depends(get_persistence),
                        info: dict = Depends(require_scope("keys:manage"))):
    _require_admin(info)
    t = persistence.get_or_create_tenant(tenant)
    ok = _cfg_service(persistence).delete(key, tenant_id=t.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Config key not found")
    return {"deleted": key}
