const express = require('express');
const router = express.Router();
const { productSchema } = require('../validation/productSchema');
const { AppError } = require('../middleware/errorHandler');

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
    res.status(201).json({ status: 'created', data: result.data });
  } catch (err) {
    next(err);
  }
});

module.exports = router;
