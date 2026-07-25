const { AppError } = require('../api/middleware/errorHandler');
const { ERROR_CODES } = require('../constants/errors');
const { PRODUCT_STATUS } = require('../constants/product');

class ProductService {
  constructor(repository) {
    this.repository = repository;
  }

  async create(product) {
    if (product.status === PRODUCT_STATUS.ACTIVE && (!product.images || !product.images.length)) {
      throw new AppError('Active products must have at least one image', 400, ERROR_CODES.VALIDATION_ERROR);
    }
    return this.repository ? this.repository.save(product) : product;
  }

  async createBulk(products) {
    if (!Array.isArray(products) || products.length === 0) {
      throw new AppError('Products array required', 400, ERROR_CODES.VALIDATION_ERROR);
    }
    return products.map((p) => ({ ...p, status: p.status || PRODUCT_STATUS.DRAFT }));
  }

  async updateBulk(updates) {
    return updates;
  }

  async deleteBulk(identifiers) {
    return { deleted: identifiers.length };
  }
}

module.exports = { ProductService };
