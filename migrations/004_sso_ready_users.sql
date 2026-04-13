-- migrations/004_sso_ready_users.sql
--
-- Fixes the users table from migration 001 to match the V3.2 spec:
-- - password_hash is now nullable (NULL when user authenticates via SSO)
-- - Adds external_idp and external_subject_id for Entra ID / Google Workspace
-- - Adds display_name, is_active, last_login_at
-- - Adds unique constraint on (external_idp, external_subject_id)
--   so the same SSO identity maps back to the same global user record
--
-- Migration is idempotent: safe to run multiple times.

-- 1. Make password_hash nullable (was NOT NULL in migration 001)
ALTER TABLE users
  ALTER COLUMN password_hash DROP NOT NULL;

-- 2. Add SSO columns if they don't exist yet
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'users' AND column_name = 'external_idp'
  ) THEN
    ALTER TABLE users
      ADD COLUMN external_idp         VARCHAR(50),   -- e.g. 'entra_id', 'google_workspace'
      ADD COLUMN external_subject_id  VARCHAR(255),  -- User's immutable ID at the IdP
      ADD COLUMN display_name         VARCHAR(255),
      ADD COLUMN is_active            BOOLEAN DEFAULT TRUE,
      ADD COLUMN last_login_at        TIMESTAMP;
  END IF;
END
$$;

-- 3. Unique constraint: one external identity per provider globally
--    Users can still belong to multiple tenants via tenant_users.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'users_external_identity_unique'
  ) THEN
    ALTER TABLE users
      ADD CONSTRAINT users_external_identity_unique
        UNIQUE (external_idp, external_subject_id);
  END IF;
END
$$;

-- 4. Check constraint: user must have either a password OR an SSO identity
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_auth_method_check;
ALTER TABLE users
  ADD CONSTRAINT users_auth_method_check CHECK (
    password_hash IS NOT NULL OR external_subject_id IS NOT NULL
  );

-- Result:
-- v1 users:  password_hash = 'bcrypt...', external_idp = NULL, external_subject_id = NULL
-- v2 users:  password_hash = NULL,        external_idp = 'entra_id', external_subject_id = 'abc123'
-- Migration path v1→v2: set external_idp + external_subject_id on first SSO login, then null out password_hash
