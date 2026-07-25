const express = require('express');
const config = require('./config');
const { correlationIdMiddleware } = require('./api/middleware/correlationId');
const { errorHandler } = require('./api/middleware/errorHandler');
const productsRouter = require('./api/routes/products');
const metricsRouter = require('./api/routes/metrics');

const app = express();

app.use(express.json());
app.use(correlationIdMiddleware);

app.use('/api/products', productsRouter);
app.use('/metrics', metricsRouter);

app.use(errorHandler);

if (require.main === module) {
  app.listen(config.port, () => {
    console.log(`Server running on port ${config.port}`);
  });
}

module.exports = app;
