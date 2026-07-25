const express = require('express');
const router = express.Router();

router.get('/', async (req, res) => {
  res.set('Content-Type', 'text/plain');
  res.send('# HELP http_requests_total Total HTTP requests\n# TYPE http_requests_total counter\nhttp_requests_total 1\n');
});

module.exports = router;
