const bcrypt = require('bcrypt');
const { query } = require('../db/pool');

/**
 * API key authentication middleware.
 *
 * Fast path:  prefix lookup → single bcrypt compare.
 * Slow path:  full table scan for legacy keys that predate migration 005.
 *             The slow path does NOT update the prefix here — that is done
 *             by scripts/backfill-api-key-prefixes.js to avoid adding a
 *             write to every auth request for legacy tenants.
 */
module.exports = async (req, res, next) => {
  const apiKey = req.headers['x-api-key'];

  if (!apiKey) {
    return res.status(401).json({ error: 'API key required' });
  }

  if (!apiKey.startsWith('tk_') || apiKey.length < 20) {
    return res.status(401).json({ error: 'Invalid API key format' });
  }

  try {
    const prefix = apiKey.slice(0, 12);

    // Fast path: prefix is indexed — at most one row returned
    const prefixResult = await query(
      `SELECT id, name, status, plan, api_key_hash
       FROM tenants
       WHERE api_key_prefix = $1
         AND status = 'active'
         AND api_key_hash IS NOT NULL`,
      [prefix]
    );

    if (prefixResult.rows.length === 1) {
      const tenant = prefixResult.rows[0];
      const matches = await bcrypt.compare(apiKey, tenant.api_key_hash);
      if (!matches) {
        return res.status(401).json({ error: 'Invalid API key' });
      }
      req.tenantId = tenant.id;
      req.tenant = tenant;
      return next();
    }

    // Slow path: no prefix match — could be a legacy key (prefix IS NULL)
    // Once all tenants have been backfilled this branch can be removed.
    const allResult = await query(
      `SELECT id, name, status, plan, api_key_hash
       FROM tenants
       WHERE status = 'active'
         AND api_key_hash IS NOT NULL
         AND api_key_prefix IS NULL`,
      []
    );

    let matchedTenant = null;
    for (const tenant of allResult.rows) {
      if (await bcrypt.compare(apiKey, tenant.api_key_hash)) {
        matchedTenant = tenant;
        break;
      }
    }

    if (!matchedTenant) {
      return res.status(401).json({ error: 'Invalid API key' });
    }

    req.tenantId = matchedTenant.id;
    req.tenant = matchedTenant;
    return next();
  } catch (error) {
    console.error('Auth middleware error:', error);
    return res.status(500).json({ error: 'Authentication failed' });
  }
};

/**
 * Generate a new API key.
 * Returns plaintext (send to customer once, never store),
 * bcrypt hash and prefix (both stored in tenants table).
 */
async function generateApiKey() {
  const { randomBytes } = require('crypto');
  const secret = randomBytes(32).toString('hex');
  const plaintext = `tk_${secret}`;
  const prefix = plaintext.slice(0, 12);
  const hash = await bcrypt.hash(plaintext, 12);
  return { plaintext, hash, prefix };
}

module.exports.generateApiKey = generateApiKey;
