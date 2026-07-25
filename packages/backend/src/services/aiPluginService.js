class AIPluginService {
  async classify(product) {
    return { category: 'Furniture', confidence: 0.95 };
  }

  async generateDescription(product) {
    return `High-quality ${product.name} designed for comfort and style.`;
  }

  async detectDuplicates(product, catalog) {
    return [];
  }
}

module.exports = { AIPluginService };
