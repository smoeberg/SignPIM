-- migrations/005_api_key_prefix.sql
--
-- Adds api_key_prefix for O(1) tenant lookup in auth middleware.
-- Prefix = first 12 characters of plaintext key (e.g. "tk_a1b2c3d4e5")
-- The prefix is NOT secret — the bcrypt hash still guards the actual key.
--
-- Idempotent: safe to run multiple times.

-- 1. Add column
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS api_key_prefix VARCHAR(12);

-- 2. Partial index: only index rows that are active and have a prefix
CREATE INDEX IF NOT EXISTS idx_tenants_api_key_prefix
  ON tenants (api_key_prefix)
  WHERE status = 'active' AND api_key_prefix IS NOT NULL;

-- 3. Check constraint: prefix must start with 'tk_'
ALTER TABLE tenants
  DROP CONSTRAINT IF EXISTS tenants_api_key_prefix_check;

ALTER TABLE tenants
  ADD CONSTRAINT tenants_api_key_prefix_check
    CHECK (api_key_prefix IS NULL OR api_key_prefix LIKE 'tk\_%' ESCAPE '\');

-- Notes:
-- * Existing tenants will have api_key_prefix = NULL until key rotation.
-- * The auth middleware handles this via a controlled fallback path.
-- * Run scripts/backfill-api-key-prefixes.js to populate prefixes without
--   requiring customers to rotate their keys immediately.
