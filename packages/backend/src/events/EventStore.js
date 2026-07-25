const fs = require('fs');
const path = require('path');

class EventStore {
  constructor(storagePath) {
    this.storagePath = storagePath || path.join(__dirname, '../../data/events.json');
    this.ensureStorage();
  }

  ensureStorage() {
    const dir = path.dirname(this.storagePath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    if (!fs.existsSync(this.storagePath)) {
      fs.writeFileSync(this.storagePath, JSON.stringify([]), 'utf8');
    }
  }

  async getAll() {
    try {
      const data = fs.readFileSync(this.storagePath, 'utf8');
      return JSON.parse(data);
    } catch {
      return [];
    }
  }

  async publish(eventType, payload, metadata = {}) {
    const events = await this.getAll();
    const event = {
      id: `${Date.now()}_${Math.random().toString(36).substring(2, 9)}`,
      eventType,
      payload,
      metadata,
      timestamp: new Date().toISOString()
    };
    events.push(event);
    fs.writeFileSync(this.storagePath, JSON.stringify(events, null, 2), 'utf8');
    return event;
  }
}

module.exports = { EventStore };
