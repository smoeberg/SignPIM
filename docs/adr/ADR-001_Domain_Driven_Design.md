# ADR-001: Domain-Driven Design

**Status:** Accepted
**Dato:** 2026-07-23

## Context
Signalement PIM skal have en levetid på 10-15 år og understøtte mange forskellige integrationsscenarier.

## Decision
We adopt Domain-Driven Design (DDD) as the primary design methodology.
- Rich domain models with business logic encapsulated
- Aggregates as the primary unit of consistency
- Repository pattern for persistence

## Consequences
- Business logic lives in the domain, not in services
- Domain has no external dependencies
