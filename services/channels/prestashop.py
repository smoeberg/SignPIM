"""PrestaShop Webservice client and payload mapping (ADR-018).

Uses the PrestaShop Webservice (``/api/``), which covers every resource the
connector needs in PrestaShop 8 and 9 (products, images, specific_prices,
stock_availables, orders). Reads request JSON; writes send XML, which is what
the Webservice accepts.

The PrestaShop 9 Admin API (OAuth2/JSON, module ``ps_apiresources``) is still
pre-1.0; ``ChannelClient`` is the seam where an Admin API adapter will plug in.
"""
from __future__ import annotations

import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol

import httpx


class ChannelError(RuntimeError):
    """A channel call failed. ``retryable`` tells the outbox whether to retry."""

    def __init__(self, message: str, *, retryable: bool = True, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ChannelClient(Protocol):
    """Operations the sync engine needs from any channel API."""

    def ping(self) -> Dict[str, Any]: ...
    def create(self, resource: str, fields: Mapping[str, Any]) -> str: ...
    def update(self, resource: str, external_id: str, fields: Mapping[str, Any]) -> None: ...
    def delete(self, resource: str, external_id: str) -> None: ...
    def find(self, resource: str, filters: Mapping[str, Any], display: Iterable[str]) -> List[Dict[str, Any]]: ...
    def list_page(self, resource: str, display: Iterable[str], offset: int, limit: int) -> List[Dict[str, Any]]: ...
    def upload_product_image(self, product_id: str, filename: str, data: bytes, mime: str) -> str: ...
    def delete_product_image(self, product_id: str, image_id: str) -> None: ...


# Resource name -> XML element name for single objects.
_SINGULAR = {
    "products": "product",
    "specific_prices": "specific_price",
    "stock_availables": "stock_available",
    "orders": "order",
}


def _xml_value(parent: ET.Element, key: str, value: Any) -> None:
    node = ET.SubElement(parent, key)
    if isinstance(value, dict) and value.get("__lang__"):
        # Multilanguage field: {"__lang__": True, "1": "Navn", "2": "Name"}
        for lang_id, text in value.items():
            if lang_id == "__lang__":
                continue
            lang = ET.SubElement(node, "language", id=str(lang_id))
            lang.text = "" if text is None else str(text)
    elif isinstance(value, dict):
        for sub_key, sub_value in value.items():
            _xml_value(node, sub_key, sub_value)
    elif isinstance(value, list):
        # Associations: {"categories": [{"category": {"id": 2}}, ...]}
        for item in value:
            for sub_key, sub_value in item.items():
                _xml_value(node, sub_key, sub_value)
    elif isinstance(value, bool):
        node.text = "1" if value else "0"
    else:
        node.text = "" if value is None else str(value)


def to_xml(resource: str, fields: Mapping[str, Any], external_id: Optional[str] = None) -> bytes:
    root = ET.Element("prestashop")
    obj = ET.SubElement(root, _SINGULAR.get(resource, resource.rstrip("s")))
    if external_id is not None:
        ET.SubElement(obj, "id").text = str(external_id)
    for key, value in fields.items():
        _xml_value(obj, key, value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _extract_id(response: httpx.Response) -> str:
    text = response.text
    try:
        data = response.json()
        for value in data.values():
            if isinstance(value, dict) and "id" in value:
                return str(value["id"])
    except (ValueError, AttributeError):
        pass
    try:
        root = ET.fromstring(text)
        node = root.find(".//id")
        if node is not None and node.text:
            return node.text.strip()
    except ET.ParseError:
        pass
    raise ChannelError("could not read id from PrestaShop response", retryable=False)


class PrestaShopWebservice:
    """Thin, testable client for the PrestaShop Webservice."""

    def __init__(self, base_url: str, api_key: str, *, transport: Optional[httpx.BaseTransport] = None,
                 timeout: float = 30.0):
        if not base_url.startswith(("https://", "http://")):
            raise ChannelError("base_url must be an http(s) URL", retryable=False)
        self.base = base_url.rstrip("/") + "/api"
        self.http = httpx.Client(auth=(api_key, ""), timeout=timeout, transport=transport)

    def close(self) -> None:
        self.http.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        params = dict(kwargs.pop("params", {}) or {})
        params.setdefault("output_format", "JSON")
        try:
            response = self.http.request(method, f"{self.base}/{path}", params=params, **kwargs)
        except httpx.HTTPError as exc:
            raise ChannelError(f"{method} {path}: {exc}") from exc
        if response.status_code >= 400:
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            detail = response.text[:500]
            raise ChannelError(
                f"{method} {path} -> HTTP {response.status_code}: {detail}",
                retryable=retryable, status=response.status_code,
            )
        return response

    def ping(self) -> Dict[str, Any]:
        response = self._request("GET", "")
        return {"ok": True, "status": response.status_code}

    def create(self, resource: str, fields: Mapping[str, Any]) -> str:
        response = self._request(
            "POST", resource, content=to_xml(resource, fields),
            headers={"Content-Type": "application/xml"},
        )
        return _extract_id(response)

    def update(self, resource: str, external_id: str, fields: Mapping[str, Any]) -> None:
        # PATCH sends only fields SignPIM owns; anything else in the shop is untouched.
        self._request(
            "PATCH", f"{resource}/{external_id}", content=to_xml(resource, fields, external_id),
            headers={"Content-Type": "application/xml"},
        )

    def delete(self, resource: str, external_id: str) -> None:
        try:
            self._request("DELETE", f"{resource}/{external_id}")
        except ChannelError as exc:
            if exc.status != 404:
                raise

    def _rows(self, response: httpx.Response, resource: str) -> List[Dict[str, Any]]:
        if not response.content.strip():
            return []
        data = response.json()
        if isinstance(data, list):     # PrestaShop returns [] for empty results
            return []
        rows = data.get(resource, [])
        return rows if isinstance(rows, list) else [rows]

    def find(self, resource: str, filters: Mapping[str, Any], display: Iterable[str]) -> List[Dict[str, Any]]:
        params = {f"filter[{k}]": f"[{v}]" for k, v in filters.items()}
        params["display"] = "[" + ",".join(display) + "]"
        return self._rows(self._request("GET", resource, params=params), resource)

    def list_page(self, resource: str, display: Iterable[str], offset: int, limit: int) -> List[Dict[str, Any]]:
        params = {"display": "[" + ",".join(display) + "]", "limit": f"{offset},{limit}", "sort": "[id_ASC]"}
        return self._rows(self._request("GET", resource, params=params), resource)

    def upload_product_image(self, product_id: str, filename: str, data: bytes, mime: str) -> str:
        response = self._request(
            "POST", f"images/products/{product_id}", files={"image": (filename, data, mime)},
        )
        return _extract_id(response)

    def delete_product_image(self, product_id: str, image_id: str) -> None:
        self.delete(f"images/products/{product_id}", image_id)


# ---------------------------------------------------------------------------
# Mapping SignPIM -> PrestaShop
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
                                 .replace("Æ", "Ae").replace("Ø", "Oe").replace("Å", "Aa"))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:128] or "product"


