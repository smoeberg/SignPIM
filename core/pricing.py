"""Central pricing engine (ADR-017).

Calculates one selling price excl. VAT in EUR per ProductMaster and Channel:

    cost (supplier currency) -> EUR (+FX buffer) -> landed cost
    -> price rule (markup | margin | fixed) -> optional rounding
    -> floor check -> change check -> PriceCalculation

VAT and the customer's currency are applied by the shop. The channel VAT rate
is only used to compute psychological prices incl. VAT.

All arithmetic uses Decimal. Markup (avance) and margin (dækningsgrad) are
never interchangeable: every rule names its method explicitly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import or_, select

from core.models import (
    Channel,
    ExchangeRate,
    PriceCalculation,
    PriceFloor,
    PriceRule,
    ProductMaster,
    PublishedPrice,
    Supplier,
    SupplierOffer,
)

EUR = "EUR"
METHODS = ("markup", "margin", "fixed")
IN_STOCK_STATUSES = {"in_stock", "instock", "available", "on_stock", "på lager"}
PRICE_Q = Decimal("0.000001")     # PrestaShop stores 6 decimals excl. VAT
RATIO_Q = Decimal("0.0001")
MONEY_Q = Decimal("0.01")


class PricingError(ValueError):
    """Invalid pricing configuration or request."""


# ---------------------------------------------------------------------------
# Pure arithmetic (no database) — unit-testable in isolation.
# ---------------------------------------------------------------------------

def to_decimal(value: Any, field: str, *, allow_none: bool = True) -> Optional[Decimal]:
    if value is None or value == "":
        if allow_none:
            return None
        raise PricingError(f"{field} is required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PricingError(f"{field} must be a valid decimal")
    if not result.is_finite():
        raise PricingError(f"{field} must be finite")
    return result


def apply_rule(cost: Decimal, method: str, value: Decimal) -> Decimal:
    """Return the price excl. VAT for a cost and a rule. Percentages: 30 = 30 %.

    markup: cost * (1 + v/100)   30 % markup on 100 -> 130 (margin 23.08 %)
    margin: cost / (1 - v/100)   30 % margin on 100 -> 142.857 (margin 30 %)
    fixed:  v (EUR excl. VAT)
    """
    if method == "markup":
        return cost * (1 + value / 100)
    if method == "margin":
        if value >= 100:
            raise PricingError("margin must be below 100 %")
        return cost / (1 - value / 100)
    if method == "fixed":
        return value
    raise PricingError(f"unknown price rule method: {method}")


def margin_of(price: Decimal, cost: Decimal) -> Optional[Decimal]:
    """Dækningsgrad in percent: (price - cost) / price * 100."""
    if price <= 0:
        return None
    return ((price - cost) / price * 100).quantize(RATIO_Q, rounding=ROUND_HALF_UP)


def markup_of(price: Decimal, cost: Decimal) -> Optional[Decimal]:
    """Avance in percent: (price - cost) / cost * 100."""
    if cost <= 0:
        return None
    return ((price - cost) / cost * 100).quantize(RATIO_Q, rounding=ROUND_HALF_UP)


def round_up_price(value: Decimal, step: Decimal, minus: Decimal) -> Decimal:
    """Round up to the next price of the form n*step - minus.

    step=1,  minus=0.05 -> x.95       (129.31 -> 129.95)
    step=10, minus=0.05 -> x9.95      (1212.25 -> 1219.95)
    step=1,  minus=0    -> whole units
    Always rounds up so rounding never reduces the margin.
    """
    if step <= 0:
        raise PricingError("rounding step must be positive")
    if minus < 0 or minus >= step:
        raise PricingError("rounding minus must be >= 0 and below step")
    units = ((value + minus) / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step - minus


def validate_rounding(rounding: Any) -> Dict[str, Dict[str, str]]:
    if rounding in (None, {}):
        return {}
    if not isinstance(rounding, dict):
        raise PricingError("rounding must be an object keyed by currency")
    result = {}
    for currency, spec in rounding.items():
        code = _currency(currency, "rounding currency")
        if not isinstance(spec, dict):
            raise PricingError(f"rounding for {code} must be an object")
        step = to_decimal(spec.get("step", "1"), f"rounding.{code}.step")
        minus = to_decimal(spec.get("minus", "0"), f"rounding.{code}.minus")
        round_up_price(Decimal("1"), step, minus)  # validates step/minus
        result[code] = {"step": str(step), "minus": str(minus)}
    return result


def _currency(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha():
        raise PricingError(f"{field} must be a three-letter ISO code")
    return value.upper()


def _ratio(value: Any, field: str, *, upper: Optional[Decimal] = None) -> Decimal:
    result = to_decimal(value, field, allow_none=False)
    if result < 0:
        raise PricingError(f"{field} cannot be negative")
    if upper is not None and result >= upper:
        raise PricingError(f"{field} must be below {upper}")
    return result


def D(value: Any, default: Any = None) -> Optional[Decimal]:
    """Decimal from a DB value (some drivers return float for NUMERIC)."""
    if value is None:
        return None if default is None else Decimal(default)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _s(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, "f")


# Precedence rank: lower wins (ADR-017 "Rule precedence").
def rule_rank(rule: PriceRule) -> int:
    if rule.method == "fixed" and rule.product_id:
        return 0 if rule.channel_id else 1
    if rule.product_id:
        return 2 if rule.channel_id else 3
    if rule.category:
        return 4 if rule.channel_id else 5
    return 6 if rule.channel_id else 7


RANK_SCOPE = {
    0: "fixed product+channel", 1: "fixed product", 2: "product+channel",
    3: "product", 4: "category+channel", 5: "category",
    6: "channel default", 7: "tenant default",
}


def _in_stock(offer: SupplierOffer) -> bool:
    if offer.stock_quantity is not None:
        return D(offer.stock_quantity) > 0
    return (offer.stock_status or "").strip().casefold() in IN_STOCK_STATUSES


def _row(obj: Any, fields: Iterable[str]) -> Dict[str, Any]:
    out = {}
    for f in fields:
        v = getattr(obj, f)
        if isinstance(v, Decimal):
            v = format(v, "f")
        elif hasattr(v, "isoformat"):
            v = v.isoformat()
        out[f] = v
    return out


CHANNEL_FIELDS = [
    "id", "tenant_id", "code", "name", "platform", "base_currency", "vat_rate_pct",
    "rounding", "fx_buffer_pct", "approval_threshold_pct", "preferred_supplier_id",
    "status",
]
RULE_FIELDS = [
    "id", "tenant_id", "channel_id", "product_id", "category", "method", "value",
    "priority", "active",
]
FLOOR_FIELDS = [
    "id", "tenant_id", "channel_id", "product_id", "category", "min_price",
    "min_margin_pct", "active",
]
CALC_FIELDS = [
    "id", "tenant_id", "channel_id", "product_id", "offer_id", "rule_id", "status",
    "price_ex_vat", "cost_eur", "margin_pct", "markup_pct", "breakdown", "reasons",
    "warnings", "reviewed_by", "reviewed_at", "created_at",
]


class PricingService:
    def __init__(self, persistence, *, today: Optional[date] = None):
        self.persistence = persistence
        self._today = today

    @property
    def today(self) -> date:
        return self._today or datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def create_channel(
        self, tenant_id: str, code: str, name: str, *,
        platform: str = "prestashop", base_currency: str = EUR,
        vat_rate_pct: Any = 0, rounding: Optional[Dict[str, Any]] = None,
        fx_buffer_pct: Any = 0, approval_threshold_pct: Any = 10,
        preferred_supplier_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not code or not code.strip() or not name or not name.strip():
            raise PricingError("channel code and name are required")
        if _currency(base_currency, "base_currency") != EUR:
            raise PricingError("v1 supports EUR as channel base currency only")
        vat = _ratio(vat_rate_pct, "vat_rate_pct", upper=Decimal(100))
        buffer = _ratio(fx_buffer_pct, "fx_buffer_pct", upper=Decimal(100))
        threshold = _ratio(approval_threshold_pct, "approval_threshold_pct")
        rounding_cfg = validate_rounding(rounding)
        with self.persistence.session() as session:
            if preferred_supplier_id:
                supplier = session.get(Supplier, preferred_supplier_id)
                if supplier is None or supplier.tenant_id != tenant_id:
                    raise PricingError("preferred supplier not found")
            if session.scalar(select(Channel).where(
                Channel.tenant_id == tenant_id, Channel.code == code.strip()
            )):
                raise PricingError("channel code already exists")
            channel = Channel(
                tenant_id=tenant_id, code=code.strip(), name=name.strip(),
                platform=platform, base_currency=EUR, vat_rate_pct=vat,
                rounding=rounding_cfg, fx_buffer_pct=buffer,
                approval_threshold_pct=threshold,
                preferred_supplier_id=preferred_supplier_id,
            )
            session.add(channel)
            session.flush()
            return _row(channel, CHANNEL_FIELDS)

    def _scope_check(self, session, tenant_id: str, channel_id, product_id, category):
        if product_id and category:
            raise PricingError("a scope is either a product or a category, not both")
        if channel_id:
            channel = session.get(Channel, channel_id)
            if channel is None or channel.tenant_id != tenant_id:
                raise PricingError("channel not found")
        if product_id:
            product = session.get(ProductMaster, product_id)
            if product is None or product.tenant_id != tenant_id:
                raise PricingError("product not found")

    def create_rule(
        self, tenant_id: str, method: str, value: Any, *,
        channel_id: Optional[str] = None, product_id: Optional[str] = None,
        category: Optional[str] = None, priority: int = 0,
    ) -> Dict[str, Any]:
        if method not in METHODS:
            raise PricingError("method must be markup, margin or fixed")
        amount = to_decimal(value, "value", allow_none=False)
        if amount < 0:
            raise PricingError("value cannot be negative")
        if method == "margin" and amount >= 100:
            raise PricingError("margin is a percentage below 100, e.g. 30 for 30 %")
        if method == "fixed" and not product_id:
            raise PricingError("fixed prices require a product scope")
        if method == "fixed" and amount == 0:
            raise PricingError("fixed price must be positive")
        category = category.strip() if isinstance(category, str) and category.strip() else None
        with self.persistence.session() as session:
            self._scope_check(session, tenant_id, channel_id, product_id, category)
            rule = PriceRule(
                tenant_id=tenant_id, channel_id=channel_id, product_id=product_id,
                category=category, method=method, value=amount, priority=priority,
            )
            session.add(rule)
            session.flush()
            return _row(rule, RULE_FIELDS)

    def deactivate_rule(self, tenant_id: str, rule_id: str) -> Dict[str, Any]:
        with self.persistence.session() as session:
            rule = session.get(PriceRule, rule_id)
            if rule is None or rule.tenant_id != tenant_id:
                raise PricingError("price rule not found")
            rule.active = False
            session.flush()
            return _row(rule, RULE_FIELDS)

    def list_rules(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.persistence.session() as session:
            rules = session.scalars(select(PriceRule).where(
                PriceRule.tenant_id == tenant_id, PriceRule.active.is_(True)
            )).all()
            ordered = sorted(rules, key=lambda r: (rule_rank(r), -r.priority))
            return [dict(_row(r, RULE_FIELDS), scope=RANK_SCOPE[rule_rank(r)]) for r in ordered]

    def create_floor(
        self, tenant_id: str, *, min_price: Any = None, min_margin_pct: Any = None,
        channel_id: Optional[str] = None, product_id: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        price = to_decimal(min_price, "min_price")
        margin = to_decimal(min_margin_pct, "min_margin_pct")
        if price is None and margin is None:
            raise PricingError("a floor needs min_price and/or min_margin_pct")
        if price is not None and price < 0:
            raise PricingError("min_price cannot be negative")
        if margin is not None and not (Decimal(0) <= margin < 100):
            raise PricingError("min_margin_pct must be between 0 and 100")
        category = category.strip() if isinstance(category, str) and category.strip() else None
        with self.persistence.session() as session:
            self._scope_check(session, tenant_id, channel_id, product_id, category)
            floor = PriceFloor(
                tenant_id=tenant_id, channel_id=channel_id, product_id=product_id,
                category=category, min_price=price, min_margin_pct=margin,
            )
            session.add(floor)
            session.flush()
            return _row(floor, FLOOR_FIELDS)

    def set_exchange_rate(
        self, tenant_id: str, currency: str, rate: Any, *, valid_on: Optional[date] = None,
        source: str = "manual",
    ) -> Dict[str, Any]:
        code = _currency(currency, "currency")
        if code == EUR:
            raise PricingError("EUR is the base currency and has no rate")
        value = to_decimal(rate, "rate", allow_none=False)
        if value <= 0:
            raise PricingError("rate must be positive")
        day = valid_on or self.today
        with self.persistence.session() as session:
            row = session.scalar(select(ExchangeRate).where(
                ExchangeRate.tenant_id == tenant_id,
                ExchangeRate.currency == code, ExchangeRate.valid_on == day,
            ))
            if row is None:
                row = ExchangeRate(tenant_id=tenant_id, currency=code, valid_on=day)
                session.add(row)
            row.rate = value
            row.source = source
            session.flush()
            return _row(row, ["currency", "rate", "valid_on", "source"])

    # ------------------------------------------------------------------
    # Calculation
    # ------------------------------------------------------------------
    def _rate(self, session, tenant_id: str, currency: str) -> Optional[Tuple[Decimal, date]]:
        if currency == EUR:
            return Decimal(1), self.today
        row = session.scalar(
            select(ExchangeRate)
            .where(
                ExchangeRate.tenant_id == tenant_id,
                ExchangeRate.currency == currency,
                ExchangeRate.valid_on <= self.today,
            )
            .order_by(ExchangeRate.valid_on.desc())
            .limit(1)
        )
        return (D(row.rate), row.valid_on) if row else None

    def _select_offer(self, session, product: ProductMaster, channel: Channel, warnings):
        offers = session.scalars(select(SupplierOffer).where(
            SupplierOffer.product_id == product.id,
            SupplierOffer.active.is_(True),
            SupplierOffer.cost_price.is_not(None),
        )).all()
        candidates = []
        for offer in offers:
            currency = (offer.currency or EUR).upper()
            rate = self._rate(session, product.tenant_id, currency)
            if rate is None:
                warnings.append(f"offer {offer.supplier_sku}: no exchange rate for {currency}")
                continue
            supplier = session.get(Supplier, offer.supplier_id)
            cost_eur = D(offer.cost_price) / rate[0]
            if currency != EUR:
                cost_eur *= 1 + D(channel.fx_buffer_pct, 0) / 100
            landed = cost_eur * (1 + D(supplier.landed_cost_pct, 0) / 100) + D(
                supplier.landed_cost_fixed_eur, 0
            )
            candidates.append({
                "offer": offer, "supplier": supplier, "currency": currency,
                "rate": rate, "cost_eur": cost_eur, "landed": landed,
                "in_stock": _in_stock(offer),
            })
        if not candidates:
            return None
        if channel.preferred_supplier_id:
            preferred = [
                c for c in candidates
                if c["supplier"].id == channel.preferred_supplier_id and c["in_stock"]
            ]
            if preferred:
                return dict(min(preferred, key=lambda c: c["landed"]), selection="preferred_supplier")
        in_stock = [c for c in candidates if c["in_stock"]]
        if in_stock:
            return dict(min(in_stock, key=lambda c: c["landed"]), selection="cheapest_in_stock")
        return dict(min(candidates, key=lambda c: c["landed"]), selection="cheapest_no_stock")

    def _applicable(self, session, model, product: ProductMaster, channel: Channel):
        return session.scalars(select(model).where(
            model.tenant_id == product.tenant_id,
            model.active.is_(True),
            or_(model.channel_id.is_(None), model.channel_id == channel.id),
            or_(model.product_id.is_(None), model.product_id == product.id),
            or_(model.category.is_(None), model.category == product.category),
        )).all()

    def _select_rule(self, session, product, channel) -> Optional[PriceRule]:
        rules = self._applicable(session, PriceRule, product, channel)
        if not rules:
            return None
        return sorted(
            rules,
            key=lambda r: (rule_rank(r), -r.priority, -(r.created_at.timestamp() if r.created_at else 0)),
        )[0]

    def _local_prices(self, session, channel, price_eur: Decimal, reasons) -> Tuple[Decimal, Dict[str, Any]]:
        """Apply EUR rounding and compute fixed local prices for other currencies."""
        vat = D(channel.vat_rate_pct, 0) / 100
        rounding = channel.rounding or {}
        local: Dict[str, Any] = {}
        if EUR in rounding:
            spec = rounding[EUR]
            gross = round_up_price(price_eur * (1 + vat), Decimal(spec["step"]), Decimal(spec["minus"]))
            price_eur = (gross / (1 + vat)).quantize(PRICE_Q, rounding=ROUND_UP)
            local[EUR] = {"incl_vat": _s(gross), "ex_vat": _s(price_eur)}
        else:
            price_eur = price_eur.quantize(MONEY_Q, rounding=ROUND_UP)
        for currency, spec in rounding.items():
            if currency == EUR:
                continue
            rate = self._rate(session, channel.tenant_id, currency)
            if rate is None:
                reasons.append(f"no exchange rate for rounding currency {currency}")
                continue
            gross = round_up_price(
                price_eur * rate[0] * (1 + vat), Decimal(spec["step"]), Decimal(spec["minus"])
            )
            local[currency] = {
                "incl_vat": _s(gross),
                "ex_vat": _s((gross / (1 + vat)).quantize(PRICE_Q, rounding=ROUND_UP)),
                "rate": _s(rate[0]), "rate_date": rate[1].isoformat(),
            }
        return price_eur, local

    def calculate(self, tenant_id: str, channel_id: str, product_id: str) -> Dict[str, Any]:
        with self.persistence.session() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or channel.tenant_id != tenant_id:
                raise PricingError("channel not found")
            product = session.get(ProductMaster, product_id)
            if product is None or product.tenant_id != tenant_id:
                raise PricingError("product not found")

            reasons: List[str] = []       # drive status (blocked / review)
            warnings: List[str] = []      # informational
            breakdown: Dict[str, Any] = {"vat_rate_pct": _s(D(channel.vat_rate_pct, 0))}
            blocked = False

            chosen = self._select_offer(session, product, channel, warnings)
            rule = self._select_rule(session, product, channel)
            if chosen is None:
                reasons.append("no active offer with cost price and exchange rate")
                blocked = True
            if rule is None:
                reasons.append("no applicable price rule")
                blocked = True

            price = cost = None
            local: Dict[str, Any] = {}
            if chosen is not None:
                cost = chosen["landed"].quantize(PRICE_Q, rounding=ROUND_HALF_UP)
                offer = chosen["offer"]
                breakdown.update({
                    "supplier_id": chosen["supplier"].id,
                    "supplier_sku": offer.supplier_sku,
                    "selection": chosen["selection"],
                    "cost_price": _s(D(offer.cost_price)),
                    "cost_currency": chosen["currency"],
                    "fx_rate": _s(chosen["rate"][0]),
                    "fx_rate_date": chosen["rate"][1].isoformat(),
                    "fx_buffer_pct": _s(D(channel.fx_buffer_pct, 0)) if chosen["currency"] != EUR else "0",
                    "cost_eur": _s(chosen["cost_eur"].quantize(PRICE_Q, rounding=ROUND_HALF_UP)),
                    "landed_cost_pct": _s(D(chosen["supplier"].landed_cost_pct, 0)),
                    "landed_cost_fixed_eur": _s(D(chosen["supplier"].landed_cost_fixed_eur, 0)),
                    "landed_cost_eur": _s(cost),
                })
                if chosen["selection"] == "cheapest_no_stock":
                    reasons.append("no offer in stock")

            if chosen is not None and rule is not None:
                raw_price = apply_rule(cost, rule.method, D(rule.value))
                breakdown.update({
                    "rule_method": rule.method,
                    "rule_value": _s(D(rule.value)),
                    "rule_scope": RANK_SCOPE[rule_rank(rule)],
                    "price_before_rounding": _s(raw_price.quantize(PRICE_Q, rounding=ROUND_HALF_UP)),
                })
                price, local = self._local_prices(session, channel, raw_price, reasons)
                breakdown["local_prices"] = local
                if price <= 0:
                    reasons.append("calculated price is not positive")
                    blocked = True

            margin = markup = None
            if price is not None and cost is not None:
                margin = margin_of(price, cost)
                markup = markup_of(price, cost)
                floors = self._applicable(session, PriceFloor, product, channel)
                min_price = max((D(f.min_price) for f in floors if f.min_price is not None), default=None)
                min_margin = max((D(f.min_margin_pct) for f in floors if f.min_margin_pct is not None), default=None)
                breakdown["floor"] = {"min_price": _s(min_price), "min_margin_pct": _s(min_margin)}
                if min_price is not None and price < min_price:
                    reasons.append(f"price {_s(price)} below minimum price {_s(min_price)}")
                    blocked = True
                if min_margin is not None and (margin is None or margin < min_margin):
                    reasons.append(f"margin {_s(margin)} % below minimum margin {_s(min_margin)} %")
                    blocked = True

                rrp = self._rrp_eur(session, chosen["offer"], channel)
                if rrp is not None:
                    breakdown["rrp_ex_vat_eur"] = _s(rrp)
                    if price > rrp:
                        warnings.append("price is above supplier RRP")

            previous = session.scalar(
                select(PriceCalculation)
                .where(
                    PriceCalculation.product_id == product.id,
                    PriceCalculation.channel_id == channel.id,
                    PriceCalculation.status == "approved",
                )
                .order_by(PriceCalculation.created_at.desc())
                .limit(1)
            )
            if previous is not None and previous.price_ex_vat is not None and price is not None:
                prev_price = D(previous.price_ex_vat)
                change = (price - prev_price) / prev_price * 100
                breakdown["previous_price_ex_vat"] = _s(previous.price_ex_vat)
                breakdown["change_pct"] = _s(change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                if abs(change) > D(channel.approval_threshold_pct, 0):
                    reasons.append(
                        f"price change {breakdown['change_pct']} % exceeds approval threshold "
                        f"{_s(D(channel.approval_threshold_pct, 0))} %"
                    )

            status = "blocked" if blocked else ("pending_review" if reasons else "approved")

            # Identical recalculation of the latest result: return it unchanged.
            latest = session.scalar(
                select(PriceCalculation)
                .where(
                    PriceCalculation.product_id == product.id,
                    PriceCalculation.channel_id == channel.id,
                    PriceCalculation.status.in_(("approved", "pending_review", "blocked")),
                )
                .order_by(PriceCalculation.created_at.desc())
                .limit(1)
            )
            comparable = {k: v for k, v in breakdown.items() if k not in ("previous_price_ex_vat", "change_pct")}
            if latest is not None and D(latest.price_ex_vat) == price and (
                latest.offer_id == (chosen["offer"].id if chosen else None)
                and latest.rule_id == (rule.id if rule else None)
                and {k: v for k, v in (latest.breakdown or {}).items()
                     if k not in ("previous_price_ex_vat", "change_pct")} == comparable
            ):
                return dict(_row(latest, CALC_FIELDS), unchanged=True)

            calc = PriceCalculation(
                tenant_id=tenant_id, channel_id=channel.id, product_id=product.id,
                offer_id=chosen["offer"].id if chosen else None,
                rule_id=rule.id if rule else None, status=status,
                price_ex_vat=price, cost_eur=cost, margin_pct=margin, markup_pct=markup,
                breakdown=breakdown, reasons=reasons, warnings=warnings,
            )
            session.add(calc)
            session.flush()
            if status == "approved":
                self._supersede_pending(session, calc)
            return dict(_row(calc, CALC_FIELDS), unchanged=False)

    def _rrp_eur(self, session, offer: SupplierOffer, channel: Channel) -> Optional[Decimal]:
        if offer.rrp is None:
            return None
        rate = self._rate(session, channel.tenant_id, (offer.rrp_currency or offer.currency or EUR).upper())
        if rate is None:
            return None
        value = D(offer.rrp) / rate[0]
        if offer.rrp_includes_vat is not False:
            # Supplier RRPs are consumer prices; assume incl. VAT unless stated.
            value = value / (1 + D(channel.vat_rate_pct, 0) / 100)
        return value.quantize(PRICE_Q, rounding=ROUND_HALF_UP)

    def _supersede_pending(self, session, calc: PriceCalculation) -> None:
        for older in session.scalars(select(PriceCalculation).where(
            PriceCalculation.product_id == calc.product_id,
            PriceCalculation.channel_id == calc.channel_id,
            PriceCalculation.status == "pending_review",
            PriceCalculation.id != calc.id,
        )).all():
            older.status = "superseded"

    def calculate_channel(self, tenant_id: str, channel_id: str) -> Dict[str, Any]:
        """Recalculate every product that has at least one matched active offer."""
        with self.persistence.session() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or channel.tenant_id != tenant_id:
                raise PricingError("channel not found")
            product_ids = session.scalars(
                select(SupplierOffer.product_id).where(
                    SupplierOffer.tenant_id == tenant_id,
                    SupplierOffer.product_id.is_not(None),
                    SupplierOffer.active.is_(True),
                ).distinct()
            ).all()
        summary = {"products": 0, "approved": 0, "pending_review": 0, "blocked": 0, "unchanged": 0}
        for product_id in product_ids:
            result = self.calculate(tenant_id, channel_id, product_id)
            summary["products"] += 1
            if result["unchanged"]:
                summary["unchanged"] += 1
            else:
                summary[result["status"]] += 1
        return summary

    # ------------------------------------------------------------------
    # Review and publication
    # ------------------------------------------------------------------
    def _calc(self, session, tenant_id: str, calc_id: str) -> PriceCalculation:
        calc = session.get(PriceCalculation, calc_id)
        if calc is None or calc.tenant_id != tenant_id:
            raise PricingError("price calculation not found")
        return calc

    def approve(self, tenant_id: str, calc_id: str, reviewer: str) -> Dict[str, Any]:
        with self.persistence.session() as session:
            calc = self._calc(session, tenant_id, calc_id)
            if calc.status == "blocked":
                raise PricingError("blocked prices cannot be approved; change the rule or floor")
            if calc.status != "pending_review":
                raise PricingError("only prices pending review can be approved")
            calc.status = "approved"
            calc.reviewed_by = reviewer
            calc.reviewed_at = datetime.now(timezone.utc)
            session.flush()
            self._supersede_pending(session, calc)
            return _row(calc, CALC_FIELDS)

    def reject(self, tenant_id: str, calc_id: str, reviewer: str) -> Dict[str, Any]:
        with self.persistence.session() as session:
            calc = self._calc(session, tenant_id, calc_id)
            if calc.status != "pending_review":
                raise PricingError("only prices pending review can be rejected")
            calc.status = "rejected"
            calc.reviewed_by = reviewer
            calc.reviewed_at = datetime.now(timezone.utc)
            session.flush()
            return _row(calc, CALC_FIELDS)

    def record_published(self, tenant_id: str, calc_id: str) -> Dict[str, Any]:
        """Called by the channel connector after the price was pushed to the shop."""
        with self.persistence.session() as session:
            calc = self._calc(session, tenant_id, calc_id)
            if calc.status != "approved":
                raise PricingError("only approved prices can be published")
            published = PublishedPrice(
                tenant_id=tenant_id, channel_id=calc.channel_id,
                product_id=calc.product_id, calculation_id=calc.id,
                price_ex_vat=calc.price_ex_vat,
                local_prices=(calc.breakdown or {}).get("local_prices", {}),
            )
            session.add(published)
            session.flush()
            return _row(published, [
                "id", "channel_id", "product_id", "calculation_id",
                "price_ex_vat", "local_prices", "published_at",
            ])

    def lowest_published(
        self, tenant_id: str, channel_id: str, product_id: str, days: int = 30
    ) -> Optional[Dict[str, Any]]:
        """Lowest price published in the last ``days`` days (Omnibus reference price).

        Includes the price that was already live when the window started.
        """
        if days <= 0:
            raise PricingError("days must be positive")
        since = datetime.now(timezone.utc) - timedelta(days=days)
        with self.persistence.session() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or channel.tenant_id != tenant_id:
                raise PricingError("channel not found")
            base = select(PublishedPrice).where(
                PublishedPrice.tenant_id == tenant_id,
                PublishedPrice.channel_id == channel_id,
                PublishedPrice.product_id == product_id,
            )
            in_window = session.scalars(base.where(PublishedPrice.published_at >= since)).all()
            live_at_start = session.scalar(
                base.where(PublishedPrice.published_at < since)
                .order_by(PublishedPrice.published_at.desc()).limit(1)
            )
            rows = list(in_window) + ([live_at_start] if live_at_start else [])
            if not rows:
                return None
            lowest = min(rows, key=lambda r: D(r.price_ex_vat))
            return {
                "days": days,
                "price_ex_vat": _s(lowest.price_ex_vat),
                "local_prices": lowest.local_prices,
                "published_at": lowest.published_at.isoformat(),
            }
