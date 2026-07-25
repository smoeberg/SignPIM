const express = require('express');
const router = express.Router();
const { productSchema } = require('../validation/productSchema');
const { AppError } = require('../middleware/errorHandler');
const { ProductService } = require('../../services/productService');

const productService = new ProductService();

router.get('/', async (req, res, next) => {
  try {
    res.json({ status: 'ok', data: [] });
  } catch (err) {
    next(err);
  }
});

router.post('/', async (req, res, next) => {
  try {
    const result = productSchema.safeParse(req.body);
    if (!result.success) {
      throw new AppError('Validation failed', 400, 'VALIDATION_ERROR');
    }
    const saved = await productService.create(result.data);
    res.status(201).json({ status: 'created', data: saved });
  } catch (err) {
    next(err);
  }
});

router.post('/bulk', async (req, res, next) => {
  try {
    const { products } = req.body;
    const results = await productService.createBulk(products);
    res.status(201).json({ status: 'bulk_created', count: results.length, data: results });
  } catch (err) {
    next(err);
  }
});

router.put('/bulk', async (req, res, next) => {
  try {
    const { updates } = req.body;
    const results = await productService.updateBulk(updates);
    res.json({ status: 'bulk_updated', data: results });
  } catch (err) {
    next(err);
  }
});

router.delete('/bulk', async (req, res, next) => {
  try {
    const { identifiers } = req.body;
    const results = await productService.deleteBulk(identifiers || []);
    res.json({ status: 'bulk_deleted', result: results });
  } catch (err) {
    next(err);
  }
});

module.exports = router;
