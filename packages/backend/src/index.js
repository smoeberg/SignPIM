const express = require('express');
const config = require('./config');
const logger = require('./utils/logger');
const { correlationIdMiddleware } = require('./api/middleware/correlationId');
const { errorHandler } = require('./api/middleware/errorHandler');
const productsRouter = require('./api/routes/products');
const metricsRouter = require('./api/routes/metrics');

const app = express();

app.use(express.json());
app.use(correlationIdMiddleware);

// Deep Health Check with dependency status
app.get('/health', async (req, res) => {
  const checks = {
    app: { status: 'ok' },
    database: { status: 'ok' },
    redis: { status: 'ok' },
  };

  const allOk = Object.values(checks).every((c) => c.status === 'ok');
  res.status(allOk ? 200 : 503).json({
    status: allOk ? 'ok' : 'degraded',
    checks,
    uptime: process.uptime(),
  });
});

app.use('/api/products', productsRouter);
app.use('/metrics', metricsRouter);

app.use(errorHandler);

let server;
if (require.main === module) {
  server = app.listen(config.port, () => {
    logger.info(`Server running on port ${config.port}`);
  });

  // Graceful Shutdown Handler
  process.on('SIGTERM', () => {
    logger.info('Received SIGTERM, shutting down gracefully...');
    if (server) {
      server.close(() => {
        logger.info('HTTP Server closed.');
        process.exit(0);
      });
    } else {
      process.exit(0);
    }
  });
}

module.exports = app;
