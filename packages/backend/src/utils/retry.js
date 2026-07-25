async function retry(fn, options = {}) {
  const { maxAttempts = 3, delay = 1000, backoff = 'exponential' } = options;
  let lastError;
  let currentDelay = delay;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      if (attempt === maxAttempts) break;
      if (backoff === 'exponential') {
        currentDelay = delay * Math.pow(2, attempt - 1);
      }
      await new Promise((resolve) => setTimeout(resolve, currentDelay));
    }
  }
  throw lastError;
}

module.exports = { retry };
