# Signalement v6.1 - Model-Driven PIM Platform

Signalement (internally branded "SignPIM") is a multi-tenant SaaS Product Information Management (PIM) system. Version 6.1 marks a major architectural shift from a traditional Node.js REST API to a **Metadata-Driven Workflow Engine**.

## Architecture Overview

The system is built on a "Software Factory" model where business logic, entity schemas, and integration workflows are defined as declarative YAML files.

### 1. Meta Layer (`/meta`)
The "Source of Truth" for the entire platform.
- **Entities**: Data schemas for products, tenants, etc.
- **Rules**: Declarative business rules (EAN validation, quality scores, etc.).
- **Workflows**: Step-by-step processing pipelines (Import, Sync, Publish).

### 2. Engine Layer (`/engine`)
A generic, domain-agnostic runtime that interprets the Meta Layer.
- **Runtime**: Loads metadata and executes workflows.
- **Rule Evaluator**: Safe, non-eval based logic dispatcher for business rules.

### 3. Core Layer (`/core`)
Essential infrastructure services.
- **Database**: Parameterized, SQL-injection-safe persistence with tenant isolation.

### 4. Legacy Layer (`/legacy`)
The original Node.js v3.2 codebase, preserved for reference and phased migration.

## Getting Started

### Prerequisites
- Python 3.8+
- PostgreSQL

### Installation
```bash
pip install -r requirements.txt
```

### Running the Engine
The main entry point demonstrates a full sync workflow:
```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/pim_db"
python3 main.py
```

## Security & Compliance
- **Tenant Isolation**: Strictly enforced at the Engine and Database layers.
- **SQL Injection Protection**: All queries use parameterized values.
- **No Dangerous Execution**: YAML rules are parsed through a secure dispatcher, avoiding `eval()`.

## Development
- Add new rules in `meta/rules/`
- Add new workflows in `meta/workflow/`
- Define new entities in `meta/entities/`

---

## Production (v0.2)

```bash
export POSTGRES_PASSWORD=<secret>
docker compose -f docker-compose.prod.yml up -d --build
# optional TLS edge:
docker compose -f docker-compose.prod.yml --profile edge up -d
```

- API: `http://localhost:8000` — interactive docs at `/docs` (Swagger UI).
- Health probe: `GET /health`.
- Tenant-scoped: all endpoints accept `?tenant=<slug>`.
- Quality scoring: 3D model (completeness/consistency/accuracy), per-product breakdown.

### Ingest a supplier feed
```bash
curl -X POST "http://localhost:8000/tenants/acme/ingest?body=sku,name,price%0AABC-1,Widget,199.5"
```

### Regenerate the OpenAPI contract
```bash
python scripts/export_openapi.py docs/openapi.json
```


## Canonical Product Master (new domain foundation)

The canonical catalogue is being established independently of the original tenant/SKU feed record. New catalogue integrations use these domain objects:

- **ProductMaster** is the canonical product identity and owns master SKU, GTIN, brand, manufacturer, MPN, canonical content, attributes and translations.
- **Supplier** represents one supplier connection and its adapter identity.
- **SupplierOffer** represents one supplier's SKU, price, currency, stock and source content. A single ProductMaster may have many offers from different suppliers.
- **SupplierOfferSnapshot** records each observed supplier payload and commercial state.
- **ProductMatch** records exact matches and human-review proposals.

Supplier payloads are stored in raw_data; normalized values are stored separately in normalized_data. GTIN exact matches may be linked automatically. Brand/MPN matches remain proposals and never merge products automatically.

The initial API is available under /catalog:

- POST /catalog/suppliers
- POST /catalog/products
- POST /catalog/suppliers/{supplier_id}/offers
- POST /catalog/offers/{offer_id}/match-gtin
- POST /catalog/suppliers/{supplier_id}/import

### Supplier Hub adapter contract

Each Supplier has an `adapter_key` and non-secret `adapter_config`. The application resolves that key through a central `SupplierAdapterRegistry`. Vendor-specific adapters implement the same `parse(payload, config) -> SupplierRecord[]` contract and can be registered at application startup.

Built-in reference adapters:
- `csv`: delimiter/encoding detection and per-supplier `field_map`.
- `json`: JSON arrays with optional dotted `records_path` and per-supplier `field_map`.

Both adapters produce the same offer contract. The Hub stores the original source row unchanged in `raw_data`, stores mapped identity fields in `normalized_data`, and writes offers/snapshots. It does not create canonical products; GTIN matching is performed only against an existing ProductMaster.

Example supplier configuration:
```json
{
  "adapter_key": "csv",
  "adapter_config": {
    "delimiter": ";",
    "field_map": {
      "supplier_sku": "Varenummer",
      "cost_price": "Indkoebspris",
      "gtin": "EAN",
      "title": "Varetekst"
    }
  }
}
```

SFTP connections now reject unknown SSH host keys. Configure a trusted known_hosts file through `sftp.known_hosts_path` or `SIGNPIM_SFTP_KNOWN_HOSTS`.

See [ADR-016](docs/adr/ADR-016_Product_Master_Supplier_Offer.md) for the domain decision. The previous /products and generic feed ingestion paths are legacy surfaces; new supplier adapters and sales-channel integrations must be built on the canonical model.


## Pricing engine (ADR-017)

SignPIM calculates **one price excl. VAT in EUR per product and channel** (PrestaShop shop). The shop applies VAT and converts to the customer's currency.

```
supplier cost -> EUR (+FX buffer) -> landed cost -> rule (markup | margin | fixed)
  -> optional rounding -> floor check -> approval check -> PriceCalculation
```

- Rules per tenant, channel, category or product; the most specific wins. Fixed prices are product-only.
- Percentages are plain percent: `{"method": "markup", "value": "30"}` = 30 % avance (€100 -> €130, dækningsgrad 23.08 %); `"margin"` = dækningsgrad.
- Floors (`min_price`, `min_margin_pct`) block a price; they never raise it silently.
- Changes above the channel's `approval_threshold_pct` (default 10 %) wait for approval.
- Psychological prices are computed incl. VAT: `rounding: {"DKK": {"step": "10", "minus": "0.05"}}` gives x9.95 DKK as a fixed local price for PrestaShop `specific_price`.
- `PublishedPrice` keeps what was live per channel; `/lowest?days=30` answers the Omnibus "før"-pris question.

See `docs/adr/ADR-017_Central_Pricing_Engine.md` for the full design.

## PrestaShop channel connector (ADR-018)

SignPIM owns product data, images, price and stock and keeps every connected PrestaShop shop up to date in near realtime.

- `PUT /catalog/channels/{id}/connection` connects a shop (Webservice key referenced as `env:NAME`, never stored).
- `POST /catalog/channels/{id}/import-catalog` links products already in the shop (reference, then GTIN).
- Changes are queued per product and shop and pushed by `scripts/channel_sync_worker.py` (`sync-worker` service); only changed parts are sent.
- `POST /catalog/products/{id}/media` uploads images; their order (first = cover) is mirrored in every shop.
- The PrestaShop module in `integrations/prestashop/signpimconnector` sends signed order events, so a sale in one shop lowers stock in all shops, and restores SignPIM's data if someone edits it in the back office.

See `docs/adr/ADR-018_PrestaShop_Channel_Connector.md`, including the checklist to verify on a staging shop before go-live.
