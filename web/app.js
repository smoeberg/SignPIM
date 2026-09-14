/* SignPIM Dashboard — vanilla JS, no build step, talks to the headless API. */
const $ = (id) => document.getElementById(id);

function headers() {
  const key = $('apikey').value.trim() || sessionStorage.getItem('spim_token') || '';
  return key ? { Authorization: `Bearer ${key}` } : {};
}

function tenant() { return $('tenant').value.trim() || 'acme'; }

function scoreColor(s) {
  if (s == null) return '#888';
  if (s >= 90) return '#22c55e';
  if (s >= 75) return '#eab308';
  return '#ef4444';
}

function renderQuality(summary) {
  const s = summary.quality_score;
  $('score-value').textContent = s == null ? '–' : s.toFixed(1);
  $('score-ring').style.borderColor = scoreColor(s);
  for (const [dim, val] of Object.entries(summary.by_dimension || {})) {
    $(`val-${dim}`).textContent = val == null ? '–' : `${val.toFixed(1)}%`;
    $(`bar-${dim}`).style.width = `${val ?? 0}%`;
    $(`bar-${dim}`).style.background = scoreColor(val);
  }
}

function renderProducts(products) {
  const body = $('products-body');
  body.innerHTML = '';
  $('product-count').textContent = `(${products.length})`;
  for (const p of products) {
    const tr = document.createElement('tr');
    const d = p.data || {};
    const score = p.quality_score;
    tr.innerHTML = `
      <td>${d.sku ?? p.sku ?? ''}</td>
      <td>${d.name ?? ''}</td>
      <td>${d.price != null ? d.price.toFixed(2) : ''}</td>
      <td style="color:${scoreColor(score)};font-weight:600">${score != null ? score.toFixed(1) : '–'}</td>
      <td>${p.version ?? ''}</td>`;
    body.appendChild(tr);
  }
}

async function loadAll() {
  const t = tenant(), h = headers();
  try {
    const qs = new URLSearchParams();
    if ($('min-score').value) qs.set('min_score', $('min-score').value);
    const [qRes, pRes] = await Promise.all([
      fetch(`/quality/${t}`, { headers: h }),
      fetch(`/products?${qs}&tenant=${t}`, { headers: h }),
    ]);
    if (qRes.ok) renderQuality(await qRes.json());
    else $('score-value').textContent = '–';
    if (pRes.ok) renderProducts(await pRes.json());
    else $('products-body').innerHTML = `<tr><td colspan="5">Fejl: ${pRes.status}</td></tr>`;
  } catch (e) {
    $('products-body').innerHTML = `<tr><td colspan="5">Netværksfejl: ${e}</td></tr>`;
  }
}

$('load').onclick = loadAll;
$('min-score').onchange = loadAll;

$('ingest-btn').onclick = async () => {
  const csv = $('csv-input').value;
  const res = await fetch(`/tenants/${tenant()}/ingest?body=${encodeURIComponent(csv)}`, {
    method: 'POST', headers: headers(),
  });
  const j = await res.json();
  $('ingest-result').textContent = JSON.stringify(j, null, 2);
  // ---------- login / logout ----------
$('login-toggle').onclick = () => $('login-panel').classList.toggle('hidden');

$('login-btn').onclick = async () => {
  const res = await fetch(`/auth/login?tenant=${tenant()}&email=${encodeURIComponent($('login-email').value)}&password=${encodeURIComponent($('login-password').value)}`, { method: 'POST' });
  const j = await res.json();
  if (res.ok) {
    sessionStorage.setItem('spim_token', j.token);
    sessionStorage.setItem('spim_user', JSON.stringify(j.user));
    $('login-result').classList.add('hidden');
    $('login-panel').classList.add('hidden');
    $('login-btn').classList.add('hidden');
    $('logout-btn').classList.remove('hidden');
    $('login-toggle').textContent = j.user.email;
    $('whoami').textContent = `${j.user.display_name || j.user.email} (${j.user.role})`;
    loadAll();
  } else {
    $('login-result').classList.remove('hidden');
    $('login-result').textContent = j.detail || 'Login fejlede';
  }
};

$('logout-btn').onclick = async () => {
  await fetch('/auth/logout', { method: 'POST', headers: headers() });
  sessionStorage.removeItem('spim_token');
  sessionStorage.removeItem('spim_user');
  $('whoami').textContent = '';
  $('logout-btn').classList.add('hidden');
  $('login-btn').classList.remove('hidden');
  $('login-toggle').textContent = 'Log ind';
  loadAll();
};

// restore session on load
(function restore() {
  const user = sessionStorage.getItem('spim_user');
  if (user) {
    const u = JSON.parse(user);
    $('whoami').textContent = `${u.display_name || u.email} (${u.role})`;
    $('login-toggle').textContent = u.email;
  }
  loadAll();
})();
};

loadAll();
