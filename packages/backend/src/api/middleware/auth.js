const jwt = require('jsonwebtoken');
const { AppError } = require('./errorHandler');

const JWT_SECRET = process.env.JWT_SECRET || 'signpim_super_secret_jwt_key_2026';

const authenticateToken = (req, res, next) => {
  const authHeader = req.headers['authorization'];
  const token = authHeader && authHeader.split(' ')[1];

  if (!token) {
    if (process.env.NODE_ENV === 'development') {
      req.user = { id: 'dev_user', tenantId: 'TENANT_DEV', role: 'admin' };
      return next();
    }
    return next(new AppError('Authentication token required', 401, 'UNAUTHORIZED'));
  }

  jwt.verify(token, JWT_SECRET, (err, user) => {
    if (err) {
      return next(new AppError('Invalid or expired token', 403, 'FORBIDDEN'));
    }
    req.user = user;
    next();
  });
};

module.exports = { authenticateToken, JWT_SECRET };
