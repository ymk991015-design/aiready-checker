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
  <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
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
    .btn-outline { background: transparent; color: var(--accent-light); border: 1px solid rgba(167,139,250,0.4); padding: 10px 22px; border-radius: 10px; font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
    .btn-outline:hover { background: rgba(124,58,237,0.1); }
    .results { max-width: 860px; margin: 0 auto; padding: 0 40px 80px; }
    .product-card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 28px; margin-bottom: 20px; }
    .product-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
    .product-name { font-size: 16px; font-weight: 600; color: #fff; }
    .product-url { font-size: 12px; color: var(--muted); margin-top: 4px; }
    .score-badge { font-size: 22px; font-weight: 800; padding: 8px 18px; border-radius: 10px; }
    .score-high { background: rgba(34,197,94,0.12); color: var(--green); }
    .score-mid  { background: rgba(245,158,11,0.12); color: var(--yellow); }
    .score-low  { background: rgba(239,68,68,0.12);  color: var(--red); }
    .fields-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .field-item { display: flex; align-items: flex-start; gap: 8px; padding: 8px 12px; border-radius: 8px; font-size: 13px; }
    .field-ok   { background: rgba(34,197,94,0.07); color: #86efac; }
    .field-miss { background: rgba(239,68,68,0.07); color: #fca5a5; cursor: pointer; }
    .field-miss:hover { background: rgba(239,68,68,0.14); }
    .field-dot  { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; margin-top: 4px; }
    .dot-ok   { background: var(--green); }
    .dot-miss { background: var(--red); }
    .fix-hint { font-size: 11px; color: #fca5a5; opacity: 0.7; margin-top: 3px; line-height: 1.4; display: none; }
    .field-miss.expanded .fix-hint { display: block; }
    .summary-bar { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 24px 28px; margin-bottom: 20px; display: flex; gap: 40px; flex-wrap: wrap; align-items: center; }
    .summary-stat .num { font-size: 28px; font-weight: 800; color: #fff; }
    .summary-stat .lbl { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .top-issues { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 24px 28px; margin-bottom: 28px; }
    .top-issues h3 { font-size: 14px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 14px; }
    .issue-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; font-size: 14px; }
    .issue-bar-wrap { flex: 1; background: rgba(239,68,68,0.1); border-radius: 4px; height: 6px; }
    .issue-bar { background: var(--red); border-radius: 4px; height: 6px; }
    .issue-count { font-size: 12px; color: var(--muted); min-width: 60px; text-align: right; }
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
  <p class="sub">Enter your Shopify store URL. We'll scan up to 20 products and show you exactly what schema fields are missing — the ones that determine whether AI engines recommend your products.</p>
  <div class="form-box">
    <input type="text" id="storeUrl" placeholder="yourstore.myshopify.com or yourstore.com" />
    <button class="btn" id="scanBtn" onclick="runScan()">Scan Store</button>
  </div>
</div>

<div class="results" id="results"></div>

<script>
const FIX_HINTS = {
  'Brand': 'Shopify Admin → Products → [product] → Vendor field',
  'Aggregate Rating': 'Install a reviews app (e.g. Judge.me, Loox) and enable structured data',
  'GTIN / Barcode': 'Shopify Admin → Products → [variant] → Barcode field (enter ISBN, UPC, GTIN, etc.)',
  'MPN (Manufacturer Part No)': 'Add metafield: namespace=product, key=mpn, or use SKU field',
  'Material': 'Add a product option named "Material" or add a metafield',
  'Color': 'Add a product option named "Color" or "Colour"',
  'Size': 'Add a product option named "Size"',
  'Availability Status': 'Enable inventory tracking in Shopify Admin → Products → [variant]',
  'Description': 'Shopify Admin → Products → [product] → Description (add detailed text)',
  'Price / Offers': 'Ensure at least one variant has a price set',
};

let lastData = null;

async function runScan() {
  const url = document.getElementById('storeUrl').value.trim();
  if (!url) return;
  const btn = document.getElementById('scanBtn');
  const results = document.getElementById('results');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  results.innerHTML = '<div class="loading"><span class="spinner"></span>Scanning up to 20 products — this may take 20-30 seconds...</div>';
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
      lastData = data;
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

function toggleFix(el) {
  el.classList.toggle('expanded');
}

function renderResults(data) {
  const results = document.getElementById('results');
  const s = data.summary;
  const totalIssues = data.products.reduce((a,p) => a + p.missing.length, 0);
  const maxCount = s.top_issues.length ? s.top_issues[0].count : 1;

  let html = `
    <div class="summary-bar">
      <div class="summary-stat"><div class="num">${s.total_products}</div><div class="lbl">Products scanned</div></div>
      <div class="summary-stat"><div class="num" style="color:${s.avg_score >= 70 ? '#22c55e' : s.avg_score >= 40 ? '#f59e0b' : '#ef4444'}">${s.avg_score}</div><div class="lbl">Average AI Readiness Score</div></div>
      <div class="summary-stat"><div class="num" style="color:#ef4444">${totalIssues}</div><div class="lbl">Total missing fields</div></div>
      <div style="margin-left:auto;"><button class="btn-outline" onclick="downloadPDF()">Download PDF Report</button></div>
    </div>`;

  if (s.top_issues.length) {
    html += `<div class="top-issues"><h3>Top issues across your store</h3>`;
    for (const issue of s.top_issues) {
      const pct = Math.round((issue.count / maxCount) * 100);
      html += `<div class="issue-row">
        <span style="min-width:180px;color:var(--text);font-size:14px;">${issue.field}</span>
        <div class="issue-bar-wrap"><div class="issue-bar" style="width:${pct}%"></div></div>
        <span class="issue-count">${issue.count} / ${s.total_products} products</span>
      </div>`;
    }
    html += `</div>`;
  }

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
      html += `<div class="field-item field-ok"><div class="field-dot dot-ok"></div><span>${f}</span></div>`;
    }
    for (const f of p.missing) {
      const hint = FIX_HINTS[f] || 'Add this field to improve AI discoverability';
      html += `<div class="field-item field-miss" onclick="toggleFix(this)">
        <div class="field-dot dot-miss"></div>
        <div><div>${f} — missing</div><div class="fix-hint">Fix: ${hint}</div></div>
      </div>`;
    }
    html += `</div></div>`;
  }
  results.innerHTML = html;
}

function downloadPDF() {
  if (!lastData) return;
  const { jsPDF } = window.jspdf;
  const doc = new jsPDF();
  const s = lastData.summary;
  const date = new Date().toLocaleDateString();

  doc.setFillColor(10, 10, 15);
  doc.rect(0, 0, 210, 297, 'F');

  doc.setTextColor(167, 139, 250);
  doc.setFontSize(22);
  doc.setFont('helvetica', 'bold');
  doc.text('AiReady — AI Readiness Report', 20, 25);

  doc.setTextColor(180, 180, 200);
  doc.setFontSize(11);
  doc.setFont('helvetica', 'normal');
  doc.text(`Store: ${s.store}`, 20, 36);
  doc.text(`Date: ${date}`, 20, 43);

  const scoreColor = s.avg_score >= 70 ? [34,197,94] : s.avg_score >= 40 ? [245,158,11] : [239,68,68];
  doc.setTextColor(...scoreColor);
  doc.setFontSize(36);
  doc.setFont('helvetica', 'bold');
  doc.text(`${s.avg_score}/100`, 20, 62);
  doc.setTextColor(120, 120, 150);
  doc.setFontSize(11);
  doc.setFont('helvetica', 'normal');
  doc.text('Average AI Readiness Score', 20, 70);
  doc.text(`${s.total_products} products scanned`, 20, 77);

  doc.setTextColor(167, 139, 250);
  doc.setFontSize(13);
  doc.setFont('helvetica', 'bold');
  doc.text('Top Issues Across Your Store', 20, 92);

  let y = 101;
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(10);
  for (const issue of s.top_issues) {
    doc.setTextColor(239, 68, 68);
    doc.text(`• ${issue.field}`, 22, y);
    doc.setTextColor(120, 120, 150);
    doc.text(`missing in ${issue.count}/${s.total_products} products`, 100, y);
    y += 8;
  }

  y += 8;
  doc.setTextColor(167, 139, 250);
  doc.setFontSize(13);
  doc.setFont('helvetica', 'bold');
  doc.text('Product Breakdown', 20, y);
  y += 10;

  for (const p of lastData.products) {
    if (y > 265) { doc.addPage(); doc.setFillColor(10,10,15); doc.rect(0,0,210,297,'F'); y = 20; }
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(10);
    const scoreCol = p.score >= 70 ? [34,197,94] : p.score >= 40 ? [245,158,11] : [239,68,68];
    doc.setTextColor(...scoreCol);
    doc.text(`${p.score}/100`, 20, y);
    doc.setTextColor(220, 220, 235);
    doc.text(p.name.length > 60 ? p.name.slice(0,57)+'...' : p.name, 42, y);
    y += 6;
    if (p.missing.length) {
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(9);
      doc.setTextColor(239, 100, 100);
      const missingText = 'Missing: ' + p.missing.join(', ');
      const lines = doc.splitTextToSize(missingText, 168);
      doc.text(lines, 22, y);
      y += lines.length * 5 + 4;
    } else {
      y += 4;
    }
  }

  doc.setTextColor(100, 100, 120);
  doc.setFontSize(8);
  doc.text('Generated by AiReady — aiready-checker.onrender.com', 20, 290);

  doc.save(`aiready-report-${s.store}-${date.replace(/\\//g,'-')}.pdf`);
}

document.getElementById('storeUrl').addEventListener('keydown', e => {
  if (e.key === 'Enter') runScan();
});
</script>
</body>
</html>
"""

def get_product_urls(store_url):
    """Get product URLs from Shopify store using multiple methods."""
    base = store_url.rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    urls = []

    # Method 1: products.json (most reliable)
    try:
        r = requests.get(f"{base}/products.json?limit=20", headers=headers, timeout=12)
        if r.status_code == 200:
            data = r.json()
            for p in data.get('products', []):
                handle = p.get('handle', '')
                if handle:
                    urls.append(f"{base}/products/{handle}")
    except Exception:
        pass

    # Method 2: product sitemap XML
    if not urls:
        try:
            r = requests.get(f"{base}/sitemap_products_1.xml", headers=headers, timeout=12)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'lxml-xml')
                locs = soup.find_all('loc')
                urls = [l.text.strip() for l in locs if '/products/' in l.text][:20]
        except Exception:
            pass

    # Method 3: scrape /collections/all for product links
    if not urls:
        try:
            r = requests.get(f"{base}/collections/all", headers=headers, timeout=12)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                links = soup.find_all('a', href=re.compile(r'/products/[^/?#"]+$'))
                seen = set()
                for link in links:
                    href = link['href']
                    full = urljoin(base, href)
                    if full not in seen:
                        seen.add(full)
                        urls.append(full)
                    if len(urls) >= 20:
                        break
        except Exception:
            pass

    # Method 4: scrape homepage for product links
    if not urls:
        try:
            r = requests.get(base, headers=headers, timeout=12)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                links = soup.find_all('a', href=re.compile(r'/products/[^/?#"]+'))
                seen = set()
                for link in links:
                    href = link['href']
                    full = urljoin(base, href)
                    if full not in seen:
                        seen.add(full)
                        urls.append(full)
                    if len(urls) >= 20:
                        break
        except Exception:
            pass

    return urls[:20]

def extract_schema(url):
    """Extract product data via Shopify JSON API or JSON-LD fallback."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/html, */*',
    }

    # Method 1: Shopify product JSON API (works even with JS-rendered themes)
    try:
        json_url = url.rstrip('/') + '.json'
        r = requests.get(json_url, headers=headers, timeout=12)
        if r.status_code == 200:
            data = r.json().get('product', {})
            if data:
                # Flatten Shopify product data into schema-like structure
                variants = data.get('variants', [])
                options = {o['name'].lower(): o.get('values', []) for o in data.get('options', [])}
                images = data.get('images', [])
                schema = {
                    '@type': 'Product',
                    'name': data.get('title', ''),
                    'description': data.get('body_html', ''),
                    'image': images[0].get('src') if images else None,
                    'brand': data.get('vendor', ''),
                    'sku': variants[0].get('sku', '') if variants else '',
                    'offers': {
                        'price': variants[0].get('price') if variants else None,
                        'availability': 'InStock' if any(v.get('available') for v in variants) else 'OutOfStock',
                    } if variants else None,
                }
                # Map option names to schema fields
                for opt_name, values in options.items():
                    if values:
                        if 'color' in opt_name or 'colour' in opt_name:
                            schema['color'] = values[0]
                        elif 'size' in opt_name:
                            schema['size'] = values[0]
                        elif 'material' in opt_name or 'fabric' in opt_name:
                            schema['material'] = values[0]
                # Check for GTIN/MPN in variants
                for v in variants:
                    if v.get('barcode'):
                        schema['gtin'] = v['barcode']
                        break
                    if v.get('sku'):
                        schema['mpn'] = v['sku']
                return schema
    except Exception:
        pass

    # Method 2: Parse JSON-LD from static HTML
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
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

    # Store-level summary
    avg_score = round(sum(p['score'] for p in results) / len(results))
    # Count how often each field is missing across all products
    missing_counts = {}
    for p in results:
        for f in p['missing']:
            missing_counts[f] = missing_counts.get(f, 0) + 1
    top_issues = sorted(missing_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Parse store domain for display
    from urllib.parse import urlparse
    parsed = urlparse(store_url if store_url.startswith('http') else 'https://' + store_url)
    store_domain = parsed.netloc or store_url

    return jsonify({
        'products': results,
        'summary': {
            'store': store_domain,
            'avg_score': avg_score,
            'total_products': len(results),
            'top_issues': [{'field': f, 'count': c} for f, c in top_issues],
        }
    })

if __name__ == '__main__':
    app.run(debug=True, port=5001)
