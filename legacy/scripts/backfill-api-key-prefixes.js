/**
 * scripts/backfill-api-key-prefixes.js
 *
 * One-time script to populate api_key_prefix for tenants that pre-date
 * migration 005. Eliminates the O(n) fallback path in auth middleware
 * without forcing customers to rotate keys.
 *
 * HOW IT WORKS
 * The prefix is the first 12 characters of the plaintext key (e.g. "tk_a1b2c3d4").
 * We cannot derive it from the stored bcrypt hash — we need to ask each tenant
 * to authenticate once (triggering auto-population), OR generate new keys and
 * send them.
 *
 * This script uses the "generate new key" approach:
 *   1. Generate a new plaintext + hash + prefix.
 *   2. Update the tenant row.
 *   3. Print the new plaintext key so it can be sent to the tenant.
 *
 * Run: node scripts/backfill-api-key-prefixes.js
 * Review output carefully before sending keys to customers.
 */

const { query } = require('../db/pool');
const { generateApiKey } = require('../middleware/auth');

async function backfill() {
  const result = await query(
    "SELECT id, name FROM tenants WHERE api_key_prefix IS NULL AND status = 'active'"
  );

  if (result.rows.length === 0) {
    console.log('All tenants already have a prefix — nothing to do.');
    return;
  }

  console.log(`Rotating keys for ${result.rows.length} tenant(s):\n`);

  for (const tenant of result.rows) {
    const { plaintext, hash, prefix } = await generateApiKey();
    await query(
      'UPDATE tenants SET api_key_hash = $1, api_key_prefix = $2 WHERE id = $3',
      [hash, prefix, tenant.id]
    );
    // In production, replace this with your email/notification service
    console.log(`Tenant ${tenant.id} (${tenant.name})\n  NEW KEY: ${plaintext}\n`);
  }

  console.log('Done. Send new keys to affected tenants via secure channel.');
}

backfill().catch((err) => {
  console.error('Backfill failed:', err);
  process.exit(1);
});
