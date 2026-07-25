const express = require('express');
const { queryWithTenant, withTenantClient } = require('../db/pool');

const router = express.Router();

const VALID_STATUSES = ['open', 'assigned', 'in_progress', 'resolved', 'closed', 'escalated', 'blocked'];
const VALID_PRIORITIES = ['low', 'medium', 'high'];

function parsePositiveInt(value, fallback) {
  const parsed = parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

// GET /api/tasks
router.get('/', async (req, res) => {
  const { tenantId } = req;
  const { status, role, limit = 50, offset = 0 } = req.query;

  try {
    const conditions = ['t.tenant_id = $1'];
    const params = [tenantId];
    let idx = 2;

    if (status) {
      const statuses = String(status)
        .split(',')
        .map((s) => s.trim())
        .filter((s) => VALID_STATUSES.includes(s));

      if (statuses.length === 0) {
        return res.status(400).json({ error: 'Invalid status value' });
      }

      conditions.push(`t.status = ANY($${idx})`);
      params.push(statuses);
      idx += 1;
    }

    if (role) {
      conditions.push(`t.assignee_role = $${idx}`);
      params.push(role);
      idx += 1;
    }

    const whereClause = conditions.join(' AND ');
    const limitInt = Math.min(Math.max(parsePositiveInt(limit, 50), 1), 200);
    const offsetInt = Math.max(parsePositiveInt(offset, 0), 0);

    // listParams appends LIMIT/OFFSET; countParams stops before them
    const listParams = [...params, limitInt, offsetInt];
    const countParams = [...params];

    const sql = `
      SELECT
        t.id, t.status, t.priority, t.assignee_role, t.due_at,
        t.acknowledged_at, t.resolved_at, t.resolution_code,
        t.escalation_count, t.created_at, t.updated_at,
        p.name AS product_name, p.sku,
        r.name AS rule_name
      FROM tasks t
      JOIN products p ON t.product_id = p.id
      JOIN rules r    ON t.rule_id    = r.id
      WHERE ${whereClause}
      ORDER BY
        CASE t.priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
        t.due_at ASC NULLS LAST,
        t.created_at DESC
      LIMIT $${idx} OFFSET $${idx + 1}
    `;

    const [tasksResult, countResult] = await Promise.all([
      queryWithTenant(tenantId, sql, listParams),
      queryWithTenant(tenantId, `SELECT COUNT(*) FROM tasks t WHERE ${whereClause}`, countParams),
    ]);

    return res.json({
      tasks: tasksResult.rows,
      total: parseInt(countResult.rows[0].count, 10),
      limit: limitInt,
      offset: offsetInt,
    });
  } catch (err) {
    console.error('GET /tasks error:', err);
    return res.status(500).json({ error: 'Failed to fetch tasks' });
  }
});

// GET /api/tasks/:id
router.get('/:id', async (req, res) => {
  const { tenantId } = req;
  const { id } = req.params;

  try {
    const result = await queryWithTenant(
      tenantId,
      `SELECT
         t.*,
         p.name AS product_name, p.sku, p.images, p.category_path,
         r.name AS rule_name, r.parameters AS rule_parameters
       FROM tasks t
       JOIN products p ON t.product_id = p.id
       JOIN rules r    ON t.rule_id    = r.id
       WHERE t.id = $2 AND t.tenant_id = $1`,
      [tenantId, id]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Task not found' });
    }

    return res.json(result.rows[0]);
  } catch (err) {
    console.error('GET /tasks/:id error:', err);
    return res.status(500).json({ error: 'Failed to fetch task' });
  }
});

// POST /api/tasks/:id/status
router.post('/:id/status', async (req, res) => {
  const { tenantId } = req;
  const { id } = req.params;
  const { status, resolution_code, blocked_reason_code } = req.body;

  if (!status || !VALID_STATUSES.includes(status)) {
    return res.status(400).json({ error: `status must be one of: ${VALID_STATUSES.join(', ')}` });
  }

  try {
    await withTenantClient(tenantId, async (client) => {
      const existing = await client.query(
        'SELECT id FROM tasks WHERE id = $1 AND tenant_id = $2',
        [id, tenantId]
      );

      if (existing.rows.length === 0) {
        throw Object.assign(new Error('Task not found'), { statusCode: 404 });
      }

      await client.query(
        `UPDATE tasks SET
           status              = $3,
           acknowledged_at     = CASE WHEN $3 = 'in_progress' AND acknowledged_at IS NULL THEN NOW() ELSE acknowledged_at END,
           resolved_at         = CASE WHEN $3 IN ('resolved', 'closed') THEN NOW() ELSE resolved_at END,
           resolution_code     = CASE WHEN $3 IN ('resolved', 'closed') THEN $4 ELSE resolution_code END,
           blocked_reason_code = CASE WHEN $3 = 'blocked' THEN $5 ELSE blocked_reason_code END,
           updated_at          = NOW()
         WHERE id = $1 AND tenant_id = $2`,
        [id, tenantId, status, resolution_code || 'manually_resolved', blocked_reason_code || null]
      );

      await client.query(
        `INSERT INTO audit_log (tenant_id, user_id, action, entity_type, entity_id, new_value)
         VALUES ($1, $2, 'task_status_update', 'task', $3, $4)`,
        [tenantId, req.userId || null, id, JSON.stringify({ status, resolution_code, blocked_reason_code })]
      );
    });

    return res.json({ success: true, status });
  } catch (err) {
    if (err.statusCode === 404) {
      return res.status(404).json({ error: 'Task not found' });
    }
    console.error('POST /tasks/:id/status error:', err);
    return res.status(500).json({ error: 'Failed to update task status' });
  }
});

// POST /api/tasks
router.post('/', async (req, res) => {
  const { tenantId } = req;
  const { product_id, rule_id, assignee_role, priority = 'medium', deadline_hours = 24 } = req.body;

  if (!product_id || !rule_id || !assignee_role) {
    return res.status(400).json({ error: 'product_id, rule_id and assignee_role are required' });
  }

  if (!VALID_PRIORITIES.includes(priority)) {
    return res.status(400).json({ error: 'priority must be low, medium or high' });
  }

  const hoursInt = parsePositiveInt(deadline_hours, NaN);
  if (Number.isNaN(hoursInt) || hoursInt < 1 || hoursInt > 720) {
    return res.status(400).json({ error: 'deadline_hours must be between 1 and 720' });
  }

  try {
    const result = await queryWithTenant(
      tenantId,
      `INSERT INTO tasks (tenant_id, product_id, rule_id, assignee_role, priority, due_at)
       VALUES ($1, $2, $3, $4, $5, NOW() + ($6 * INTERVAL '1 hour'))
       RETURNING *`,
      [tenantId, product_id, rule_id, assignee_role, priority, hoursInt]
    );

    return res.status(201).json(result.rows[0]);
  } catch (err) {
    console.error('POST /tasks error:', err);
    return res.status(500).json({ error: 'Failed to create task' });
  }
});

module.exports = router;
