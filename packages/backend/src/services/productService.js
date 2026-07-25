const { AppError } = require('../api/middleware/errorHandler');
const { ERROR_CODES } = require('../constants/errors');
const { PRODUCT_STATUS } = require('../constants/product');

class ProductService {
  constructor(repository) {
    this.repository = repository;
    this.productsMap = new Map(); // Local in-memory repository store fallback
  }

  async create(product) {
    if (product.status === PRODUCT_STATUS.ACTIVE && (!product.images || !product.images.length)) {
      throw new AppError('Active products must have at least one image', 400, ERROR_CODES.VALIDATION_ERROR);
    }
    if (this.repository) {
      return this.repository.save(product);
    }
    this.productsMap.set(product.identifier || product.sku, product);
    return product;
  }

  async createBulk(products) {
    if (!Array.isArray(products) || products.length === 0) {
      throw new AppError('Products array required for bulk creation', 400, ERROR_CODES.VALIDATION_ERROR);
    }
    const created = [];
    for (const p of products) {
      const saved = await this.create(p);
      created.push(saved);
    }
    return created;
  }

  async updateBulk(updates) {
    if (!Array.isArray(updates) || updates.length === 0) {
      throw new AppError('Updates array required for bulk update', 400, ERROR_CODES.VALIDATION_ERROR);
    }
    const updated = [];
    for (const item of updates) {
      const id = item.identifier || item.sku;
      const existing = this.productsMap.get(id) || {};
      const merged = { ...existing, ...item, updatedAt: new Date().toISOString() };
      this.productsMap.set(id, merged);
      updated.push(merged);
    }
    return updated;
  }

  async deleteBulk(identifiers) {
    if (!Array.isArray(identifiers) || identifiers.length === 0) {
      throw new AppError('Identifiers array required for bulk deletion', 400, ERROR_CODES.VALIDATION_ERROR);
    }
    let deletedCount = 0;
    for (const id of identifiers) {
      if (this.productsMap.has(id)) {
        this.productsMap.delete(id);
        deletedCount++;
      }
    }
    return { deletedCount, remainingCount: this.productsMap.size };
  }
}

module.exports = { ProductService };
