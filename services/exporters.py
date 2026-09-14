"""
Batch export: products → webshop/ERP formats (CSV, JSON, XML).

- Tenant-specific field mapping via normalization mappings (legacy ERP column names).
- Streaming-friendly: renders full output in memory (fine up to ~100k products).
- Format detection: csv (with delimiter/encoding options), json, xml.
"""
import csv
import io
import json
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import Any, Dict, List, Optional

from core.persistence import PersistenceService


FORMATS = ("csv", "json", "xml")


class ExportError(Exception):
    pass


class ExportService:
    def __init__(self, persistence: PersistenceService):
        self.persistence = persistence

    def _load_products(self, tenant_id: str, min_score: Optional[float] = None,
                       limit: int = 10000) -> List[Dict[str, Any]]:
        from sqlalchemy import select
        from core.models import Product
        with self.persistence.session() as s:
            q = select(Product).where(Product.tenant_id == tenant_id)
            if min_score is not None:
                q = q.where(Product.quality_score >= min_score)
            q = q.limit(limit)
            rows = s.scalars(q).all()
            return [{"sku": r.sku, "data": r.data, "quality_score": r.quality_score,
                     "version": r.version} for r in rows]

    def _apply_mapping(self, tenant_slug: str, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rename internal fields to ERP/webshop column names via tenant settings
        (settings.export_column_map: {"internal": "external", ...})."""
        t = self.persistence.get_or_create_tenant(tenant_slug)
        rename = (t.settings or {}).get("export_column_map", {})
        if not rename:
            return products
        out = []
        for p in products:
            data = p["data"]
            mapped = {rename.get(k, k): v for k, v in data.items()}
            out.append({**p, "data": mapped})
        return out

    def export(self, tenant_slug: str, fmt: str = "csv",
               min_score: Optional[float] = None, limit: int = 10000,
               delimiter: str = ",", encoding: str = "utf-8") -> Dict[str, Any]:
        if fmt not in FORMATS:
            raise ExportError(f"Unknown format '{fmt}'. Allowed: {', '.join(FORMATS)}")
        t = self.persistence.get_or_create_tenant(tenant_slug)
        products = self._load_products(t.id, min_score, limit)
        products = self._apply_mapping(tenant_slug, products)

        if fmt == "csv":
            content = self._to_csv(products, delimiter, encoding)
        elif fmt == "json":
            content = self._to_json(products)
        else:
            content = self._to_xml(products)
        return {"format": fmt, "count": len(products),
                "content": content, "filename": f"products_{tenant_slug}.{fmt}"}

    # ---------- renderers ----------
    def _to_csv(self, products, delimiter: str, encoding: str) -> str:
        if not products:
            return ""
        # union of keys across products (webshop feeds often have sparse fields)
        keys = []
        for p in products:
            for k in p["data"]:
                if k not in keys:
                    keys.append(k)
        keys = ["sku"] + [k for k in keys if k != "sku"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=keys, delimiter=delimiter,
                                extrasaction="ignore")
        writer.writeheader()
        for p in products:
            row = dict(p["data"])
            row["sku"] = p["sku"]
            if p.get("quality_score") is not None:
                row["quality_score"] = f"{p['quality_score']:.2f}"
            writer.writerow(row)
        return buf.getvalue()

    def _to_json(self, products) -> str:
        def _num(v):
            return float(v) if hasattr(v, "as_integer_ratio") else v
        out = [{"sku": p["sku"], **{k: _num(v) for k, v in p["data"].items()},
                "quality_score": _num(p["quality_score"]), "version": p["version"]}
               for p in products]
        return json.dumps(out, ensure_ascii=False, indent=2, default=str)

    def _to_xml(self, products) -> str:
        root = ET.Element("products")
        for p in products:
            prod = ET.SubElement(root, "product", sku=str(p["sku"]))
            for k, v in p["data"].items():
                field = ET.SubElement(prod, k)
                field.text = "" if v is None else str(v)
            if p["quality_score"] is not None:
                ET.SubElement(prod, "quality_score").text = f"{p['quality_score']:.2f}"
        rough = ET.tostring(root, encoding="unicode")
        return minidom.parseString(rough).toprettyxml(indent="  ")
