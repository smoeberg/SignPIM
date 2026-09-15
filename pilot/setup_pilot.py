#!/usr/bin/env python3
"""
SignPIM pilot setup — 'Bygmarked A/S' pilot tenant with a REAL supplier feed.

Idempotent: safe to re-run. Full pipeline:
  feed CSV → column mapping → normalization → engine workflow →
  quality scoring → persistence → quality summary → ERP export.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.persistence import PersistenceService
from services.ingestion import CSVIngestionService
from services.exporters import ExportService
from services.feed_import import LocalFeedImporter
from engine.kernel import PlatformKernel

PILOT_SLUG = "bygmarked-as"
FEED_PATH = os.path.join(os.path.dirname(__file__), "feeds", "leverandoer_a_uge38.csv")

PILOT_SETTINGS = {
    # Supplier column → internal field. Config, not code.
    "feed_column_map": {
        "varenr": "sku",
        "beskrivelse": "name",
        "pris_ink_moms": "price",
        "ean": "ean",
        "kategori": "category",
        "leverandoer": "supplier",
        "antal": "stock_qty",
        "enhed": "unit",
    },
    # ERP export: internal field → ERP column name.
    "export_column_map": {
        "name": "varetekst",
        "ean": "stregkode",
        "stock_qty": "lagerantal",
    },
    "feed_price_includes_vat": True,  # supplier price is already gross (pris_ink_moms)
    "llm": {"provider": "mock"},  # pilot: deterministic, offline; swap to ollama/openai when ready
}

PILOT_MAPPINGS = [  # value normalization: supplier value → canonical
    # (field, source_value, normalized)
    ("supplier", "SKOVGAARD INDUSTRI A/S", "Skovgaard Industri A/S"),
    ("supplier", "skovgaard industri a/s", "Skovgaard Industri A/S"),
    ("unit", "PK", "pakke"),
    ("unit", "STK", "stk"),
]


SPARE_SLUG = "bilservice-vest"
SPARE_FEED = os.path.join(os.path.dirname(__file__), "feeds",
                          "reservedele_bilservice_vest_uge37.csv")

SPARE_SETTINGS = {
    "feed_column_map": {
        "part_no": "sku",
        "part_name": "name",
        "list_price": "price",
        "barcode": "ean",
        "part_group": "category",
        "supplier": "supplier",
        "qty": "stock_qty",
        "unit": "unit",
        "machine_brand": "machine_brand",
        "machine_model": "machine_model",
        "oem_no": "oem_no",
    },
    "export_column_map": {"name": "varetekst", "ean": "stregkode"},
    "feed_price_includes_vat": True,
    "llm": {"provider": "mock"},
}

SPARE_MAPPINGS = [
    ("supplier", "Bilservice Vest", "Bilservice Vest A/S"),
    ("unit", "saet", "sæt"),
    ("category", "Undervogn", "Undervogn/chassi"),
]


def main_spare(persistence, ingestion):
    """Reservedels-pilot: Bilservice Vest — feed med maskine/OEM-mapping."""
    kernel_kwargs = {}
    tenant = persistence.get_or_create_tenant(SPARE_SLUG, settings=SPARE_SETTINGS)
    print(f"[S1] Spare-parts tenant '{SPARE_SLUG}' ready ({tenant.id[:8]}…)")
    for field, source_value, normalized in SPARE_MAPPINGS:
        persistence.add_mapping(tenant.id, source_value, normalized, field)
    print(f"[S2] {len(SPARE_MAPPINGS)} spare-parts normalization mappings registered")
    with open(SPARE_FEED) as f:
        csv_content = f.read()
    result = ingestion.ingest(csv_content, SPARE_SLUG)
    print(f"[S3] Ingested {result['rows_ingested']} rows, {len(result['errors'])} errors")
    for e in result["errors"]:
        print(f"    row {e['row']} ({e.get('sku')}): {e['error']}")
    print(f"[S4] Quality summary: {json.dumps(result['summary'], indent=2)[:300]}")
    return result


def main():
    dsn = os.environ.get("DATABASE_URL", "sqlite:///./pilot/pilot.db")
    persistence = PersistenceService(dsn=dsn)
    kernel = PlatformKernel(meta_dir="meta")
    kernel.bootstrap()
    ingestion = CSVIngestionService(persistence=persistence, kernel=kernel)
    exporter = ExportService(persistence=persistence)

    tenant = persistence.get_or_create_tenant(PILOT_SLUG, settings=PILOT_SETTINGS)
    print(f"[1] Pilot tenant '{PILOT_SLUG}' ready ({tenant.id[:8]}…)")

    for field, source_value, normalized in PILOT_MAPPINGS:
        persistence.add_mapping(tenant.id, source_value, normalized, field)
    print(f"[2] {len(PILOT_MAPPINGS)} normalization mappings registered")

    with open(FEED_PATH) as f:
        csv_content = f.read()
    result = ingestion.ingest(csv_content, PILOT_SLUG)
    print(f"[3] Ingested {result['rows_ingested']} rows, {len(result['errors'])} errors")
    for e in result["errors"]:
        print(f"    row {e['row']} ({e['sku']}): {e['error']}")

    print(f"[4] Quality summary: {json.dumps(result['summary'], indent=2)[:400]}")
    for p in result["products"]:
        v = [v['rule_id'] if isinstance(v, dict) else v for v in (p.get('violations') or [])]
        print(f"    {p['sku']}: score={p['quality_score']} violations={v or 'none'}")

    exp = exporter.export(PILOT_SLUG, fmt="csv", min_score=75)
    print(f"[5] ERP export (score≥75): {exp['count']} rows, filename={exp['filename']}")
    print(exp["content"][:400])

    # Feed-poll via local dir (SFTP swap-in ready)
    imp = LocalFeedImporter(persistence=persistence, ingestion=ingestion)
    feed_dir = os.path.join(os.path.dirname(FEED_PATH))
    poll = imp.poll_local(PILOT_SLUG, feed_dir)
    print(f"[6] Feed poll: {json.dumps(poll)}")

    # Live quality endpoint data
    t_summary = persistence.tenant_quality_summary(tenant.id) \
        if hasattr(persistence, "tenant_quality_summary") else "n/a"
    print(f"[7] Done. Tenant summary: {t_summary}")

    main_spare(persistence, ingestion)


if __name__ == "__main__":
    main()
