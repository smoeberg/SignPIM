const jwt = require('jsonwebtoken');
const { AppError } = require('./errorHandler');

const JWT_SECRET = process.env.JWT_SECRET;

if (!JWT_SECRET && process.env.NODE_ENV === 'production') {
  console.error("FATAL: JWT_SECRET environment variable is missing.");
  process.exit(1);
}

const authenticateToken = (req, res, next) => {
  const authHeader = req.headers['authorization'];
  const token = authHeader && authHeader.split(' ')[1];

  if (!token) {
    return next(new AppError('Authentication token required', 401, 'UNAUTHORIZED'));
  }

  const secret = JWT_SECRET || 'dev_secret_key_only_for_local_testing';
  jwt.verify(token, secret, (err, user) => {
    if (err) {
      return next(new AppError('Invalid or expired token', 403, 'FORBIDDEN'));
    }
    req.user = user;
    next();
  });
};

module.exports = { authenticateToken };
