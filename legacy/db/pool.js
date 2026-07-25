const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: 20,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 2000,
});

pool.on('error', (err) => {
  console.error('Unexpected PostgreSQL pool error:', err);
});

/**
 * Set tenant context safely for PostgreSQL RLS.
 * set_config(..., true) = SET LOCAL — context is cleared automatically
 * when the transaction ends and cannot leak to another request.
 */
async function setTenantLocal(client, tenantId) {
  await client.query(
    `SELECT set_config('app.current_tenant_id', $1, true)`,
    [String(tenantId)]
  );
}

/**
 * Execute a single query with tenant RLS context.
 * Wraps in its own transaction so the context is always scoped correctly.
 */
async function queryWithTenant(tenantId, sql, params = []) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await setTenantLocal(client, tenantId);
    const result = await client.query(sql, params);
    await client.query('COMMIT');
    return result;
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}

/**
 * Run multiple queries under the same tenant context and transaction.
 * The callback receives a client that already has tenant context set.
 */
async function withTenantClient(tenantId, callback) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await setTenantLocal(client, tenantId);
    const result = await callback(client);
    await client.query('COMMIT');
    return result;
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}

/**
 * Query without tenant context (auth lookups, health checks, migrations).
 */
async function query(sql, params = []) {
  return pool.query(sql, params);
}

module.exports = { pool, query, queryWithTenant, withTenantClient };
