const request = require('supertest');
const jwt = require('jsonwebtoken');
const app = require('../../../packages/backend/src/index');

const TEST_SECRET = process.env.JWT_SECRET || 'dev_secret_key_only_for_local_testing';
const validToken = jwt.sign({ id: 'user1', tenantId: 'TENANT_TEST' }, TEST_SECRET);

describe('Express Products API Integration Tests', () => {
  it('GET /api/products should return 401 without auth token', async () => {
    const res = await request(app).get('/api/products');
    expect(res.statusCode).toEqual(401);
    expect(res.body.error).toEqual('UNAUTHORIZED');
  });

  it('GET /api/products should return 200 with valid JWT token', async () => {
    const res = await request(app)
      .get('/api/products')
      .set('Authorization', `Bearer ${validToken}`);
    expect(res.statusCode).toEqual(200);
    expect(res.body.status).toEqual('ok');
  });

  it('POST /api/products should validate input payload via Zod', async () => {
    const res = await request(app)
      .post('/api/products')
      .set('Authorization', `Bearer ${validToken}`)
      .send({ identifier: '' }); // Invalid empty identifier

    expect(res.statusCode).toEqual(400);
    expect(res.body.error).toEqual('VALIDATION_ERROR');
  });

  it('POST /api/products should create product with valid payload', async () => {
    const res = await request(app)
      .post('/api/products')
      .set('Authorization', `Bearer ${validToken}`)
      .send({
        identifier: 'SKU-SUPER-100',
        name: 'Super Sofa',
        price_reference: 299.99,
        ean: '5701234567890'
      });

    expect(res.statusCode).toEqual(201);
    expect(res.body.status).toEqual('created');
    expect(res.body.data.identifier).toEqual('SKU-SUPER-100');
  });

  it('POST /api/products/bulk should handle bulk creation', async () => {
    const res = await request(app)
      .post('/api/products/bulk')
      .set('Authorization', `Bearer ${validToken}`)
      .send({
        products: [
          { identifier: 'SKU-BULK-1', name: 'Product 1' },
          { identifier: 'SKU-BULK-2', name: 'Product 2' }
        ]
      });

    expect(res.statusCode).toEqual(201);
    expect(res.body.status).toEqual('bulk_created');
    expect(res.body.count).toEqual(2);
  });
});
