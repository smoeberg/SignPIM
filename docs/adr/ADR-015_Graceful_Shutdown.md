# ADR-015: Graceful Shutdown Handling

**Status:** Accepted
**Date:** 2026-07-25

## Decision
Handle `SIGTERM` signals cleanly by closing HTTP servers, draining DB pools, and allowing active requests to finish within a 10s timeout window.
