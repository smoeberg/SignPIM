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

    def parse_csv(self, content: str, header_map: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Parse a supplier CSV into product dicts. header_map renames supplier
        columns to internal fields (config-driven, per tenant settings.feed_column_map).
        Supplier delimiter (;) auto-detected."""
        # auto-detect delimiter
        sample = content[:4096]
        delimiter = ";"
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            pass
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        if reader.fieldnames is None:
            raise IngestionError("Empty CSV: no header row")
        hmap = header_map or {}
        fieldnames = [hmap.get(h.strip(), h.strip()) for h in reader.fieldnames]
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise IngestionError(f"Missing required columns: {', '.join(missing)}")
        rows = []
        for i, raw in enumerate(reader):
            row = {hmap.get(k.strip(), k.strip()): (v.strip() if isinstance(v, str) else v)
                   for k, v in raw.items() if k is not None}
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
        tenant = self.persistence.get_or_create_tenant(tenant_slug)
        rows = self.parse_csv(csv_content,
                              header_map=(tenant.settings or {}).get("feed_column_map"))
        tenant_id = tenant.id

        # Load tenant config ONCE per ingest (was: one query per row)
        mappings = self.persistence.get_mappings(tenant_id)
        rules = self.persistence.active_rules(tenant_id)
        tenant_settings = dict(tenant.settings or {}) if tenant else {}
        # Merge DB-backed LLM config (tenant overrides global overrides env).
        # tenant settings['llm'] explicitly set -> wins; otherwise DB config fills in.
        db_llm = {}
        try:
            from core.config_service import ConfigService
            cs = ConfigService(self.persistence)
            for k in ("provider", "model", "ollama_host", "api_key", "api_key_anthropic", "api_key_openai"):
                v = cs.resolve(f"llm.{k}", tenant_id=tenant_id)
                if v is not None:
                    # providers take a single `api_key` kwarg
                    db_llm["api_key" if k.startswith("api_key") else k] = v
        except Exception:
            pass  # config table missing (old DB) -> env fallback as before
        if db_llm:
            merged = dict(db_llm)
            merged.update(tenant_settings.get("llm") or {})
            tenant_settings["llm"] = merged

        processed, errors, pending = [], [], []
        for i, row in enumerate(rows):
            try:
                normalized = self._apply_mappings(mappings, row)
                result = self.kernel.run_workflow(
                    workflow, normalized, tenant_id,
                    tenant_settings=tenant_settings)
                pending.append({"sku": row["sku"], "data": result["data"],
                                "quality_score": result["data"].get("quality_score")})
                processed.append({
                    "sku": row["sku"], "violations": result.get("violations", []),
                    "quality_score": result["data"].get("quality_score"),
                })
            except IngestionError:
                raise
            except Exception as e:
                logger.warning("Row %s (sku=%s) failed: %s", i + 1, row.get("sku"), e)
                errors.append({"row": i + 1, "sku": row.get("sku"), "error": str(e)})

        # Single-session bulk persist
        saved = self.persistence.upsert_products_batch(tenant_id, pending)

        # 3D summary over the ingested set (no re-read of the whole table)
        summary = self.scorer.run(
            [dict(p["data"], sku=p["sku"]) for p in pending],
            rules=rules,
            tenant_id=tenant_id,
        )
        per_product = summary.pop("per_product", [])
        if per_product:
            self.persistence.save_quality_scores_bulk(
                tenant_id, [{"sku": pp["sku"], "quality_score": pp["quality_score"]}
                            for pp in per_product])
        for p in processed:
            match = next((pp for pp in per_product if pp["sku"] == p["sku"]), None)
            if match:
                p["quality_score"] = match["quality_score"]

        return {
            "tenant": tenant_slug,
            "rows_ingested": len(processed),
            "errors": errors,
            "products": processed,
            "summary": summary,
        }

    @staticmethod
    def _apply_mappings(mappings: List[Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply pre-loaded normalization mappings without hitting the DB."""
        if not mappings:
            return data
        out = dict(data)
        for m in mappings:
            key = m["field"]
            if key in out and isinstance(out[key], str) and out[key].strip() == m["source_value"]:
                out[key] = m["normalized"]
        return out
