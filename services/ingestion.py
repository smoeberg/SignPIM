"""
CSV ingestion pipeline: supplier feed → normalization → engine workflow →
quality scoring → persistence. The 'Feed-First' front door of SignPIM.
"""
import csv
import io
import logging
from typing import Any, Dict, List, Optional

from core.persistence import PersistenceService
from engine.kernel import PlatformKernel
from engine.batch_scoring import QualityScoringService

logger = logging.getLogger("signpim.ingestion")

REQUIRED_COLUMNS = ["sku", "name", "price"]
VALID_NUMERIC_FIELDS = {"price", "quality_score"}


class IngestionError(Exception):
    pass


class PersistenceQualityHook:
    """Adapter exposing save_quality_score for QualityScoringService."""
    def __init__(self, persistence: PersistenceService):
        self.persistence = persistence

    def save_quality_score(self, tenant_id, sku, score):
        return self.persistence.save_quality_score(tenant_id, sku, score)


class CSVIngestionService:
    def __init__(self, persistence: PersistenceService, kernel: PlatformKernel):
        self.persistence = persistence
        self.kernel = kernel
        self.scorer = QualityScoringService(repository=PersistenceQualityHook(persistence))

    def parse_csv(self, content: str) -> List[Dict[str, Any]]:
        """Parse a CSV string into product dicts; raises on missing required columns."""
        reader = csv.DictReader(io.StringIO(content))
        if reader.fieldnames is None:
            raise IngestionError("Empty CSV: no header row")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise IngestionError(f"Missing required columns: {', '.join(missing)}")
        rows = []
        for i, raw in enumerate(reader):
            row = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k is not None}
            if not any(row.values()):
                continue  # skip blank lines
            for f in VALID_NUMERIC_FIELDS:
                if f in row and row[f] not in (None, ''):
                    try:
                        row[f] = float(str(row[f]).replace(',', '.'))
                    except ValueError:
                        raise IngestionError(f"Row {i + 1}: '{f}' is not numeric: {row[f]!r}")
            if 'images' in row and isinstance(row['images'], str):
                row['images'] = [i.strip() for i in row['images'].split(';') if i.strip()]
            rows.append(row)
        return rows

    def ingest(self, csv_content: str, tenant_slug: str, workflow: str = "full_sync") -> Dict[str, Any]:
        """
        Full ingestion: parse → normalize mappings → run engine workflow per row →
        persist with quality score → tenant-level 3D summary.
        """
        rows = self.parse_csv(csv_content)
        tenant = self.persistence.get_or_create_tenant(tenant_slug)
        tenant_id = tenant.id

        processed, errors = [], []
        for i, row in enumerate(rows):
            try:
                normalized = self.persistence.apply_mappings(tenant_id, row)
                result = self.kernel.run_workflow(workflow, normalized, tenant_id)
                saved = self.persistence.upsert_product(
                    tenant_id, row["sku"], result["data"],
                    quality_score=result["data"].get("quality_score"),
                )
                processed.append({
                    "sku": row["sku"], "violations": result.get("violations", []),
                    "quality_score": result["data"].get("quality_score"),
                    "version": saved["version"],
                })
            except IngestionError:
                raise
            except Exception as e:
                logger.warning("Row %s (sku=%s) failed: %s", i + 1, row.get("sku"), e)
                errors.append({"row": i + 1, "sku": row.get("sku"), "error": str(e)})

        persisted = self.persistence.list_products(tenant_id, limit=100000)
        summary = self.scorer.run(
            [dict(p["data"], sku=p["sku"]) for p in persisted],
            rules=self.persistence.active_rules(tenant_id),
            tenant_id=tenant_id,
        )
        summary.pop("per_product", None)

        return {
            "tenant": tenant_slug,
            "rows_ingested": len(processed),
            "errors": errors,
            "products": processed,
            "summary": summary,
        }
