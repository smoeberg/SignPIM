"""HTTP flow for the pricing engine: supplier feed -> match -> price -> approve -> publish."""
import pytest
from fastapi.testclient import TestClient

from api import app as app_mod
from core.auth import AuthService
from core.persistence import PersistenceService


@pytest.fixture
def clients(tmp_path):
    app_mod.persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/pricing.db")
    app_mod._auth = None
    app_mod._users = None
    tenant = app_mod.persistence.get_or_create_tenant("acme")
    auth = AuthService(app_mod.persistence)
    admin = TestClient(app_mod.app)
    admin.headers.update({"Authorization": f"Bearer {auth.create_key(tenant.id, 'a', role='admin')['key']}"})
    writer = TestClient(app_mod.app)
    writer.headers.update({"Authorization": f"Bearer {auth.create_key(tenant.id, 'w', role='writer')['key']}"})
    return admin, writer


def test_full_pricing_flow(clients):
    admin, writer = clients
    q = "?tenant=acme"
    supplier = admin.post(f"/catalog/suppliers{q}", json={
        "code": "wwtech", "name": "WWTech", "adapter_key": "csv",
        "adapter_config": {"delimiter": ";", "rrp_includes_vat": True},
        "landed_cost_fixed_eur": "2.50",
    }).json()
    product = admin.post(f"/catalog/products{q}", json={
        "master_sku": "M-1", "name": "Drill", "gtin": "4006381333931", "category": "tools",
    }).json()
    imported = admin.post(f"/catalog/suppliers/{supplier['id']}/import{q}", json={
        "payload": "sku;ean;price;currency;quantity;rrp\nA-1;4006381333931;100;EUR;4;199\n",
    }).json()
    assert imported["gtin_matches"] == 1

    channel = admin.post(f"/catalog/channels{q}", json={
        "code": "shop-a", "name": "Shop A", "vat_rate_pct": "25",
        "rounding": {"DKK": {"step": "10", "minus": "0.05"}},
    })
    assert channel.status_code == 200, channel.text
    channel = channel.json()
    assert admin.put(f"/catalog/exchange-rates{q}", json={"currency": "DKK", "rate": "7.46"}).status_code == 200
    rule = admin.post(f"/catalog/price-rules{q}", json={"method": "markup", "value": "30", "category": "tools"})
    assert rule.status_code == 200, rule.text
    assert admin.post(f"/catalog/price-floors{q}", json={"min_margin_pct": "15"}).status_code == 200

    calc = writer.post(f"/catalog/channels/{channel['id']}/prices/calculate{q}",
                       json={"product_id": product["id"]})
    assert calc.status_code == 200, calc.text
    calc = calc.json()
    # (100 + 2.50) * 1.30 = 133.25
    assert calc["status"] == "approved" and calc["price_ex_vat"] == "133.25"
    assert calc["breakdown"]["local_prices"]["DKK"]["incl_vat"] == "1249.95"
    assert calc["breakdown"]["rrp_ex_vat_eur"] == "159.200000"

    published = writer.post(f"/catalog/price-calculations/{calc['id']}/published{q}")
    assert published.status_code == 200, published.text
    lowest = writer.get(f"/catalog/channels/{channel['id']}/prices/{product['id']}/lowest{q}")
    assert lowest.json()["price_ex_vat"] == "133.250000"

    summary = writer.post(f"/catalog/channels/{channel['id']}/prices/calculate{q}", json={}).json()
    assert summary == {"products": 1, "approved": 0, "pending_review": 0, "blocked": 0, "unchanged": 1}


def test_writer_cannot_change_pricing_rules_or_approve(clients):
    _, writer = clients
    q = "?tenant=acme"
    assert writer.post(f"/catalog/price-rules{q}", json={"method": "markup", "value": "1"}).status_code == 403
    assert writer.post(f"/catalog/channels{q}", json={"code": "x", "name": "X"}).status_code == 403
    assert writer.post(f"/catalog/price-calculations/x/approve{q}").status_code == 403


def test_invalid_rule_is_rejected(clients):
    admin, _ = clients
    response = admin.post("/catalog/price-rules?tenant=acme", json={"method": "margin", "value": "120"})
    assert response.status_code == 400 and "below 100" in response.text
