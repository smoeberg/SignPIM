const z = require('zod');

const productSchema = z.object({
  identifier: z.string().min(1).max(255),
  name: z.string().min(3).max(255),
  price_reference: z.number().optional().min(0),
  ean: z.string().optional().regex(/^\d{13}$/),
});

module.exports = { productSchema };
