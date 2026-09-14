"""Pilot pipeline integration test: real supplier CSV through the FULL stack."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pilot.setup_pilot import PILOT_MAPPINGS, PILOT_SETTINGS

from core.persistence import PersistenceService
from services.ingestion import CSVIngestionService
from engine.kernel import PlatformKernel


def test_pilot_feed_ingests_with_column_mapping(tmp_path):
    persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/pilot.db")
    kernel = PlatformKernel(meta_dir="meta")
    kernel.bootstrap()
    ing = CSVIngestionService(persistence=persistence, kernel=kernel)

    tenant = persistence.get_or_create_tenant("bygmarked-as", settings=PILOT_SETTINGS)
    for field, source_value, normalized in PILOT_MAPPINGS:
        persistence.add_mapping(tenant.id, source_value, normalized, field)

    feed_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "pilot", "feeds", "leverandoer_a_uge38.csv")
    with open(feed_path) as f:
        feed = f.read()
    result = ing.ingest(feed, "bygmarked-as")

    # Real supplier feed: ; delimiter, Danish headers, comma decimals
    assert result["rows_ingested"] == 7
    assert result["errors"] == []
    assert result["summary"]["quality_score"] > 0
    assert all(p["sku"].startswith("1002") for p in result["products"])   # varenr → sku
    # normalization mappings registered for the pilot tenant
    maps = {(m["field"], m["source_value"], m["normalized"]) for m in persistence.get_mappings(tenant.id)}
    assert ("supplier", "SKOVGAARD INDUSTRI A/S", "Skovgaard Industri A/S") in maps
    assert ("unit", "PK", "pakke") in maps
