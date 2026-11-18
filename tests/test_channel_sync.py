"""PrestaShop channel connector (ADR-018) against an in-memory Webservice."""
import hashlib
import hmac
import io
import json
import time
from datetime import date
from decimal import Decimal as D

import pytest
from PIL import Image
from sqlalchemy import select

from core.models import ExternalRef, StockReservation, SyncJob
from core.persistence import PersistenceService
from core.pricing import PricingService
from core.product_master import ProductMasterService
from services.channels.prestashop import PrestaShopWebservice, gtin_fields, slugify, to_xml
from services.channels.sync import ChannelSyncService, EventRejected
from services.supplier_adapters import SupplierHubService
from tests.fake_prestashop import FakePrestaShop

TODAY = date(2026, 11, 18)


def png(color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNPIM_PS_A_KEY", "KEY")
    monkeypatch.setenv("SIGNPIM_PS_B_KEY", "KEY")
    monkeypatch.setenv("SIGNPIM_PS_A_HOOK", "hook-secret")
    persistence = PersistenceService(dsn=f"sqlite:///{tmp_path}/sync.db")
    tenant = persistence.get_or_create_tenant("acme").id
    shops = {"a": FakePrestaShop(), "b": FakePrestaShop()}
    pricing = PricingService(persistence)
    catalog = ProductMasterService(persistence)
    channels = {}
    for code in ("a", "b"):
        ch = pricing.create_channel(tenant, f"shop-{code}", f"Shop {code}", vat_rate_pct=25,
                                    rounding={"DKK": {"step": "10", "minus": "0.05"}})
        channels[code] = ch
    by_id = {channels[c]["id"]: shops[c] for c in shops}

    def factory(channel):
        return PrestaShopWebservice(channel.base_url, "KEY", transport=by_id[channel.id].transport)

    sync = ChannelSyncService(persistence, client_factory=factory, media_root=str(tmp_path / "media"))
    for code in ("a", "b"):
        sync.connect(tenant, channels[code]["id"], base_url=f"https://shop-{code}.example",
                     credential_ref=f"env:SIGNPIM_PS_{code.upper()}_KEY",
                     webhook_secret_ref="env:SIGNPIM_PS_A_HOOK" if code == "a" else None,
                     connector_config={"languages": {"da": 1, "en": 2}, "id_tax_rules_group": 7,
                                       "category_map": {"tools": 12}, "currency_ids": {"DKK": 3}})
    pricing.set_exchange_rate(tenant, "DKK", "7.46", valid_on=TODAY)
    pricing.create_rule(tenant, "markup", 30)
    supplier = catalog.create_supplier(tenant, "wwtech", "WWTech", "csv", {"delimiter": ";"})
    product = catalog.upsert_product_master(
        tenant, "M-1", "Boremaskine", gtin="4006381333931", category="tools", brand="Bosch",
        attributes={"i18n": {"en": {"name": "Drill"}}},
    )
    hub = SupplierHubService(persistence)
    yield {"p": persistence, "t": tenant, "shops": shops, "channels": channels, "sync": sync,
           "catalog": catalog, "pricing": pricing, "supplier": supplier, "product": product, "hub": hub}
    persistence.engine.dispose()


def feed(env, qty=5, price="100"):
    return env["hub"].import_payload(
        env["supplier"]["id"], f"sku;ean;price;quantity\nA-1;4006381333931;{price};{qty}\n",
        expected_tenant_id=env["t"])


def run(env):
    return env["sync"].process_due(limit=100)


def ps_product(env, shop="a"):
    products = env["shops"][shop].store["products"]
    assert len(products) == 1
    return next(iter(products.values()))


# ---------------------------------------------------------------- mapping
def test_xml_has_language_fields_and_associations():
    xml = to_xml("products", {"name": {"__lang__": True, "1": "Bor", "2": "Drill"},
                              "associations": {"categories": [{"category": {"id": 12}}]},
                              "active": True})
    text = xml.decode()
    assert '<language id="1">Bor</language>' in text and "<category><id>12</id></category>" in text
    assert "<active>1</active>" in text


def test_gtin_fields_and_slug():
    assert gtin_fields("4006381333931") == {"ean13": "4006381333931", "upc": ""}
    assert gtin_fields("012345678905") == {"ean13": "", "upc": "012345678905"}
    assert slugify("Rød Æblekasse – 2 stk") == "roed-aeblekasse-2-stk"


# ---------------------------------------------------------------- outbound
def test_product_is_created_in_both_shops_with_price_stock_and_dkk_price(env):
    feed(env, qty=5)
    summary = run(env)
    assert summary["done"] == 2 and summary["dead"] == 0
    for shop in ("a", "b"):
        p = ps_product(env, shop)
        assert p["reference"] == "M-1" and p["ean13"] == "4006381333931"
        assert D(p["price"]) == D("130") and D(p["wholesale_price"]) == D("100")
        assert p["name"] == {"1": "Boremaskine", "2": "Drill"}
        assert p["id_category_default"] == "12" and p["id_tax_rules_group"] == "7"
        assert p["active"] == "1" and p["state"] == "1"
        stock = [s for s in env["shops"][shop].store["stock_availables"].values() if s["id_product"] == p["id"]]
        assert stock[0]["quantity"] == "5"
        sp = list(env["shops"][shop].store["specific_prices"].values())
        assert sp[0]["id_currency"] == "3" and D(sp[0]["price"]) == D("975.96")


def test_unchanged_product_is_not_resent(env):
    feed(env)
    run(env)
    calls_before = len(env["shops"]["a"].calls)
    env["sync"].sync_product(_channel(env, "a"), env["product"]["id"], _client(env, "a"))
    new_calls = env["shops"]["a"].calls[calls_before:]
    assert not any(c.startswith(("POST", "PATCH")) for c in new_calls)


def test_cost_change_flows_to_shop_price(env):
    feed(env, price="100")
    run(env)
    feed(env, price="105")          # +5 %: below approval threshold
    assert run(env)["done"] == 2
    assert D(ps_product(env)["price"]) == D("136.50")


def test_large_cost_change_keeps_old_price_until_approved(env):
    feed(env, price="100")
    run(env)
    feed(env, price="150")
    run(env)
    assert D(ps_product(env)["price"]) == D("130.00")      # waiting for approval
    pending = env["pricing"].calculate(env["t"], env["channels"]["a"]["id"], env["product"]["id"])
    assert pending["status"] == "pending_review"
    env["pricing"].approve(env["t"], pending["id"], "søren")
    run(env)
    assert D(ps_product(env, "a")["price"]) == D("195.00")
    assert D(ps_product(env, "b")["price"]) == D("130.00")   # approval was for shop A only


def test_no_price_rule_means_product_is_not_created(env):
    with env["p"].session() as s:
        from core.models import PriceRule
        for rule in s.scalars(select(PriceRule)).all():
            rule.active = False
    feed(env)
    run(env)
    assert env["shops"]["a"].store["products"] == {}


def test_delisted_product_is_deactivated_not_deleted(env):
    feed(env)
    run(env)
    with env["p"].session() as s:
        from core.models import SupplierOffer
        s.scalars(select(SupplierOffer)).one().active = False
    from core.outbox import enqueue
    enqueue(env["p"], env["t"], [env["product"]["id"]], reason="delisted")
    run(env)
    drill = ps_product(env, "a")
    assert drill["active"] == "0" and drill["available_for_order"] == "0"


# ---------------------------------------------------------------- images
def test_images_are_uploaded_in_order_and_reordered(env):
    feed(env)
    red = env["sync"].add_media(env["t"], env["product"]["id"], png((255, 0, 0)))
    blue = env["sync"].add_media(env["t"], env["product"]["id"], png((0, 0, 255)))
    assert env["sync"].add_media(env["t"], env["product"]["id"], png((255, 0, 0)))["duplicate"]
    run(env)
    pid = ps_product(env)["id"]
    assert len(env["shops"]["a"].images[pid]) == 2
    first_ids = [i["id"] for i in env["shops"]["a"].images[pid]]

    green = env["sync"].add_media(env["t"], env["product"]["id"], png((0, 255, 0)))
    run(env)                                    # appended: incremental
    ids = [i["id"] for i in env["shops"]["a"].images[pid]]
    assert ids[:2] == first_ids and len(ids) == 3

    env["sync"].reorder_media(env["t"], env["product"]["id"], [green["id"], red["id"], blue["id"]])
    run(env)                                    # reorder: full re-upload
    assert len(env["shops"]["a"].images[pid]) == 3
    assert not set(first_ids) & {i["id"] for i in env["shops"]["a"].images[pid]}

    env["sync"].delete_media(env["t"], env["product"]["id"], blue["id"])
    run(env)
    assert len(env["shops"]["a"].images[pid]) == 2


def test_media_rejects_non_images(env):
    from services.channels.sync import ChannelSyncError
    with pytest.raises(ChannelSyncError, match="unsupported"):
        env["sync"].add_media(env["t"], env["product"]["id"], b"%PDF-1.4")


# ---------------------------------------------------------------- reliability
def test_server_error_is_retried_then_succeeds(env):
    feed(env)
    env["shops"]["a"].fail_next = [503]
    summary = run(env)
    assert summary["retry"] == 1 and summary["done"] == 1
    with env["p"].session() as s:
        job = s.scalars(select(SyncJob).where(SyncJob.status == "pending")).one()
        job.next_attempt_at = job.created_at
    assert run(env)["done"] == 1
    assert ps_product(env, "a")["reference"] == "M-1"


def test_client_error_is_dead_not_retried(env):
    feed(env)
    env["shops"]["a"].fail_next = [400]
    summary = run(env)
    assert summary["dead"] == 1
    with env["p"].session() as s:
        dead = s.scalars(select(SyncJob).where(SyncJob.status == "dead")).one()
        assert "HTTP 400" in dead.last_error


def test_jobs_coalesce(env):
    from core.outbox import enqueue
    feed(env)
    enqueue(env["p"], env["t"], [env["product"]["id"]], reason="x")
    enqueue(env["p"], env["t"], [env["product"]["id"]], reason="y")
    with env["p"].session() as s:
        assert len(s.scalars(select(SyncJob).where(SyncJob.status == "pending")).all()) == 2  # 1 per shop


# ---------------------------------------------------------------- catalogue import
def test_import_links_existing_shop_products(env):
    shop = env["shops"]["a"]
    existing = shop.seed_product(reference="M-1", ean13="", upc="", name="old")
    shop.seed_product(reference="", ean13="4006381333931", upc="", name="dupe")
    shop.seed_product(reference="X", ean13="", upc="", name="unknown")
    result = env["sync"].import_catalog(env["t"], env["channels"]["a"]["id"])
    assert result["linked"] == 1 and result["unmatched_count"] == 1 and len(result["conflicts"]) == 1
    feed(env)
    run(env)
    assert D(shop.store["products"][existing]["price"]) == D("130")     # updated, not duplicated
    assert len(shop.store["products"]) == 3


# ---------------------------------------------------------------- realtime events
def signed(body: dict, secret="hook-secret", ts=None):
    raw = json.dumps(body).encode()
    ts = str(ts or int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, ts, "sha256=" + sig


def test_order_in_shop_a_lowers_stock_in_shop_b(env):
    feed(env, qty=5)
    run(env)
    ext = ps_product(env, "a")["id"]
    raw, ts, sig = signed({"event_id": "o-1", "type": "order.created",
                           "data": {"order_id": "9001", "lines": [{"product_id": ext, "quantity": 2}]}})
    out = env["sync"].handle_event(env["channels"]["a"]["id"], raw, ts, sig)
    assert out["reservations_created"] == 1
    run(env)
    for shop in ("a", "b"):
        pid = ps_product(env, shop)["id"]
        stock = [s for s in env["shops"][shop].store["stock_availables"].values() if s["id_product"] == pid]
        assert stock[0]["quantity"] == "3"
    # duplicate delivery is ignored
    assert env["sync"].handle_event(env["channels"]["a"]["id"], raw, ts, sig)["duplicate"] is True
    # next supplier feed reflects the sale -> reservation no longer counts
    feed(env, qty=3)
    run(env)
    pid = ps_product(env, "b")["id"]
    stock = [s for s in env["shops"]["b"].store["stock_availables"].values() if s["id_product"] == pid]
    assert stock[0]["quantity"] == "3"


def test_cancelled_order_releases_stock(env):
    feed(env, qty=5)
    run(env)
    ext = ps_product(env, "a")["id"]
    chan = env["channels"]["a"]["id"]
    env["sync"].handle_event(chan, *signed({"event_id": "o-1", "type": "order.created",
                             "data": {"order_id": "1", "lines": [{"product_id": ext, "quantity": 2}]}}))
    env["sync"].handle_event(chan, *signed({"event_id": "o-2", "type": "order.updated",
                             "data": {"order_id": "1", "cancelled": True}}))
    with env["p"].session() as s:
        assert s.scalars(select(StockReservation)).all() == []


def test_shop_edit_is_overwritten_by_signpim(env):
    feed(env)
    run(env)
    p = ps_product(env, "a")
    p["price"] = "1.00"                     # someone edits the price in the back office
    env["sync"].handle_event(env["channels"]["a"]["id"], *signed(
        {"event_id": "p-1", "type": "product.updated", "data": {"product_id": p["id"]}}))
    run(env)
    assert D(ps_product(env, "a")["price"]) == D("130.00")


@pytest.mark.parametrize("mutate,error", [
    (lambda raw, ts, sig: (raw, ts, "sha256=" + "0" * 64), "signature"),
    (lambda raw, ts, sig: (raw, str(int(ts) - 3600), sig), "timestamp"),
    (lambda raw, ts, sig: (raw + b" ", ts, sig), "signature"),
])
def test_event_authentication(env, mutate, error):
    raw, ts, sig = signed({"event_id": "x", "type": "order.created", "data": {"order_id": "1"}})
    with pytest.raises(EventRejected, match=error):
        env["sync"].handle_event(env["channels"]["a"]["id"], *mutate(raw, ts, sig))


def test_events_disabled_without_secret(env):
    raw, ts, sig = signed({"event_id": "x", "type": "order.created", "data": {}})
    with pytest.raises(EventRejected, match="not enabled"):
        env["sync"].handle_event(env["channels"]["b"]["id"], raw, ts, sig)


def test_connect_rejects_secrets_in_config(env):
    from services.channels.sync import ChannelSyncError
    with pytest.raises(ChannelSyncError, match="secrets"):
        env["sync"].connect(env["t"], env["channels"]["a"]["id"], base_url="https://x.example",
                            credential_ref="env:SIGNPIM_PS_A_KEY", connector_config={"ws_key": "abc"})
    with pytest.raises(ChannelSyncError, match="env:"):
        env["sync"].connect(env["t"], env["channels"]["a"]["id"], base_url="https://x.example",
                            credential_ref="abc123")
    with pytest.raises(ChannelSyncError, match="https"):
        env["sync"].connect(env["t"], env["channels"]["a"]["id"], base_url="http://shop.example",
                            credential_ref="env:SIGNPIM_PS_A_KEY")


# ---------------------------------------------------------------- helpers
def _channel(env, code):
    from core.models import Channel
    with env["p"].session() as s:
        ch = s.get(Channel, env["channels"][code]["id"])
        s.expunge(ch)
        return ch


def _client(env, code):
    return PrestaShopWebservice("https://x", "KEY", transport=env["shops"][code].transport)


def test_update_keeps_shop_seo_url_and_extra_categories(env):
    feed(env)
    run(env)
    p = ps_product(env, "a")
    assert "link_rewrite" in p and "associations" in p      # set on create
    p["link_rewrite"] = {"1": "shop-chosen-url"}
    p["associations"] = "<shop-categories/>"
    feed(env, price="104")
    run(env)
    p = ps_product(env, "a")
    assert D(p["price"]) == D("135.20")
    assert p["link_rewrite"] == {"1": "shop-chosen-url"} and p["associations"] == "<shop-categories/>"
