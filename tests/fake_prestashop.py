"""In-memory PrestaShop Webservice for tests (httpx.MockTransport)."""
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

import httpx


def _parse(obj: ET.Element) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for child in obj:
        langs = child.findall("language")
        if langs:
            out[child.tag] = {lang.get("id"): lang.text or "" for lang in langs}
        elif len(child):
            out[child.tag] = ET.tostring(child, encoding="unicode")
        else:
            out[child.tag] = child.text or ""
    return out


class FakePrestaShop:
    def __init__(self, api_key: str = "KEY"):
        self.api_key = api_key
        self.store: Dict[str, Dict[str, Dict[str, Any]]] = {
            "products": {}, "stock_availables": {}, "specific_prices": {},
        }
        self.images: Dict[str, List[Dict[str, Any]]] = {}
        self.next_id = 100
        self.calls: List[str] = []
        self.fail_next: List[int] = []
        self.transport = httpx.MockTransport(self.handle)

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def seed_product(self, **fields) -> str:
        pid = self._id()
        self.store["products"][pid] = {"id": pid, **fields}
        sid = self._id()
        self.store["stock_availables"][sid] = {"id": sid, "id_product": pid, "id_product_attribute": "0", "quantity": "0"}
        return pid

    def handle(self, request: httpx.Request) -> httpx.Response:
        import base64
        auth = request.headers.get("Authorization", "")
        if auth != "Basic " + base64.b64encode(f"{self.api_key}:".encode()).decode():
            return httpx.Response(401, text="unauthorized")
        path = request.url.path.split("/api", 1)[1].strip("/")
        self.calls.append(f"{request.method} {path}")
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), text="boom")
        parts = path.split("/") if path else []
        if not parts:
            return httpx.Response(200, json={"api": "ok"})

        if parts[0] == "images":
            pid = parts[2]
            if request.method == "POST":
                body = request.content
                assert b'name="image"' in body
                image_id = self._id()
                self.images.setdefault(pid, []).append({"id": image_id, "bytes": len(body)})
                return httpx.Response(200, text=f'<prestashop><image><id>{image_id}</id></image></prestashop>')
            if request.method == "DELETE":
                self.images[pid] = [i for i in self.images.get(pid, []) if i["id"] != parts[3]]
                return httpx.Response(200, text="")

        resource = parts[0]
        table = self.store[resource]
        if request.method == "GET":
            params = dict(request.url.params)
            rows = list(table.values()) if len(parts) == 1 else [table[parts[1]]]
            for key, value in params.items():
                m = re.match(r"filter\[(\w+)\]", key)
                if m:
                    rows = [r for r in rows if str(r.get(m.group(1))) == value.strip("[]")]
            if "limit" in params:
                offset, count = (int(x) for x in params["limit"].split(","))
                rows = sorted(rows, key=lambda r: int(r["id"]))[offset:offset + count]
            if not rows:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={resource: rows})
        root = ET.fromstring(request.content)
        fields = _parse(root[0])
        if request.method == "POST":
            new_id = self._id()
            fields["id"] = new_id
            table[new_id] = fields
            if resource == "products":
                sid = self._id()
                self.store["stock_availables"][sid] = {
                    "id": sid, "id_product": new_id, "id_product_attribute": "0", "quantity": "0"}
                self.images.setdefault(new_id, [])
            singular = {"products": "product", "specific_prices": "specific_price"}[resource]
            return httpx.Response(201, text=json.dumps({singular: {"id": int(new_id)}}))
        if request.method == "PATCH":
            object_id = parts[1]
            if object_id not in table:
                return httpx.Response(404, text="not found")
            table[object_id].update({k: v for k, v in fields.items() if k != "id"})
            return httpx.Response(200, text="{}")
        return httpx.Response(405)
