# ADR-012: HashiCorp Vault for Secrets Management (Phase 2)

**Status:** Proposed
**Date:** 2026-07-25

## Context
In Sprint 0-9, environment variables and `.env` files are used for database credentials and API tokens. While sufficient for initial setup, enterprise deployment requires encrypted secret management, rotation, and audit logs.

## Decision
Implement HashiCorp Vault in Phase 2 for all credential and secret storage.

## Consequences
- Dynamic secret rotation for DB and external integrations
- Full access auditing
- Elimination of plain-text secrets in deployment environments
