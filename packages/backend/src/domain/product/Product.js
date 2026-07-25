class Product {
  constructor(id, sku, name, status, attributes = [], media = []) {
    this.id = id;
    this.sku = sku;
    this.name = name;
    this.status = status || 'draft'; // Valid values: draft, ready, published
    this.attributes = attributes;
    this.media = media;
    this.provenance = [];
  }

  canPublish() {
    // Aligned with Product.yaml status enum: [draft, ready, published]
    return (this.status === 'ready' || this.status === 'published') && this.media.length > 0;
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
