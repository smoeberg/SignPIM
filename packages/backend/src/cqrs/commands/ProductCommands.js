class CreateProductCommand {
  constructor(payload) {
    this.payload = payload;
  }
}

class ApproveProductCommand {
  constructor(productId, userId) {
    this.productId = productId;
    this.userId = userId;
  }
}

module.exports = { CreateProductCommand, ApproveProductCommand };
