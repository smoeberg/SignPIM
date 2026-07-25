class EventStore {
  constructor() {
    this.events = [];
  }

  async publish(eventType, payload, metadata = {}) {
    const event = {
      id: Date.now().toString(),
      eventType,
      payload,
      metadata,
      timestamp: new Date().toISOString()
    };
    this.events.push(event);
    return event;
  }
}

module.exports = { EventStore };
