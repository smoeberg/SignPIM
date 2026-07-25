class FindProductQuery {
  constructor(productId) {
    this.productId = productId;
  }
}

class SearchProductQuery {
  constructor(filters) {
    this.filters = filters;
  }
}

module.exports = { FindProductQuery, SearchProductQuery };
