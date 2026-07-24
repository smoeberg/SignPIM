const rateLimit = require('express-rate-limit');

/**
 * NOTE: These limiters use the default in-memory store.
 * That means limits are NOT shared across multiple server instances.
 * For multi-instance deployments, swap the store for a Redis-backed one:
 *
 *   const RedisStore = require('rate-limit-redis');
 *   store: new RedisStore({ client: redisClient })
 *
 * Single-instance deployments (PM2 cluster mode counts as multi-instance)
 * should configure Redis before going to production.
 */

const globalLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 200,
  message: { error: 'Too many requests, please try again later' },
  standardHeaders: true,
  legacyHeaders: false,
});

// Only counts failed requests — successful auth doesn't burn budget
const authLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 30,
  skipSuccessfulRequests: true,
  message: { error: 'Too many authentication attempts, try again later' },
  standardHeaders: true,
  legacyHeaders: false,
});

const tenantLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 1000,
  keyGenerator: (req) => req.tenantId || req.ip,
  message: { error: 'Rate limit exceeded for this tenant' },
  standardHeaders: true,
  legacyHeaders: false,
});

// Higher ceiling for batch imports
const webhookLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 5000,
  keyGenerator: (req) => req.tenantId || req.ip,
  message: { error: 'Webhook rate limit exceeded' },
  standardHeaders: true,
  legacyHeaders: false,
});

module.exports = { globalLimiter, authLimiter, tenantLimiter, webhookLimiter };
