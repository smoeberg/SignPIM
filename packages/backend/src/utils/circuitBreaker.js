class CircuitBreaker {
  constructor(options = {}) {
    this.failureThreshold = options.failureThreshold || 5;
    this.timeout = options.timeout || 10000;
    this.state = 'closed';
    this.failures = 0;
  }

  async execute(fn) {
    if (this.state === 'open') {
      throw new Error('Circuit breaker is open');
    }

    try {
      const result = await Promise.race([
        fn(),
        new Promise((_, reject) => setTimeout(() => reject(new Error('Timeout')), this.timeout)),
      ]);
      this.failures = 0;
      return result;
    } catch (error) {
      this.failures++;
      if (this.failures >= this.failureThreshold) {
        this.state = 'open';
        setTimeout(() => {
          this.state = 'half-open';
        }, this.timeout);
      }
      throw error;
    }
  }
}

module.exports = { CircuitBreaker };
