"""Pricing engine (ADR-017)."""
from datetime import date
from decimal import Decimal

import pytest

from core.persistence import PersistenceService
from core.pricing import (
    PricingError, PricingService, apply_rule, margin_of, markup_of, round_up_price,
)
from core.product_master import ProductMasterService

TODAY = date(2026, 11, 18)


# ---------------------------------------------------------------- pure maths
def test_markup_and_margin_are_different_things():
    cost = Decimal("100")
    markup_price = apply_rule(cost, "markup", Decimal("30"))
    assert markup_price == Decimal("130")
    assert margin_of(markup_price, cost) == Decimal("23.0769")
    margin_price = apply_rule(cost, "margin", Decimal("30"))
    assert margin_of(margin_price, cost) == Decimal("30.0000")
    assert markup_of(margin_price, cost) == Decimal("42.8571")


def test_margin_of_100_percent_is_rejected():
    with pytest.raises(PricingError):
        apply_rule(Decimal("1"), "margin", Decimal("100"))


@pytest.mark.parametrize("value,step,minus,expected", [
    ("129.31", "1", "0.05", "129.95"),
    ("129.95", "1", "0.05", "129.95"),
    ("129.96", "1", "0.05", "130.95"),
    ("1212.25", "10", "0.05", "1219.95"),
    ("12.01", "1", "0", "13"),
])
def test_round_up_price(value, step, minus, expected):
    assert round_up_price(Decimal(value), Decimal(step), Decimal(minus)) == Decimal(expected)


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def env():
    persistence = PersistenceService(dsn="sqlite:///:memory:")
    tenant = persistence.get_or_create_tenant("pricing")
    catalog = ProductMasterService(persistence)
    pricing = PricingService(persistence, today=TODAY)
    supplier = catalog.create_supplier(tenant.id, "wwtech", "WWTech", "csv")
    product = catalog.upsert_product_master(
        tenant.id, "M-1", "Drill", gtin="4006381333931", category="tools"
    )
    channel = pricing.create_channel(tenant.id, "shop-a", "Shop A", vat_rate_pct=25)

    def offer(sku="S1", cost="100", currency="EUR", stock=5, sup=None, **kw):
        o = catalog.upsert_supplier_offer(
            (sup or supplier)["id"], sku, {"sku": sku, "gtin": "4006381333931"},
            cost_price=cost, currency=currency, stock_quantity=stock, **kw,
        )
        catalog.match_offer_by_gtin(o["id"])
        return o

    yield {
        "p": persistence, "t": tenant.id, "catalog": catalog, "pricing": pricing,
        "supplier": supplier, "product": product, "channel": channel, "offer": offer,
    }
    persistence.engine.dispose()


def calc(env, channel=None):
    return env["pricing"].calculate(env["t"], (channel or env["channel"])["id"], env["product"]["id"])


# ---------------------------------------------------------------- engine
def test_example_100_eur_with_30_percent_markup(env):
    env["offer"]()
    env["pricing"].create_rule(env["t"], "markup", 30, category="tools")
    result = calc(env)
    assert result["status"] == "approved"
    assert Decimal(result["price_ex_vat"]) == Decimal("130.00")
    assert result["margin_pct"] == "23.0769"
    assert result["markup_pct"] == "30.0000"


def test_rule_precedence_most_specific_wins(env):
    env["offer"]()
    t, ch, pid = env["t"], env["channel"]["id"], env["product"]["id"]
    pr = env["pricing"]
    pr.create_rule(t, "markup", 10)                                   # tenant default
    assert Decimal(calc(env)["price_ex_vat"]) == Decimal("110.00")
    pr.create_rule(t, "markup", 20, channel_id=ch)                    # channel default
    assert Decimal(calc(env)["price_ex_vat"]) == Decimal("120.00")
    pr.create_rule(t, "markup", 30, category="tools")                 # category
    assert Decimal(calc(env)["price_ex_vat"]) == Decimal("130.00")
    pr.create_rule(t, "markup", 40, category="tools", channel_id=ch)  # category+channel
    pr.create_rule(t, "markup", 50, product_id=pid)                   # product
    pr.create_rule(t, "markup", 60, product_id=pid, channel_id=ch)    # product+channel
    pr.create_rule(t, "fixed", 199, product_id=pid)                   # fixed product
    result = calc(env)
    assert result["breakdown"]["rule_scope"] == "fixed product"
    assert Decimal(result["price_ex_vat"]) == Decimal("199.00")


