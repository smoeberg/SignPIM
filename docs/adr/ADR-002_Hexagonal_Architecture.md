# ADR-002: Hexagonal Architecture (Ports & Adapters)

**Status:** Accepted
**Dato:** 2026-07-23

## Context
Signalement PIM skal understøtte mange forskellige integrationsscenarier uden at domænet bliver afhængigt af dem.

## Decision
We adopt Hexagonal Architecture (Ports & Adapters).
- Domain is in the center
- Ports define interfaces
- Adapters implement ports for external systems

## Consequences
- Domain has zero dependencies on infrastructure
- Easy to test domain in isolation
