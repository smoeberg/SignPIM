class Product {
  constructor(id, sku, name, status, attributes = [], media = []) {
    this.id = id;
    this.sku = sku;
    this.name = name;
    this.status = status || 'draft';
    this.attributes = attributes;
    this.media = media;
    this.provenance = [];
  }

  canPublish() {
    return this.status === 'active' && this.media.length > 0;
  }

  recordChange(field, oldValue, newValue, source, user) {
    this.provenance.push({
      field,
      oldValue,
      newValue,
      source,
      user,
      timestamp: new Date().toISOString()
    });
  }
}

module.exports = { Product };