def test_fixed_price_requires_product_scope(env):
    with pytest.raises(PricingError, match="product scope"):
        env["pricing"].create_rule(env["t"], "fixed", 199, category="tools")


def test_rules_differ_per_channel(env):
    env["offer"]()
    shop_b = env["pricing"].create_channel(env["t"], "shop-b", "Shop B", vat_rate_pct=25)
    env["pricing"].create_rule(env["t"], "markup", 30, channel_id=env["channel"]["id"])
    env["pricing"].create_rule(env["t"], "markup", 45, channel_id=shop_b["id"])
    assert Decimal(calc(env)["price_ex_vat"]) == Decimal("130.00")
    assert Decimal(calc(env, shop_b)["price_ex_vat"]) == Decimal("145.00")


def test_floor_blocks_and_cannot_be_approved(env):
    env["offer"]()
    env["pricing"].create_rule(env["t"], "fixed", 105, product_id=env["product"]["id"])
    env["pricing"].create_floor(env["t"], min_margin_pct=15)
    result = calc(env)
    assert result["status"] == "blocked"
    assert "below minimum margin" in result["reasons"][0]
    with pytest.raises(PricingError, match="blocked"):
        env["pricing"].approve(env["t"], result["id"], "søren")


def test_strictest_floor_wins(env):
    env["offer"]()
    env["pricing"].create_rule(env["t"], "markup", 30)
    env["pricing"].create_floor(env["t"], min_price=100)
    env["pricing"].create_floor(env["t"], min_price=140, category="tools")
    result = calc(env)
    assert result["status"] == "blocked" and "minimum price" in result["reasons"][0]


def test_fx_conversion_buffer_and_landed_cost(env):
    t = env["t"]
    supplier = env["catalog"].create_supplier(
        t, "dk", "DK supplier", "csv", landed_cost_pct=2, landed_cost_fixed_eur=5
    )
    channel = env["pricing"].create_channel(t, "shop-fx", "FX", fx_buffer_pct=1)
    env["pricing"].set_exchange_rate(t, "DKK", "7.46", valid_on=TODAY)
    env["offer"](cost="746", currency="DKK", sup=supplier)
    env["pricing"].create_rule(t, "markup", 0)
    result = calc(env, channel)
    # 746 DKK / 7.46 = 100 EUR, +1 % FX buffer = 101, +2 % = 103.02, +5 = 108.02
    assert result["breakdown"]["landed_cost_eur"] == "108.020000"
    assert Decimal(result["price_ex_vat"]) == Decimal("108.02")


def test_missing_exchange_rate_blocks(env):
    env["offer"](cost="100", currency="SEK")
    env["pricing"].create_rule(env["t"], "markup", 30)
    result = calc(env)
    assert result["status"] == "blocked"
    assert any("SEK" in w for w in result["warnings"])


def test_cheapest_in_stock_offer_wins_and_preferred_supplier_overrides(env):
    t = env["t"]
    other = env["catalog"].create_supplier(t, "other", "Other", "csv")
    env["offer"](sku="A", cost="100", stock=5)
    env["offer"](sku="B", cost="80", stock=0, sup=other)     # cheaper, but no stock
    env["pricing"].create_rule(t, "markup", 0)
    assert Decimal(calc(env)["cost_eur"]) == Decimal("100")
    env["offer"](sku="B", cost="80", stock=3, sup=other)
    assert Decimal(calc(env)["cost_eur"]) == Decimal("80")
    preferring = env["pricing"].create_channel(
        t, "pref", "Pref", preferred_supplier_id=env["supplier"]["id"]
    )
    result = calc(env, preferring)
    assert result["breakdown"]["selection"] == "preferred_supplier"
    assert Decimal(result["cost_eur"]) == Decimal("100")


def test_no_stock_goes_to_review(env):
    env["offer"](stock=0)
    env["pricing"].create_rule(env["t"], "markup", 30)
    result = calc(env)
    assert result["status"] == "pending_review" and "no offer in stock" in result["reasons"]


