const express = require('express');
const cors = require('cors');
const helmet = require('helmet');

const authMiddleware = require('./middleware/auth');
const { globalLimiter, authLimiter, tenantLimiter, webhookLimiter } = require('./middleware/rateLimit');

const webhookRoutes = require('./routes/webhook');
const taskRoutes = require('./routes/tasks');

const app = express();

app.use(helmet());
app.use(cors());
app.use(express.json({ limit: '10mb' }));

// Global rate limit
app.use('/api/', globalLimiter);

// Health check — no auth, no rate limit
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// Auth endpoints
app.use('/api/auth/', authLimiter);

// Public webhook ingestion — no auth (systems POST here before we know the tenant),
// but the auth middleware inside webhookRoutes validates x-api-key when required.
// Rate-limited by IP at this layer; tenant-level limiting applies post-auth.
//
// NOTE: GET /api/webhook/status is also inside webhookRoutes but requires
// req.tenantId — it is protected further down via the authMiddleware mount.
app.use('/api/webhook', authMiddleware, webhookLimiter, webhookRoutes);

// Protected API routes
app.use('/api/tasks', authMiddleware, tenantLimiter, taskRoutes);
app.use('/api/quality', authMiddleware, tenantLimiter, require('./routes/quality'));
app.use('/api/rules', authMiddleware, tenantLimiter, require('./routes/rules'));
app.use('/api/tenants', authMiddleware, tenantLimiter, require('./routes/tenants'));

// Internal endpoints — NOT public-facing (should sit behind a firewall or
// be served on a separate internal port in production)
const internalWebhookRoutes = require('./routes/webhook');
app.use('/api/internal/webhook', internalWebhookRoutes);

// Error handler
app.use((err, _req, res, _next) => {
  console.error('Unhandled error:', err);
  res.status(500).json({ error: 'Internal server error' });
});

// 404
app.use((_req, res) => {
  res.status(404).json({ error: 'Endpoint not found' });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});

module.exports = app;
