# ADR-016: Canonical Product Master and Supplier Offers

- **Status:** Accepted for implementation
- **Date:** 2026-11-18
- **Scope:** Product identity, supplier catalogue data and matching

## Context

A supplier SKU identifies a supplier's offer, not a canonical product. The same physical product may be sold by several suppliers, each with its own SKU, price, availability, description and source feed. Storing those fields in one tenant/SKU product record causes supplier updates to overwrite one another and mixes product identity with commercial data.

SignPIM is not in production. The current API and persistence shape do not constrain this redesign; breaking changes are acceptable while the new domain model is established.

## Decision

The canonical catalogue has four distinct concepts:

1. **ProductMaster** — one canonical product identity owned by a tenant. It holds the SignPIM master SKU, GTIN, brand, manufacturer, MPN, canonical name/description, structured attributes, translations and lifecycle status.
2. **Supplier** — one configured supplier integration, identified by a tenant-scoped code and an adapter key. Secret material is referenced by credential_ref; it does not belong in ordinary adapter configuration.
3. **SupplierOffer** — one supplier's commercial offer for a product. Its supplier SKU is unique only within that supplier. Price, currency, stock, supplier URLs and source text belong here, not on ProductMaster. An offer may remain unmatched until identity is known.
4. **SupplierOfferSnapshot / ProductMatch** — append-only observations of supplier data and an auditable record of exact or proposed identity matches.

One ProductMaster may have many SupplierOffers, including multiple offers from different suppliers. One SupplierOffer belongs to exactly one Supplier and may link to at most one ProductMaster at a time.

## Source-data invariant

SupplierOffer.raw_data is the supplier payload, copied as received and never normalized in place. Transformations are stored separately in normalized_data. Every offer upsert creates a SupplierOfferSnapshot containing the raw payload and the commercial values observed at that time. This preserves both the current source record and its history.

## Matching policy

- A valid, exact GTIN read from the supplier offer's own raw or normalized source data may link an offer automatically; callers cannot supply a different GTIN to force a match.
- Brand + MPN values are read from the supplier offer source. Matches are proposals only; they never merge or link products until a reviewer explicitly confirms them.
- Ambiguous or missing identity remains unmatched until reviewed.
- Supplier SKU is never treated as a global product identifier.

## API surface introduced

- POST /catalog/suppliers
- POST /catalog/products
- POST /catalog/suppliers/{supplier_id}/offers
- POST /catalog/offers/{offer_id}/match-gtin

These endpoints are an initial domain API. Supplier-specific feed adapters and channel publishing are separate layers and are not implemented by this ADR.

## Consequences

- New catalogue work must use ProductMaster and SupplierOffer, not the legacy tenant/SKU JSON record.
- Existing legacy product endpoints and feed ingestion are not a compatibility constraint and will be replaced as the supplier adapter and channel layers are implemented.
- The current persistence bootstrap uses SQLAlchemy metadata creation. A production-grade migration system must be selected before deployment; schema creation is not a substitute for managed migrations.