def test_large_change_requires_approval_and_supersedes(env):
    t = env["t"]
    env["offer"](cost="100")
    env["pricing"].create_rule(t, "markup", 30)
    first = calc(env)
    assert first["status"] == "approved"
    env["offer"](cost="105")                       # +5 % -> under threshold
    assert calc(env)["status"] == "approved"
    env["offer"](cost="130")                       # big jump
    pending = calc(env)
    assert pending["status"] == "pending_review"
    assert "exceeds approval threshold" in pending["reasons"][0]
    env["offer"](cost="131")
    newer = calc(env)
    approved = env["pricing"].approve(t, newer["id"], "søren")
    assert approved["status"] == "approved" and approved["reviewed_by"] == "søren"
    from core.models import PriceCalculation
    with env["p"].session() as s:
        assert s.get(PriceCalculation, pending["id"]).status == "superseded"


def test_identical_recalculation_is_not_duplicated(env):
    env["offer"]()
    env["pricing"].create_rule(env["t"], "markup", 30)
    first = calc(env)
    second = calc(env)
    assert second["unchanged"] is True and second["id"] == first["id"]


def test_eur_rounding_is_done_incl_vat(env):
    env["offer"](cost="100")
    channel = env["pricing"].create_channel(
        env["t"], "round", "Round", vat_rate_pct=25,
        rounding={"EUR": {"step": "1", "minus": "0.05"}},
    )
    env["pricing"].create_rule(env["t"], "markup", 30)
    result = calc(env, channel)
    # 130 ex VAT -> 162.50 incl. -> 162.95 incl. -> 130.36 ex VAT
    assert result["breakdown"]["local_prices"]["EUR"]["incl_vat"] == "162.95"
    assert Decimal(result["price_ex_vat"]) == Decimal("130.360000")


def test_local_currency_fixed_price_for_round_dkk(env):
    env["offer"](cost="100")
    env["pricing"].set_exchange_rate(env["t"], "DKK", "7.46", valid_on=TODAY)
    channel = env["pricing"].create_channel(
        env["t"], "dk", "DK", vat_rate_pct=25,
        rounding={"DKK": {"step": "10", "minus": "0.05"}},
    )
    env["pricing"].create_rule(env["t"], "markup", 30)
    result = calc(env, channel)
    dkk = result["breakdown"]["local_prices"]["DKK"]
    # 130 EUR * 7.46 * 1.25 = 1212.25 -> 1219.95 incl. VAT
    assert dkk["incl_vat"] == "1219.95" and dkk["ex_vat"] == "975.960000"
    assert Decimal(result["price_ex_vat"]) == Decimal("130.00")


def test_rrp_warning(env):
    env["offer"](rrp="150", rrp_currency="EUR", rrp_includes_vat=True)
    env["pricing"].create_rule(env["t"], "markup", 30)
    result = calc(env)
    assert result["breakdown"]["rrp_ex_vat_eur"] == "120.000000"
    assert "price is above supplier RRP" in result["warnings"]


def test_published_history_gives_lowest_30_day_price(env):
    t = env["t"]
    env["offer"](cost="100")
    env["pricing"].create_rule(t, "markup", 30)
    first = calc(env)
    env["pricing"].record_published(t, first["id"])
    env["offer"](cost="95")
    second = calc(env)
    env["pricing"].record_published(t, second["id"])
    lowest = env["pricing"].lowest_published(t, env["channel"]["id"], env["product"]["id"])
    assert Decimal(lowest["price_ex_vat"]) == Decimal("123.50")


def test_only_approved_prices_can_be_published(env):
    env["offer"](stock=0)
    env["pricing"].create_rule(env["t"], "markup", 30)
    result = calc(env)
    with pytest.raises(PricingError, match="approved"):
        env["pricing"].record_published(env["t"], result["id"])


def test_channel_base_currency_must_be_eur(env):
    with pytest.raises(PricingError, match="EUR"):
        env["pricing"].create_channel(env["t"], "x", "X", base_currency="DKK")


def test_calculate_channel_summary(env):
    env["offer"]()
    env["pricing"].create_rule(env["t"], "markup", 30)
    summary = env["pricing"].calculate_channel(env["t"], env["channel"]["id"])
    assert summary["products"] == 1 and summary["approved"] == 1
    again = env["pricing"].calculate_channel(env["t"], env["channel"]["id"])
    assert again["unchanged"] == 1


def test_tenant_isolation(env):
    other = env["p"].get_or_create_tenant("other")
    with pytest.raises(PricingError, match="not found"):
        env["pricing"].calculate(other.id, env["channel"]["id"], env["product"]["id"])
    with pytest.raises(PricingError, match="not found"):
        env["pricing"].create_rule(other.id, "markup", 10, channel_id=env["channel"]["id"])
