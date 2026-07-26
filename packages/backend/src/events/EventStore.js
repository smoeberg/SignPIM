const fs = require('fs').promises;
const path = require('path');
const crypto = require('crypto');

class EventStore {
  constructor(storagePath) {
    this.storagePath = storagePath || path.join(__dirname, '../../data/events.ndjson');
    this.ensureStorage();
  }

  async ensureStorage() {
    const dir = path.dirname(this.storagePath);
    try {
      await fs.mkdir(dir, { recursive: true });
    } catch (e) {
      // Directory exists
    }
  }

  async getAll() {
    try {
      const data = await fs.readFile(this.storagePath, 'utf8');
      return data
        .trim()
        .split('\n')
        .filter(line => line.length > 0)
        .map(line => JSON.parse(line));
    } catch {
      return [];
    }
  }

  async publish(eventType, payload, metadata = {}) {
    await this.ensureStorage();
    const event = {
      id: crypto.randomUUID(),
      eventType,
      payload,
      metadata,
      timestamp: new Date().toISOString()
    };
    const line = JSON.stringify(event) + '\n';
    await fs.appendFile(this.storagePath, line, 'utf8');
    return event;
  }
}

module.exports = { EventStore };
