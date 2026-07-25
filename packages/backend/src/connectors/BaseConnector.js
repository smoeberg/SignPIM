class BaseConnector {
  async import() { throw new Error('Not implemented'); }
  async normalize(data) { return data; }
  async validate(data) { return true; }
  async publish(data) { throw new Error('Not implemented'); }
  async report(status) { return { status }; }
}

module.exports = { BaseConnector };
