"""Batch export: CSV/JSON/XML with column mapping and min_score filter."""
import json
import csv as csv_mod
import io
import xml.etree.ElementTree as ET
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from services.ingestion import CSVIngestionService
from services.exporters import ExportService, ExportError
from engine.kernel import PlatformKernel

GOOD = "sku,name,price,ean,images\nG-1,Stol,499.0,5901234123457,x.jpg\nB-1,Slet,199.0,,\n"


@pytest.fixture()
def env():
    app_mod.persistence = PersistenceService()
    app_mod._auth = None
    app_mod._users = None
    app_mod._webhooks = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = CSVIngestionService(app_mod.persistence, app_mod.kernel)
    t = app_mod.persistence.get_or_create_tenant("acme")
    key = AuthService(app_mod.persistence).create_key(t.id, "test", "admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {key}"})
    c.post("/tenants/acme/ingest", params={"body": GOOD})
    return c, t.id


def test_export_csv(env):
    c, tid = env
    r = c.get("/export/acme", params={"fmt": "csv"})
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    rows = list(csv_mod.DictReader(io.StringIO(r.text)))
    assert len(rows) == 2
    assert {r["sku"] for r in rows} == {"G-1", "B-1"}


def test_export_csv_custom_delimiter(env):
    c, tid = env
    r = c.get("/export/acme", params={"fmt": "csv", "delimiter": ";"})
    assert ";" in r.text.splitlines()[0]


def test_export_json(env):
    c, tid = env
    r = c.get("/export/acme", params={"fmt": "json"})
    assert r.status_code == 200
    data = json.loads(r.text)
    assert len(data) == 2
    by_sku = {d["sku"]: d for d in data}
    assert by_sku["G-1"]["name"] == "Stol"
    assert by_sku["G-1"]["quality_score"] is not None


def test_export_xml(env):
    c, tid = env
    r = c.get("/export/acme", params={"fmt": "xml"})
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    assert root.tag == "products"
    prods = root.findall("product")
    assert len(prods) == 2
    assert {p.get("sku") for p in prods} == {"G-1", "B-1"}
    names = [p.find("name").text for p in prods]
    assert "Stol" in names


def test_export_min_score_filter(env):
    c, tid = env
    r = c.get("/export/acme", params={"fmt": "json", "min_score": 90})
    data = json.loads(r.text)
    assert all(d["quality_score"] >= 90 for d in data)
    assert all(d["sku"] != "B-1" for d in data)  # the bad one excluded


def test_export_column_mapping(env):
    """settings.export_column_map renames internal → ERP column names."""
    c, tid = env
    from core.models import Tenant
    from sqlalchemy import select
    with app_mod.persistence.session() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == "acme"))
        t.settings = {"export_column_map": {"name": "product_title", "price": "net_price"}}
    r = c.get("/export/acme", params={"fmt": "json"})
    d = json.loads(r.text)[0]
    assert "product_title" in d and "net_price" in d
    assert "name" not in d and "price" not in d


def test_export_invalid_format_400(env):
    c, tid = env
    r = c.get("/export/acme", params={"fmt": "parquet"})
    assert r.status_code == 400 or r.status_code == 422


def test_direct_service_invalid_format():
    svc = ExportService(app_mod.persistence)
    with pytest.raises(ExportError):
        svc.export("acme", fmt="parquet")