def _lang_field(values: Mapping[str, Optional[str]], languages: Mapping[str, int]) -> Dict[str, Any]:
    field: Dict[str, Any] = {"__lang__": True}
    for iso, lang_id in languages.items():
        field[str(lang_id)] = values.get(iso)
    return field


def gtin_fields(gtin: Optional[str]) -> Dict[str, str]:
    """PrestaShop has separate EAN-13 and UPC fields."""
    if not gtin:
        return {"ean13": "", "upc": ""}
    digits = gtin.lstrip("0") if len(gtin) == 14 else gtin
    if len(gtin) == 12:
        return {"ean13": "", "upc": gtin}
    if len(gtin) == 13:
        return {"ean13": gtin, "upc": ""}
    if len(gtin) == 14 and len(digits) <= 13:
        return {"ean13": digits.zfill(13), "upc": ""}
    return {"ean13": "", "upc": ""}   # GTIN-8/14 have no PrestaShop field


def product_fields(product: Mapping[str, Any], price: Optional[Mapping[str, Any]],
                   config: Mapping[str, Any], *, active: bool) -> Dict[str, Any]:
    """Fields SignPIM owns on a PrestaShop product. Shop-owned fields are omitted."""
    languages: Mapping[str, int] = config.get("languages") or {"da": 1}
    i18n = (product.get("attributes") or {}).get("i18n") or {}

    def texts(field: str, fallback: Optional[str]) -> Dict[str, Optional[str]]:
        return {iso: (i18n.get(iso) or {}).get(field) or fallback for iso in languages}

    names = texts("name", product["name"])
    category_map: Mapping[str, Any] = config.get("category_map") or {}
    category_id = category_map.get(product.get("category") or "", config.get("default_category_id", 2))
    fields: Dict[str, Any] = {
        "reference": product["master_sku"],
        **gtin_fields(product.get("gtin")),
        "mpn": product.get("mpn") or "",
        "name": _lang_field(names, languages),
        "link_rewrite": _lang_field({iso: slugify(names[iso] or product["master_sku"]) for iso in languages},
                                    languages),
        "description": _lang_field(texts("description", product.get("description") or ""), languages),
        "id_category_default": category_id,
        "associations": {"categories": [{"category": {"id": category_id}}]},
        "id_tax_rules_group": config.get("id_tax_rules_group", 1),
        "active": active,
        "state": 1,                   # 0 would leave the product as a back-office draft
        "available_for_order": active,
        "show_price": True,
        "visibility": "both",
    }
    manufacturer_id = (config.get("manufacturer_map") or {}).get(product.get("brand") or "")
    if manufacturer_id:
        fields["id_manufacturer"] = manufacturer_id
    if price is not None:
        fields["price"] = price["price_ex_vat"]
        if price.get("cost_eur") is not None:
            fields["wholesale_price"] = price["cost_eur"]
    return fields


def specific_price_fields(product_external_id: str, currency_id: Any, price_ex_vat: str) -> Dict[str, Any]:
    """Fixed price in a given currency (rounded local price, ADR-017)."""
    return {
        "id_product": product_external_id,
        "id_product_attribute": 0,
        "id_shop": 0,
        "id_shop_group": 0,
        "id_cart": 0,
        "id_currency": currency_id,
        "id_country": 0,
        "id_group": 0,
        "id_customer": 0,
        "price": price_ex_vat,
        "from_quantity": 1,
        "reduction": 0,
        "reduction_tax": 0,
        "reduction_type": "amount",
        "from": "0000-00-00 00:00:00",
        "to": "0000-00-00 00:00:00",
    }


def content_hash(value: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
