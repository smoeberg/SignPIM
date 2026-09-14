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
    const imgs = (d.images || []);
    tr.innerHTML = `
      <td>${d.sku ?? p.sku ?? ''}</td>
      <td>${d.name ?? ''}</td>
      <td>${d.price != null ? d.price.toFixed(2) : ''}</td>
      <td style="color:${scoreColor(score)};font-weight:600">${score != null ? score.toFixed(1) : '–'}</td>
      <td>${imgs.length ? imgs.map(u => `<img src="${u}" class="thumb" loading="lazy">`).join('') : '<span class="noimg">–</span>'}</td>
      <td>${p.version ?? ''}</td>
      <td><input type="file" accept="image/*" onchange="uploadImage(this, '${p.sku}')"></td>`;
    body.appendChild(tr);
  }
}

async function uploadImage(input, sku) {
  const f = input.files[0];
  if (!f) return;
  const r = await fetch(`/products/${encodeURIComponent(sku)}/images?tenant=${tenant()}`,
                        { method: 'POST', headers: headers(), body: f });
  if (r.ok) { loadAll(); }
  else { const j = await r.json().catch(() => ({})); alert(`Upload fejlede (${r.status}): ${j.detail || ''}`); }
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
  updateWhoami();
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


/* ---------- Session login (multi-user) ---------- */
async function doLogin() {
  const t = tenant(), email = $('login-email').value.trim(), pw = $('login-password').value;
  const r = await fetch(`/auth/login?tenant=${t}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ email, password: pw }),
  });
  if (r.ok) {
    const j = await r.json();
    sessionStorage.setItem('spim_token', j.token);
    sessionStorage.setItem('spim_who', j.email || email);
    loadAll();
  } else { alert('Login fejlede: ' + r.status); }
}

function doLogout() {
  sessionStorage.removeItem('spim_token');
  sessionStorage.removeItem('spim_who');
  location.reload();
}

async function updateWhoami() {
  try {
    const r = await fetch('/auth/me', { headers: headers() });
    if (r.ok) {
      const j = await r.json();
      $('whoami').textContent = `${j.email || 'user'} · ${j.role || ''}`;
      $('user-badge').style.display = 'inline-flex';
      $('logout-btn2').classList.remove('hidden');
      $('login-toggle').classList.add('hidden');
    } else {
      $('user-badge').style.display = 'none';
      $('logout-btn2').classList.add('hidden');
      $('login-toggle').classList.remove('hidden');
    }
  } catch { /* offline */ }
}

/* ---------- ERP export ---------- */
function downloadExport() {
  const fmt = $('export-format').value || 'csv';
  window.open(`/export/${tenant()}?fmt=${fmt}`, '_blank');
}

$('login-toggle').addEventListener('click', () => $('login-panel').classList.toggle('hidden'));
$('login-btn').addEventListener('click', doLogin);
$('logout-btn').addEventListener('click', doLogout);
$('logout-btn2').addEventListener('click', doLogout);
$('export-btn').addEventListener('click', downloadExport);
