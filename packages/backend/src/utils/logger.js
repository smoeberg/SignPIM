const logger = {
  info: (msg, meta = {}) => console.log(`[INFO] ${msg}`, JSON.stringify(meta)),
  warn: (msg, meta = {}) => console.warn(`[WARN] ${msg}`, JSON.stringify(meta)),
  error: (msg, meta = {}) => console.error(`[ERROR] ${msg}`, JSON.stringify(meta)),
};

module.exports = logger;
