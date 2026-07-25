const requiredEnv = ['DB_HOST', 'DB_PASSWORD', 'DB_NAME', 'REDIS_HOST'];
const missing = requiredEnv.filter(key => !process.env[key]);

if (process.env.NODE_ENV === 'production' && missing.length > 0) {
  console.error(`❌ Missing environment variables: ${missing.join(', ')}`);
  process.exit(1);
}

module.exports = {
  port: process.env.PORT || 3000,
  env: process.env.NODE_ENV || 'development',
  db: {
    host: process.env.DB_HOST || 'localhost',
    port: process.env.DB_PORT || 5432,
    database: process.env.DB_NAME || 'signpim',
    user: process.env.DB_USER || 'postgres',
    password: process.env.DB_PASSWORD || 'postgres',
  }
};
