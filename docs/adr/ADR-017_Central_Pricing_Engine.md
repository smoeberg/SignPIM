# ADR-017: Central Pricing Engine per Sales Channel

- **Status:** Accepted for implementation
- **Date:** 2026-11-18
- **Scope:** Cost price selection, price rules, minimum prices, rounding, approval and price history
- **Builds on:** ADR-016 (ProductMaster / SupplierOffer)

## Context

SignPIM manages products centrally and sells them through several PrestaShop shops. Each shop needs its own selling price, but the commercial logic (cost price, markup, minimum margin) must be controlled in one place. Until now a hidden `price × 1.25` transform in the `full_sync` workflow was the only price logic; it mixed VAT-like arithmetic into a sync step and has been removed.

## Decision

SignPIM calculates **one selling price excluding VAT, in EUR, per product and sales channel**. The shop applies VAT and converts to the customer's currency.

```
SupplierOffer cost (supplier currency)
  → FX to EUR (+ channel FX buffer for non-EUR cost)
  → landed cost (supplier surcharge % + fixed surcharge per unit)
  → price rule (markup | margin | fixed)
  → optional psychological rounding (computed incl. VAT, stored excl. VAT)
  → floor check (min. price, min. margin)
  → change check (approval threshold)
  → PriceCalculation (audit) → PublishedPrice (history)
```

### Channels

`Channel` represents one shop. All channels use EUR as base currency (v1 enforces this). A channel has:

- `vat_rate_pct` — used **only** to compute rounded prices incl. VAT. VAT itself is still applied by the shop.
- `rounding` — per currency, e.g. `{"EUR": {"step": "1", "minus": "0.05"}, "DKK": {"step": "10", "minus": "0.05"}}`. Prices are always rounded **up** (never reducing margin).
  - EUR rounding changes the EUR price excl. VAT itself (6 decimals, as PrestaShop stores).
  - Rounding in another currency produces a **fixed local price** to be published as a PrestaShop `specific_price` with `id_currency`. Without it the shop converts the EUR price and local prices will not be round.
- `fx_buffer_pct` — added to non-EUR cost after conversion to protect margin against FX movement.
- `approval_threshold_pct` — price changes above this (default 10 %) require human approval.
- `preferred_supplier_id` — optional; otherwise the cheapest in-stock offer wins.

### Cost price selection

Among active, matched offers with a cost price: a channel's preferred supplier wins if it has stock; otherwise the offer with the lowest **landed cost in EUR** among in-stock offers. If no offer is in stock, the cheapest offer is used and the calculation goes to review. Offers whose currency has no exchange rate are skipped and reported.

### Markup vs. margin

Every rule states its method explicitly. They are never interchangeable. All percentages in the API are plain percent (`30` = 30 %), never fractions:

| Method | Formula | 30 % on €100 cost |
|---|---|---|
| `markup` (avance) | cost × (1 + v/100) | €130.00 → margin 23.08 % |
| `margin` (dækningsgrad) | cost ÷ (1 − v/100) | €142.86 → margin 30.00 % |
| `fixed` | v (EUR excl. VAT) | — |

Minimum margins (`PriceFloor.min_margin_pct`) are always expressed as **margin (dækningsgrad)**, because that is what tells whether a sale is profitable. Every calculation stores both `margin_pct` and `markup_pct`.

### Rule precedence

The most specific rule wins:

1. fixed price, product + channel
2. fixed price, product
3. product + channel
4. product
5. category + channel
6. category
7. channel default
8. tenant default

Ties within a level: higher `priority`, then newest. `fixed` is only allowed on product scope.

### Floors are a check, not a rule

`PriceFloor` (min. price excl. VAT and/or min. margin) is evaluated **after** the rule and rounding. All applicable floors (tenant, channel, category, product) are combined and the strictest value wins. A price below the floor is **blocked** — never silently raised — and cannot be approved. The rule or floor must be changed.

### Approval

- First price for a product/channel: approved automatically if it passes the floor.
- Change larger than `approval_threshold_pct` versus the last approved price: `pending_review`.
- No in-stock offer: `pending_review`.
- Floor violation, missing cost, missing rule or missing FX rate: `blocked`.
- An identical recalculation returns the existing calculation instead of creating a new one.
- A new approved calculation supersedes older pending ones.

### Recommended retail price (RRP)

Supplier RRP is stored on the offer with its currency and whether it includes VAT. Calculations convert it to EUR excl. VAT for comparison and warn when SignPIM's price exceeds it. RRP never drives the price in v1.

### History and the Omnibus rule

`PriceCalculation` is the audit trail of every calculation (inputs, FX rate, rule, result, status, reviewer). `PublishedPrice` records what was actually pushed to a shop and when. Announcing a price reduction requires referring to the lowest price in the preceding 30 days (EU Omnibus directive); `PublishedPrice` makes that answerable per channel.

## Not in v1

- Campaigns / time-limited prices. They will be modelled as separate campaign rules and published as PrestaShop `specific_price` with from/to dates, so the base price stays clean. Campaign prices pass the same floor check.
- The PrestaShop push itself (channel connector). The connector reads approved calculations and reports back via the "published" endpoint.
- Automatic FX feed (ECB). Rates are maintained per tenant via the API (`PUT /catalog/exchange-rates`); a scheduled ECB import is a small follow-up. The latest rate on or before the calculation date is used, and the rate and its date are stored in every calculation.
- Non-EUR base currency.

## Consequences

- Price logic lives only in the pricing engine; workflows in `meta/` must not transform prices.
- ProductMaster gains `category`; SupplierOffer gains RRP fields; Supplier gains landed-cost surcharges.
- Pricing rules, floors, channels, exchange rates and approvals require the new admin scopes `pricing:write` and `pricing:approve`. Writers may trigger calculations; they cannot change rules or approve.
- Exchange rates are tenant-scoped so one tenant cannot influence another tenant's prices.

## API

- `POST /catalog/channels`
- `POST|GET /catalog/price-rules`, `DELETE /catalog/price-rules/{id}` (deactivates)
- `POST /catalog/price-floors`
- `PUT /catalog/exchange-rates`
- `POST /catalog/channels/{channel_id}/prices/calculate` — one product, or all matched products when `product_id` is omitted
- `POST /catalog/price-calculations/{id}/approve|reject`
- `POST /catalog/price-calculations/{id}/published` — called by the channel connector
- `GET /catalog/channels/{channel_id}/prices/{product_id}/lowest?days=30`
