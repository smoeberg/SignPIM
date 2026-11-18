# SignPIM Connector for PrestaShop

Realtime link from a PrestaShop 8/9 shop to SignPIM (ADR-018).

## What it sends

| Hook | Event | SignPIM does |
|---|---|---|
| `actionValidateOrder` | `order.created` | Reserves the sold quantity and lowers stock in **every** connected shop |
| `actionOrderStatusPostUpdate` (cancelled/refund/error) | `order.updated` with `cancelled: true` | Releases the reservation |
| `actionProductUpdate` (back office only) | `product.updated` | Restores the fields SignPIM owns |
| `actionUpdateQuantity` (back office only) | `stock.updated` | Restores the stock SignPIM calculates |
| `actionProductDelete` (back office only) | `product.deleted` | Recreates the product on next sync |

Changes made through the Webservice — i.e. by SignPIM — are never sent back, so the two systems cannot loop.

## Install

1. Zip the `signpimconnector` folder and upload it under *Modules → Module Manager → Upload a module*.
2. In SignPIM, connect the channel with a `webhook_secret_ref`, e.g. `env:SIGNPIM_PS_SHOP_A_HOOK`, and set that variable on the SignPIM server.
3. In the module configuration enter
   - **Events URL**: `https://<signpim>/channels/<channel_id>/events`
   - **Shared secret**: the same value as the variable above.
4. Add the cron URL shown on the configuration page to a crontab (every minute). It retries events that could not be delivered, e.g. while SignPIM was restarting.

## Webservice key for SignPIM

Create a key under *Advanced Parameters → Webservice* with GET/POST/PUT/PATCH/DELETE on:
`products`, `images`, `specific_prices`, `stock_availables`, and GET on `orders`, `currencies`, `languages`, `categories`, `tax_rule_groups`.
Store it on the SignPIM server as e.g. `SIGNPIM_PS_SHOP_A_KEY` and reference it as `env:SIGNPIM_PS_SHOP_A_KEY`.

## Security

Every event is signed with HMAC-SHA256 over `"<timestamp>.<body>"` (headers `X-SignPIM-Timestamp`, `X-SignPIM-Signature`). SignPIM rejects events older than 5 minutes and ignores duplicate `event_id`s.
