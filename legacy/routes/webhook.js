const express = require('express');
const crypto = require('crypto');
const { v4: uuidv4 } = require('uuid');
const { query, withTenantClient } = require('../db/pool');

const router = express.Router();

function stableStringify(value) {
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(',')}]`;
  }
  if (value && typeof value === 'object') {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

function computeFingerprint(tenantId, sourceSystem, payload) {
  const canonical = stableStringify({ tenantId, sourceSystem, payload });
  return crypto.createHash('sha256').update(canonical).digest('hex');
}

/**
 * POST /api/webhook/:system
 * Ingest a product event from an external system.
 * Returns 200 for duplicates (idempotent), 202 for new events.
 */
router.post('/:system', async (req, res) => {
  const { tenantId } = req;
  const { system } = req.params;
  const payload = req.body;

  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return res.status(400).json({ error: 'Payload must be a JSON object' });
  }

  const eventHash = computeFingerprint(tenantId, system, payload);
  const externalEventId = req.headers['x-event-id'] || payload.event_id || null;

  try {
    const response = await withTenantClient(tenantId, async (client) => {
      const result = await client.query(
        `INSERT INTO raw_events
           (id, tenant_id, source_system, external_event_id, event_hash, payload, processed, retry_count)
         VALUES ($1, $2, $3, $4, $5, $6, FALSE, 0)
         ON CONFLICT (event_hash) DO NOTHING
         RETURNING id`,
        [uuidv4(), tenantId, system, externalEventId, eventHash, JSON.stringify(payload)]
      );

      if (result.rows.length === 0) {
        return { statusCode: 200, body: { status: 'duplicate', message: 'Event already received' } };
      }

      const eventId = result.rows[0].id;

      await client.query(
        `INSERT INTO connector_status (tenant_id, system_name, status, last_successful_event, events_last_24h)
         VALUES ($1, $2, 'ok', NOW(), 1)
         ON CONFLICT (tenant_id, system_name) DO UPDATE SET
           status = 'ok',
           last_successful_event = NOW(),
           events_last_24h = connector_status.events_last_24h + 1,
           last_error = NULL,
           updated_at = NOW()`,
        [tenantId, system]
      );

      await client.query('SELECT pg_notify($1, $2)', ['webhook_event', String(eventId)]);

      return {
        statusCode: 202,
        body: { status: 'accepted', event_id: eventId, message: 'Event queued for processing' },
      };
    });

    return res.status(response.statusCode).json(response.body);
  } catch (err) {
    console.error(`Webhook error [tenant=${tenantId} system=${system}]:`, err);
    try {
      await query(
        `INSERT INTO connector_status (tenant_id, system_name, status, last_error)
         VALUES ($1, $2, 'error', $3)
         ON CONFLICT (tenant_id, system_name) DO UPDATE SET
           status = 'error', last_error = $3, updated_at = NOW()`,
        [tenantId, system, err.message]
      );
    } catch (logErr) {
      console.error('Failed to update connector status:', logErr);
    }
    return res.status(500).json({ error: 'Failed to process webhook' });
  }
});

/**
 * GET /api/webhook/status
 * Returns connector health for all systems belonging to this tenant.
 * Mounted after auth middleware in app.js — tenantId is always set.
 */
router.get('/status', async (req, res) => {
  const { tenantId } = req;
  try {
    const result = await query(
      `SELECT system_name, status, last_successful_event, last_error,
              events_last_24h, error_rate_percent, updated_at
       FROM connector_status
       WHERE tenant_id = $1
       ORDER BY system_name`,
      [tenantId]
    );
    return res.json({ connectors: result.rows });
  } catch (err) {
    console.error('GET /webhook/status error:', err);
    return res.status(500).json({ error: 'Failed to fetch connector status' });
  }
});

/**
 * POST /api/internal/webhook/recover-stale
 * Re-queues unprocessed events older than 5 minutes, up to 3 retries.
 *
 * Security: internal-only. Protected by INTERNAL_API_TOKEN header.
 * Explicit tenant_id filter ensures RLS is not the only line of defence
 * when this runs without a tenant context.
 *
 * NOTE: This route is registered under /api/internal in app.js,
 * not under /api/webhook, so it is never reachable via the public path.
 */
router.post('/recover-stale', async (req, res) => {
  if (req.headers['x-internal-token'] !== process.env.INTERNAL_API_TOKEN) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  try {
    // Explicit tenant_id IS NOT NULL guard so we never accidentally touch
    // rows that somehow lack a tenant (belt-and-suspenders alongside RLS).
    const result = await query(
      `UPDATE raw_events
       SET processed   = FALSE,
           retry_count = retry_count + 1,
           updated_at  = NOW()
       WHERE processed   = FALSE
         AND tenant_id   IS NOT NULL
         AND created_at  < NOW() - INTERVAL '5 minutes'
         AND retry_count < 3
       RETURNING id`
    );

    for (const event of result.rows) {
      await query('SELECT pg_notify($1, $2)', ['webhook_event', String(event.id)]);
    }

    return res.json({ message: 'Stale events recovered', count: result.rows.length });
  } catch (err) {
    console.error('Recover stale events error:', err);
    return res.status(500).json({ error: 'Failed to recover stale events' });
  }
});

module.exports = router;
