# ADR-013: HTTPS & TLS Termination

**Status:** Proposed
**Date:** 2026-07-25

## Context
Production environments require encrypted HTTPS communication via TLS termination at Nginx ingress.

## Decision
Configure Nginx reverse proxy with SSL certificate volumes in Docker composition.
