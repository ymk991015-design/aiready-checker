from flask import Flask, request, jsonify, render_template_string
import requests
from bs4 import BeautifulSoup
import json
import re
from urllib.parse import urljoin, urlparse

app = Flask(__name__)

REQUIRED_FIELDS = {
    "name":              {"label": "Product Name",              "weight": 5},
    "description":       {"label": "Description",               "weight": 8},
    "image":             {"label": "Product Image",             "weight": 5},
    "brand":             {"label": "Brand",                     "weight": 12},
    "offers":            {"label": "Price / Offers",            "weight": 8},
    "aggregateRating":   {"label": "Aggregate Rating",          "weight": 15},
    "gtin":              {"label": "GTIN / Barcode",            "weight": 10},
    "mpn":               {"label": "MPN (Manufacturer Part No)","weight": 8},
    "material":          {"label": "Material",                  "weight": 10},
    "color":             {"label": "Color",                     "weight": 8},
    "size":              {"label": "Size",                      "weight": 8},
    "availability":      {"label": "Availability Status",       "weight": 8},
    "sku":               {"label": "SKU",                       "weight": 5},
}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AiReady — Schema Checker</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0a0a0f; --surface: #13131a; --border: #22222e;
      --text: #e8e8f0; --muted: #7a7a9a; --accent: #7c3aed;
      --accent-light: #a78bfa; --green: #22c55e; --red: #ef4444; --yellow: #f59e0b;
    }
    body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; }
    nav { padding: 20px 40px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
    .logo { font-size: 18px; font-weight: 800; color: #fff; }
    .logo span { color: var(--accent-light); }
    .badge { font-size: 11px; background: rgba(124,58,237,0.15); color: var(--accent-light); border: 1px solid rgba(167,139,250,0.3); padding: 4px 12px; border-radius: 20px; font-weight: 600; }
    .hero { max-width: 680px; margin: 0 auto; padding: 80px 40px 60px; text-align: center; }
    h1 { font-size: 40px; font-weight: 800; color: #fff; letter-spacing: -1px; margin-bottom: 14px; }
    h1 em { font-style: normal; color: var(--accent-light); }
    .sub { font-size: 17px; color: var(--muted); margin-bottom: 40px; line-height: 1.7; }
    .form-box { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
    input[type=text] { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 14px 20px; border-radius: 10px; font-size: 15px; width: 380px; outline: none; transition: border-color 0.2s; }
    input[type=text]:focus { border-color: var(--accent); }
    input::placeholder { color: var(--muted); }
    .btn { background: var(--accent); color: #fff; border: none; padding: 14px 28px; border-radius: 10px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
    .btn:hover { background: #6d28d9; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .results { max-width: 860px; margin: 0 auto; padding: 0 40px 80px; }
    .product-card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 28px; margin-bottom: 20px; }
    .product-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
    .product-name { font-size: 16px; font-weight: 600; color: #fff; }
    .product-url { font-size: 12px; color: var(--muted); margin-top: 4px; }
    .score-badge { font-size: 22px; font-weight: 800; padding: 8px 18px; border-radius: 10px; }
    .score-high { background: rgba(34,197,94,0.12); color: var(--green); }
    .score-mid  { background: rgba(245,158,11,0.12); color: var(--yellow); }
    .score-low  { background: rgba(239,68,68,0.12);  color: var(--red); }
    .fields-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
    .field-item { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 8px; font-size: 13px; }
    .field-ok   { background: rgba(34,197,94,0.07); color: #86efac; }
    .field-miss { background: rgba(239,68,68,0.07); color: #fca5a5; }
    .field-dot  { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .dot-ok   { background: var(--green); }
    .dot-miss { background: var(--red); }
    .summary-bar { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 24px 28px; margin-bottom: 28px; display: flex; gap: 40px; flex-wrap: wrap; }
    .summary-stat .num { font-size: 28px; font-weight: 800; color: #fff; }
    .summary-stat .lbl { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .error-box { background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.3); border-radius: 12px; padding: 20px 24px; color: #fca5a5; font-size: 14px; margin-bottom: 20px; }
    .loading { text-align: center; padding: 40px; color: var(--muted); font-size: 15px; }
    .spinner { display: inline-block; width: 20px; height: 20px; border: 2px solid var(--border); border-top-color: var(--accent-light); border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 10px; vertical-align: middle; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 600px) { .hero { padding: 60px 20px 40px; } h1 { font-size: 28px; } input[type=text] { width: 100%; } .results { padding: 0 20px 60px; } }
  </style>
</head>
<body>
<nav>
  <div class="logo">Ai<span>Ready</span></div>
  <div class="badge">Schema Checker</div>
</nav>

<div class="hero">
  <h1>Check your store's <em>AI Readiness</em></h1>
  <p class="sub">Enter your Shopify store URL. We'll scan up to 5 products and show you exactly what schema fields are missing — the ones that determine whether AI engines recommend your products.</p>
  <div class="form-box">
    <input type="text" id="storeUrl" placeholder="yourstore.myshopify.com or yourstore.com" />
    <button class="btn" id="scanBtn" onclick="runScan()">Scan Store</button>
  </div>
</div>

<div class="results" id="results"></div>

<script>
async function runScan() {
  const url = document.getElementById('storeUrl').value.trim();
  if (!url) return;
  const btn = document.getElementById('scanBtn');
  const results = document.getElementById('results');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  results.innerHTML = '<div class="loading"><span class="spinner"></span>Fetching product data — this takes about 10 seconds...</div>';
  try {
    const res = await fetch('/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (data.error) {
      results.innerHTML = `<div class="error-box">Error: ${data.error}</div>`;
    } else {
      renderResults(data);
    }
  } catch(e) {
    results.innerHTML = '<div class="error-box">Could not connect to scanner. Make sure the store URL is correct.</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Scan Store';
}

function scoreClass(s) {
  if (s >= 70) return 'score-high';
  if (s >= 40) return 'score-mid';
  return 'score-low';
}

function renderResults(data) {
  const results = document.getElementById('results');
  const avgScore = Math.round(data.products.reduce((a,p) => a + p.score, 0) / data.products.length);
  const totalIssues = data.products.reduce((a,p) => a + p.missing.length, 0);

  let html = `
    <div class="summary-bar">
      <div class="summary-stat"><div class="num">${data.products.length}</div><div class="lbl">Products scanned</div></div>
      <div class="summary-stat"><div class="num" style="color:${avgScore >= 70 ? '#22c55e' : avgScore >= 40 ? '#f59e0b' : '#ef4444'}">${avgScore}</div><div class="lbl">Average AI Readiness Score</div></div>
      <div class="summary-stat"><div class="num" style="color:#ef4444">${totalIssues}</div><div class="lbl">Total missing fields</div></div>
    </div>`;

  for (const p of data.products) {
    html += `
      <div class="product-card">
        <div class="product-header">
          <div>
            <div class="product-name">${p.name}</div>
            <div class="product-url">${p.url}</div>
          </div>
          <div class="score-badge ${scoreClass(p.score)}">${p.score}/100</div>
        </div>
        <div class="fields-grid">`;
    for (const f of p.present) {
      html += `<div class="field-item field-ok"><div class="field-dot dot-ok"></div>${f}</div>`;
    }
    for (const f of p.missing) {
      html += `<div class="field-item field-miss"><div class="field-dot dot-miss"></div>${f} — missing</div>`;
    }
    html += `</div></div>`;
  }
  results.innerHTML = html;
}

document.getElementById('storeUrl').addEventListener('keydown', e => {
  if (e.key === 'Enter') runScan();
});
</script>
</body>
</html>
"""

def get_product_urls(store_url):
    """Get product URLs from Shopify sitemap."""
    base = store_url.rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; AiReadyBot/1.0)'}
    urls = []
    try:
        sitemap_url = f"{base}/sitemap_products_1.xml"
        r = requests.get(sitemap_url, headers=headers, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'xml')
            locs = soup.find_all('loc')
            urls = [l.text.strip() for l in locs if '/products/' in l.text][:5]
    except Exception:
        pass
    if not urls:
        try:
            r = requests.get(f"{base}/products.json?limit=5", headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                for p in data.get('products', []):
                    handle = p.get('handle', '')
                    if handle:
                        urls.append(f"{base}/products/{handle}")
        except Exception:
            pass
    return urls[:5]

def extract_schema(url):
    """Extract JSON-LD schema from a product page."""
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; AiReadyBot/1.0)'}
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        scripts = soup.find_all('script', type='application/ld+json')
        for script in scripts:
            try:
                data = json.loads(script.string or '{}')
                if isinstance(data, list):
                    for item in data:
                        if item.get('@type') == 'Product':
                            return item
                elif data.get('@type') == 'Product':
                    return data
            except Exception:
                continue
    except Exception:
        pass
    return None

def check_field(schema, field):
    """Check if a field exists and has a value."""
    if field == 'availability':
        offers = schema.get('offers', {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        return bool(offers.get('availability'))
    if field == 'gtin':
        return any(schema.get(k) for k in ['gtin', 'gtin8', 'gtin12', 'gtin13', 'gtin14'])
    val = schema.get(field)
    if val is None:
        return False
    if isinstance(val, (dict, list)):
        return bool(val)
    return bool(str(val).strip())

def score_product(schema):
    """Calculate AI Readiness Score."""
    total_weight = sum(f['weight'] for f in REQUIRED_FIELDS.values())
    earned = 0
    present = []
    missing = []
    for field, meta in REQUIRED_FIELDS.items():
        if check_field(schema, field):
            earned += meta['weight']
            present.append(meta['label'])
        else:
            missing.append(meta['label'])
    score = round((earned / total_weight) * 100)
    return score, present, missing

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json()
    store_url = data.get('url', '').strip()
    if not store_url:
        return jsonify({'error': 'Please provide a store URL.'})
    product_urls = get_product_urls(store_url)
    if not product_urls:
        return jsonify({'error': 'Could not find products. Make sure the store URL is correct and the store is live.'})
    results = []
    for url in product_urls:
        schema = extract_schema(url)
        if not schema:
            continue
        score, present, missing = score_product(schema)
        name = schema.get('name', url.split('/')[-1].replace('-', ' ').title())
        results.append({'url': url, 'name': name, 'score': score, 'present': present, 'missing': missing})
    if not results:
        return jsonify({'error': 'Could not extract schema from product pages. The store may require JavaScript rendering.'})
    return jsonify({'products': results})

if __name__ == '__main__':
    app.run(debug=True, port=5001)
