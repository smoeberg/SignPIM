"""Admin mappings CRUD API + spare-parts feed end-to-end."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.persistence import PersistenceService
from core.auth import AuthService
from engine.kernel import PlatformKernel
from services.ingestion import CSVIngestionService
from services.exporters import ExportService

SPARE_CSV = (
    "part_no;part_name;list_price;barcode;part_group;supplier;qty;unit;"
    "machine_brand;machine_model;oem_no\n"
    "BS-4412;Oliefilter 5.0 diesel;89,50;5701234123451;Filtre;Bilservice Vest;40;stk;"
    "Volvo;V60 D4;31392205\n"
    "BS-5501;Bremseklods for, sæt;549,95;;Bremser;Bilservice Vest;15;saet;"
    "VW;Golf VII 1.4 TSI;5Q0698151\n"
)


@pytest.fixture()
def env(tmp_path):
    import os
    os.environ["SIGNPIM_SECRET_KEY"] = "test-master-key-123"
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/api.db")
    app_mod._auth = None
    app_mod.kernel = PlatformKernel(meta_dir="meta")
    app_mod.kernel.bootstrap()
    app_mod.ingestion = None
    t = app_mod.persistence.get_or_create_tenant("acme", settings={
        "feed_column_map": {
            "part_no": "sku", "part_name": "name", "list_price": "price",
            "barcode": "ean", "part_group": "category", "supplier": "supplier",
            "qty": "stock_qty", "unit": "unit",
            "machine_brand": "machine_brand", "machine_model": "machine_model",
            "oem_no": "oem_no",
        },
        "feed_price_includes_vat": True, "llm": {"provider": "mock"},
    })
    svc = AuthService(app_mod.persistence)
    key = svc.create_key(t.id, "mapping-admin", role="admin")["key"]
    c = TestClient(app_mod.app)
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def test_mapping_crud(env):
    r = env.post("/admin/mappings", json={
        "field": "unit", "source_value": "saet", "normalized": "sæt"})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    lst = env.get("/admin/mappings").json()
    assert any(m["id"] == mid and m["normalized"] == "sæt" for m in lst)
    r = env.delete(f"/admin/mappings/{mid}")
    assert r.status_code == 200
    assert env.delete(f"/admin/mappings/{mid}").status_code == 404


def test_mappings_apply_to_ingest(env):
    ing = CSVIngestionService(persistence=app_mod.persistence, kernel=app_mod.kernel)
    r = ing.ingest(SPARE_CSV, "acme")
    assert r["rows_ingested"] == 2, r["errors"]
    prods = {p["sku"]: p for p in r["products"]}
    stored = {p["sku"]: p for p in app_mod.persistence.list_products(app_mod.persistence.get_or_create_tenant("acme").id)}
    assert stored["BS-4412"]["data"]["supplier"] == "Bilservice Vest"  # unmapped value passes through
    assert stored["BS-4412"]["data"].get("machine_brand") == "Volvo"
    assert stored["BS-4412"]["data"].get("oem_no") == "31392205"


def test_mapping_value_rewrites_ingest(env):
    app_mod.persistence.add_mapping(
        app_mod.persistence.get_or_create_tenant("acme").id, "saet", "sæt", "unit")
    ing = CSVIngestionService(persistence=app_mod.persistence, kernel=app_mod.kernel)
    ing.ingest(SPARE_CSV, "acme")
    stored = {p["sku"]: p for p in app_mod.persistence.list_products(app_mod.persistence.get_or_create_tenant("acme").id)}
    assert stored["BS-5501"]["data"]["unit"] == "sæt"


def test_spare_parts_export(env):
    ing = CSVIngestionService(persistence=app_mod.persistence, kernel=app_mod.kernel)
    ing.ingest(SPARE_CSV, "acme")
    exp = ExportService(persistence=app_mod.persistence).export("acme", fmt="csv", min_score=0)
    assert exp["count"] == 2
    assert "BS-4412" in exp["content"]
