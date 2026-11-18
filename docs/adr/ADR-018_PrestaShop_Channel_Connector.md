# ADR-018: PrestaShop Channel Connector

- **Status:** Accepted for implementation
- **Date:** 2026-11-18
- **Scope:** Two-way integration between SignPIM and separate PrestaShop installations
- **Builds on:** ADR-016 (ProductMaster / SupplierOffer), ADR-017 (pricing engine)

## Context

SignPIM manages products centrally and sells them through several PrestaShop shops, each a **separate installation** running the **latest PrestaShop** (9.x). The connector is a core part of SignPIM, but the domain model stays platform-neutral: PrestaShop is the first channel implementation, not a dependency of the core.

Decisions taken with the product owner:

1. Separate installations, always the newest PrestaShop.
2. SignPIM owns **product data, images, price and stock**.
3. Orders are used **only to keep stock correct**. Forwarding orders to suppliers (dropship fulfilment) is out of scope.
4. **Realtime** via a small PrestaShop module.

## Which PrestaShop API

PrestaShop 9 has two APIs:

- **Webservice** (`/api/`, XML writes, JSON reads, API key). Covers every resource needed: products, images, specific_prices, stock_availables, orders.
- **Admin API** (OAuth2/JSON, module `ps_apiresources`). Modern, but still pre-1.0 (v0.9 in Sept 2026) and coverage is incomplete.

**Decision:** use the Webservice now behind a `ChannelClient` interface; add an Admin API adapter when it reaches 1.0 and covers products, images, specific prices and stock. `Channel.api_mode` selects the adapter.

## Ownership

| Data | Owner | Behaviour |
|---|---|---|
| Name, description (per language), reference, EAN/UPC, MPN, default category, tax group, manufacturer, active | SignPIM | Sent on every change; shop edits are overwritten |
| Price excl. VAT (EUR) and cost (`wholesale_price`) | SignPIM | From the latest **approved** calculation (ADR-017) |
| Rounded local prices (e.g. DKK) | SignPIM | `specific_price` with `id_currency` |
| Stock | SignPIM | Supplier stock minus unreflected sales (below) |
| Images, order and cover | SignPIM | Position 0 is the cover |
| SEO URL (`link_rewrite`), extra category placements | Shop | Set once when SignPIM creates the product, never overwritten |
| Everything else (SEO meta, carriers, related products …) | Shop | Never sent |

Products are **deactivated, never deleted**, when they lose their approved price or their last active offer, so order history and URLs survive.

## Architecture

```
change (product, media, offer, match, approved price, order, shop drift)
   -> SyncJob outbox (one pending job per product+channel, coalesced)
   -> worker: recalc price -> product -> stock -> local prices -> images
   -> ExternalRef (local id <-> shop id + hash of last pushed content)
```

- **ExternalRef** maps product, stock row, specific price, image and image set to shop ids, with a content hash. Unchanged parts are not resent.
- **SyncJob** retries with exponential backoff (2, 4, 8 … max 60 min). 4xx errors other than 408/429 are not retryable and the job is marked `dead` immediately; after 8 attempts any job is `dead`. `GET /catalog/channels/{id}/sync-jobs?status=dead` lists what needs attention.
- The worker (`scripts/channel_sync_worker.py`, `sync-worker` service in `docker-compose.prod.yml`) polls every 5 s. Events and manual syncs are also processed immediately in the API process. On PostgreSQL jobs are claimed with `FOR UPDATE SKIP LOCKED`.
- **Price recalculation happens inside the job**, so a supplier cost change flows to the shop automatically. If the new price needs approval, the shop keeps the last approved price.

## Stock across shops

Both shops sell from the same supplier stock. An order in shop A must lower stock in shop B immediately.

`available = Σ active supplier offer quantity − Σ reservations created after the latest supplier feed observation`

An order creates a `StockReservation`; a cancelled order removes it. When the supplier feed is next observed, it reflects the sale and the reservation no longer counts. Offers with only a stock status ("in stock") count as `connector_config.unknown_stock_quantity` (default 10).

## Images

SignPIM stores originals (`ProductMedia`, SHA-256 deduplicated, under `SIGNPIM_MEDIA_ROOT`). PrestaShop sets cover and order from upload order, so:

- removals and images appended at the end are applied incrementally;
- any reorder or cover change re-uploads the set.

## Realtime module

`integrations/prestashop/signpimconnector` (PHP, PrestaShop 8/9):

- `order.created` / `order.updated` (cancelled) from order hooks.
- `product.updated`, `stock.updated`, `product.deleted` **only from the back office**; changes made through the Webservice (by SignPIM) are never sent back, which prevents sync loops.
- Events are signed with HMAC-SHA256 over `"<timestamp>.<body>"`; SignPIM rejects events older than 5 minutes and ignores duplicate `event_id`s.
- Undelivered events are kept in a local outbox table and retried on the next event or by a token-protected cron URL. Delivery uses short timeouts so checkout is never noticeably delayed.

## Onboarding existing shops

`POST /catalog/channels/{id}/import-catalog` pages through the shop's products and links them to master products by `reference = master_sku`, then by GTIN (EAN-13/UPC, normalised to GTIN-14). Linked products are queued and receive SignPIM's data on the next sync instead of being created twice. Unmatched products and conflicts (two shop products for one master) are reported for review.

## Configuration

`PUT /catalog/channels/{id}/connection`

```json
{
  "base_url": "https://shop-a.dk",
  "credential_ref": "env:SIGNPIM_PS_SHOP_A_KEY",
  "webhook_secret_ref": "env:SIGNPIM_PS_SHOP_A_HOOK",
  "connector_config": {
    "languages": {"da": 1, "en": 2},
    "id_tax_rules_group": 1,
    "category_map": {"tools": 12},
    "default_category_id": 2,
    "currency_ids": {"DKK": 2},
    "manufacturer_map": {"Bosch": 3},
    "unknown_stock_quantity": 10
  }
}
```

Secrets are never stored: `*_ref` names an environment variable (`core/secrets.py`). Secret-like keys in `connector_config` are rejected. `base_url` must be https. Translations come from `ProductMaster.attributes.i18n.<iso>.name|description`, falling back to the master text.

## Verify against a live PrestaShop 9 before go-live

The connector is tested against an in-memory Webservice that mirrors the documented behaviour. These points depend on PrestaShop internals and must be checked on a staging shop:

1. A `specific_price` with `id_currency` set is interpreted as a fixed price **in that currency** (not converted from the default currency).
2. `PATCH` on `products` with multilanguage fields leaves untouched languages and fields intact.
3. The module's back-office detection (`_PS_ADMIN_DIR_` + employee, excluding `/webservice/` and `/api/`) suppresses events from SignPIM's own Webservice writes.
4. Webservice key permissions as listed in the module README.

## Not in this version

- Combinations (variants). The data model has room for them (`ExternalRef` local ids), but ProductMaster has no variant concept yet.
- Order forwarding to suppliers (dropship fulfilment).
- Admin API adapter (see above).
- Campaign prices (ADR-017 "Not in v1").
