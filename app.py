from flask import Flask, request, jsonify, render_template_string, redirect, session
import requests
from bs4 import BeautifulSoup
import json
import re
import os
import hmac
import hashlib
import base64
import sqlite3
import tempfile
from urllib.parse import urljoin, urlparse, urlencode
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'aiready-secret-key-2025')

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
CRON_SECRET = os.environ.get('CRON_SECRET', 'aiready-cron-2025')
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
DB_PATH = os.environ.get('DB_PATH', os.path.join(tempfile.gettempdir(), 'aiready.db'))
USE_POSTGRES = bool(DATABASE_URL)

FREE_LIMIT = 5  # free AI actions per shop

def db_connect():
    if USE_POSTGRES:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(DB_PATH)

def adapt_sql(sql):
    if not USE_POSTGRES:
        return sql
    return (
        sql.replace('?', '%s')
        .replace("datetime('now')", 'CURRENT_TIMESTAMP')
        .replace('datetime("now")', 'CURRENT_TIMESTAMP')
    )

def db_execute(sql, params=(), fetchone=False, fetchall=False):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(adapt_sql(sql), params)
        result = None
        if fetchone:
            result = cur.fetchone()
        elif fetchall:
            result = cur.fetchall()
        conn.commit()
        return result
    finally:
        conn.close()

def init_db():
    if USE_POSTGRES:
        id_type = 'SERIAL PRIMARY KEY'
        now_type = 'TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP'
        scan_type = 'TIMESTAMPTZ'
    else:
        id_type = 'INTEGER PRIMARY KEY AUTOINCREMENT'
        now_type = "TEXT DEFAULT (datetime('now'))"
        scan_type = "TEXT DEFAULT ''"

    db_execute(f'''CREATE TABLE IF NOT EXISTS subscriptions (
        id {id_type},
        email TEXT NOT NULL,
        shop TEXT NOT NULL,
        last_score INTEGER DEFAULT 0,
        last_scanned {scan_type},
        created_at {now_type},
        UNIQUE(email, shop)
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS usage_counts (
        shop TEXT PRIMARY KEY,
        count INTEGER DEFAULT 0,
        updated_at {now_type}
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS paid_shops (
        shop TEXT PRIMARY KEY,
        paypal_txn_id TEXT,
        paid_at {now_type}
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS shop_tokens (
        shop TEXT PRIMARY KEY,
        access_token TEXT NOT NULL,
        scope TEXT DEFAULT '',
        updated_at {now_type}
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS unlock_requests (
        id {id_type},
        email TEXT,
        shop TEXT,
        created_at {now_type}
    )''')

def is_paid(shop):
    row = db_execute('SELECT 1 FROM paid_shops WHERE shop=?', (shop,), fetchone=True)
    return bool(row)

def get_usage(shop):
    row = db_execute('SELECT count FROM usage_counts WHERE shop=?', (shop,), fetchone=True)
    return row[0] if row else 0

def increment_usage(shop):
    if USE_POSTGRES:
        sql = '''INSERT INTO usage_counts (shop, count) VALUES (?, 1)
            ON CONFLICT(shop) DO UPDATE SET count=usage_counts.count+1, updated_at=CURRENT_TIMESTAMP
        '''
    else:
        sql = '''INSERT INTO usage_counts (shop, count) VALUES (?, 1)
            ON CONFLICT(shop) DO UPDATE SET count=count+1, updated_at=datetime('now')
        '''
    db_execute(sql, (shop,))

init_db()

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
SHOPIFY_CLIENT_ID = os.environ.get('SHOPIFY_CLIENT_ID', '')
SHOPIFY_CLIENT_SECRET = os.environ.get('SHOPIFY_CLIENT_SECRET', '')
SHOPIFY_SCOPES = 'read_products,write_products'
SHOPIFY_API_VERSION = os.environ.get('SHOPIFY_API_VERSION', '2026-04')

def normalize_shop(shop):
    shop = (shop or '').strip().lower()
    shop = re.sub(r'^https?://', '', shop).split('/')[0]
    if shop and not shop.endswith('.myshopify.com'):
        shop = f'{shop}.myshopify.com'
    return shop

def is_valid_shop(shop):
    return bool(re.fullmatch(r'[a-z0-9][a-z0-9-]*\.myshopify\.com', shop or ''))

def verify_shopify_hmac(args):
    incoming_hmac = args.get('hmac', '')
    if not incoming_hmac or not SHOPIFY_CLIENT_SECRET:
        return False
    pairs = []
    for key in sorted(k for k in args.keys() if k not in ('hmac', 'signature')):
        for value in args.getlist(key):
            pairs.append(f'{key}={value}')
    message = '&'.join(pairs)
    digest = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, incoming_hmac)

def save_shop_token(shop, access_token, scope=''):
    db_execute('''INSERT INTO shop_tokens (shop, access_token, scope, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(shop) DO UPDATE SET
            access_token=excluded.access_token,
            scope=excluded.scope,
            updated_at=datetime('now')
    ''', (shop, access_token, scope or ''))

def get_shop_token(shop):
    row = db_execute('SELECT access_token FROM shop_tokens WHERE shop=?', (shop,), fetchone=True)
    return row[0] if row else ''

def has_shop_token(shop):
    return bool(get_shop_token(shop))

def shopify_app_home_url(host):
    if not host or not SHOPIFY_CLIENT_ID:
        return ''
    try:
        padded = host + ('=' * (-len(host) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode('utf-8')).decode('utf-8')
    except Exception:
        return ''
    if not (
        decoded.startswith('admin.shopify.com/') or
        re.fullmatch(r'[a-z0-9][a-z0-9-]*\.myshopify\.com/admin', decoded)
    ):
        return ''
    return f'https://{decoded}/apps/{SHOPIFY_CLIENT_ID}/'

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
  <title>AiReady - AI Readiness Checker</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --page-bg: #F6F6F7;
      --surface: #FFFFFF;
      --border: #E4E5E7;
      --border-strong: #C9CCCF;
      --text: #202223;
      --text-sub: #6D7175;
      --text-hint: #8C9196;
      --green: #008060;
      --green-bg: #F1F8F5;
      --green-border: #AEE9D1;
      --yellow: #B98900;
      --yellow-bg: #FFF5EA;
      --yellow-border: #F1C84B;
      --red: #D72C0D;
      --red-bg: #FFF4F4;
      --red-border: #FD5749;
      --purple: #6241C3;
      --purple-bg: #F4F0FF;
      --purple-border: #B9A3F7;
      --radius: 8px;
      --shadow: 0 1px 0 rgba(0,0,0,0.05);
    }
    body { background: var(--page-bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif; min-height: 100vh; font-size: 14px; line-height: 1.5; }

    /* TOP BAR */
    .topbar { background: #1A1A1A; padding: 0 24px; display: flex; align-items: center; height: 56px; gap: 16px; }
    .topbar-logo { font-size: 16px; font-weight: 700; color: #fff; letter-spacing: -0.3px; }
    .topbar-logo span { color: #95BF47; }
    .topbar-badge { font-size: 11px; background: rgba(149,191,71,0.2); color: #95BF47; border: 1px solid rgba(149,191,71,0.4); padding: 3px 10px; border-radius: 20px; font-weight: 600; }

    /* PAGE */
    .page { max-width: 900px; margin: 0 auto; padding: 24px 24px 60px; }

    /* PAGE HEADER */
    .page-header { margin-bottom: 20px; }
    .page-title { font-size: 20px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
    .page-subtitle { font-size: 14px; color: var(--text-sub); }

    /* SCAN BAR */
    .scan-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; margin-bottom: 20px; box-shadow: var(--shadow); }
    .scan-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .scan-input { flex: 1; min-width: 240px; border: 1px solid var(--border-strong); border-radius: 6px; padding: 9px 14px; font-size: 14px; color: var(--text); outline: none; transition: border-color 0.15s; background: #fff; }
    .scan-input:focus { border-color: #458FFF; box-shadow: 0 0 0 2px rgba(69,143,255,0.2); }
    .scan-input::placeholder { color: var(--text-hint); }
    .btn-primary { background: #008060; color: #fff; border: none; padding: 9px 20px; border-radius: 6px; font-size: 14px; font-weight: 500; cursor: pointer; transition: background 0.15s; white-space: nowrap; }
    .btn-primary:hover { background: #006E52; }
    .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
    .btn-secondary { background: #fff; color: var(--text); border: 1px solid var(--border-strong); padding: 9px 16px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; transition: background 0.15s; white-space: nowrap; }
    .btn-secondary:hover { background: #F6F6F7; }
    .btn-plain { background: transparent; color: #458FFF; border: none; padding: 6px 12px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; }
    .btn-plain:hover { background: rgba(69,143,255,0.08); }

    /* SHOP BANNER */
    .shop-banner { background: var(--green-bg); border: 1px solid var(--green-border); border-radius: var(--radius); padding: 14px 20px; margin-bottom: 20px; display: none; align-items: center; gap: 16px; flex-wrap: wrap; }
    .shop-banner.visible { display: flex; }
    .shop-banner-text { flex: 1; font-size: 14px; color: #005E45; }
    .shop-banner-text strong { font-weight: 600; }

    /* METRIC CARDS */
    .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 20px; }
    .metric-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px 20px; box-shadow: var(--shadow); }
    .metric-value { font-size: 26px; font-weight: 700; line-height: 1; margin-bottom: 4px; }
    .metric-label { font-size: 13px; color: var(--text-sub); }
    .metric-green { color: var(--green); }
    .metric-yellow { color: var(--yellow); }
    .metric-red { color: var(--red); }

    /* CARDS */
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 16px; box-shadow: var(--shadow); overflow: hidden; }
    .card-header { padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
    .card-title { font-size: 14px; font-weight: 600; color: var(--text); }
    .card-body { padding: 20px; }

    /* PRIORITY FIXES */
    .fix-item { display: flex; gap: 14px; align-items: flex-start; padding: 12px 0; border-bottom: 1px solid var(--border); }
    .fix-item:last-child { border-bottom: none; padding-bottom: 0; }
    .fix-pts { background: var(--purple-bg); color: var(--purple); border: 1px solid var(--purple-border); border-radius: 20px; padding: 3px 10px; font-size: 12px; font-weight: 600; white-space: nowrap; }
    .fix-label { font-size: 14px; font-weight: 500; color: var(--text); margin-bottom: 2px; }
    .fix-hint { font-size: 13px; color: var(--text-sub); }

    /* ISSUES BAR */
    .issue-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--border); }
    .issue-row:last-child { border-bottom: none; }
    .issue-name { min-width: 200px; font-size: 13px; color: var(--text); }
    .issue-bar-wrap { flex: 1; background: #F1F1F1; border-radius: 4px; height: 6px; }
    .issue-bar { background: var(--red); border-radius: 4px; height: 6px; }
    .issue-count { font-size: 12px; color: var(--text-sub); min-width: 80px; text-align: right; }

    /* PRODUCT TABLE */
    .product-table { width: 100%; border-collapse: collapse; }
    .product-table th { font-size: 12px; font-weight: 600; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.5px; padding: 10px 16px; text-align: left; border-bottom: 1px solid var(--border); background: #FAFAFA; }
    .product-table td { padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .product-row:last-child td { border-bottom: none; }
    .product-row:hover td { background: #FAFAFA; }
    .product-row.expanded td { background: #FAFAFA; }
    .product-name-cell { font-size: 14px; font-weight: 500; color: var(--text); cursor: pointer; }
    .product-url-cell { font-size: 12px; color: var(--text-hint); margin-top: 2px; }
    .expand-icon { display: inline-block; transition: transform 0.2s; margin-left: 6px; color: var(--text-hint); font-size: 12px; }
    .product-row.expanded .expand-icon { transform: rotate(90deg); }

    /* SCORE BADGE */
    .score-pill { display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 13px; font-weight: 600; }
    .score-high { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
    .score-mid  { background: var(--yellow-bg); color: var(--yellow); border: 1px solid var(--yellow-border); }
    .score-low  { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }

    /* FIELD CHIPS */
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .chip { font-size: 12px; padding: 3px 10px; border-radius: 20px; }
    .chip-ok { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
    .chip-miss { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); cursor: pointer; }
    .chip-miss:hover { opacity: 0.8; }

    /* DETAIL ROW */
    .detail-row { display: none; }
    .detail-row.visible { display: table-row; }
    .detail-cell { padding: 16px !important; background: #FAFAFA !important; border-bottom: 1px solid var(--border) !important; }
    .detail-actions { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }

    /* AI RESULT */
    .ai-result { margin-top: 12px; border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
    .ai-result-header { background: #FAFAFA; padding: 10px 14px; font-size: 12px; font-weight: 600; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.5px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); }
    .ai-result-body { padding: 14px; font-size: 14px; color: var(--text); line-height: 1.7; }
    .ai-score-row { display: flex; gap: 20px; margin-bottom: 12px; flex-wrap: wrap; }
    .ai-score-num { font-size: 24px; font-weight: 700; }
    .ai-issues { margin-bottom: 10px; }
    .ai-issue { font-size: 13px; color: var(--red); margin-bottom: 3px; }
    .ai-sugg { font-size: 13px; color: var(--text-sub); margin-bottom: 3px; }

    /* SPINNER */
    .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--border); border-top-color: var(--green); border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 8px; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* LOADING STATE */
    .loading-state { text-align: center; padding: 60px 20px; color: var(--text-sub); }
    .loading-state p { margin-top: 12px; font-size: 15px; }
    .loading-spinner { width: 32px; height: 32px; border: 3px solid var(--border); border-top-color: var(--green); border-radius: 50%; animation: spin 0.7s linear infinite; margin: 0 auto; }

    /* ERROR */
    .error-banner { background: var(--red-bg); border: 1px solid var(--red-border); border-radius: var(--radius); padding: 14px 20px; color: var(--red); font-size: 14px; margin-bottom: 16px; }

    /* EMAIL CAPTURE */
    .email-card { background: #1A1A1A; border-radius: var(--radius); padding: 28px; text-align: center; margin-top: 24px; }
    .email-card h3 { color: #fff; font-size: 16px; font-weight: 600; margin-bottom: 6px; }
    .email-card p { color: #999; font-size: 13px; margin-bottom: 20px; }
    .email-row { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
    .email-input { border: 1px solid #444; background: #2A2A2A; color: #fff; padding: 9px 14px; border-radius: 6px; font-size: 14px; width: 260px; outline: none; }
    .email-input::placeholder { color: #666; }

    /* USAGE BADGE */
    .usage-badge { display:inline-flex; align-items:center; gap:6px; background:var(--yellow-bg); border:1px solid var(--yellow-border); color:var(--yellow); border-radius:20px; padding:4px 12px; font-size:12px; font-weight:600; }
    .usage-badge.paid { background:var(--green-bg); border-color:var(--green-border); color:var(--green); }

    /* UPGRADE MODAL */
    .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center; }
    .modal-overlay.visible { display:flex; }
    .modal-box { background:#fff; border-radius:12px; padding:32px; max-width:420px; width:90%; text-align:center; box-shadow:0 8px 40px rgba(0,0,0,0.18); }
    .modal-icon { font-size:36px; margin-bottom:12px; }
    .modal-title { font-size:20px; font-weight:700; color:var(--text); margin-bottom:8px; }
    .modal-sub { font-size:14px; color:var(--text-sub); margin-bottom:24px; line-height:1.6; }
    .modal-price { font-size:32px; font-weight:800; color:var(--green); margin-bottom:4px; }
    .modal-price-sub { font-size:13px; color:var(--text-sub); margin-bottom:24px; }
    .modal-features { text-align:left; background:var(--green-bg); border:1px solid var(--green-border); border-radius:8px; padding:14px 18px; margin-bottom:24px; }
    .modal-feature { font-size:13px; color:#005E45; padding:3px 0; }
    .modal-close { margin-top:14px; font-size:13px; color:var(--text-hint); cursor:pointer; }
    .modal-close:hover { color:var(--text-sub); }

    @media(max-width: 640px) {
      .metrics { grid-template-columns: 1fr; }
      .issue-name { min-width: 120px; }
      .page { padding: 16px 16px 40px; }
    }
  </style>
</head>
<body>

<div class="topbar">
  <a href="/" style="text-decoration:none;"><div class="topbar-logo">Ai<span>Ready</span></div></a>
  <div class="topbar-badge">AI Readiness Checker</div>
</div>

<div class="page">

  <div class="page-header">
    <div class="page-title">AI Readiness Scanner</div>
    <div class="page-subtitle">Check how visible your Shopify products are to AI engines like ChatGPT, Perplexity, and Gemini.</div>
  </div>

  <div id="shopBanner" class="shop-banner">
    <div class="shop-banner-text">Store detected: <strong id="shopLabel"></strong></div>
    <button class="btn-primary" onclick="runShopScan()">Scan My Store</button>
  </div>

  <div class="scan-card">
    <div class="scan-row">
      <input type="text" class="scan-input" id="storeUrl" placeholder="yourstore.myshopify.com or yourstore.com" />
      <button class="btn-primary" id="scanBtn" onclick="runScan()">Scan Store</button>
    </div>
  </div>

  <div id="results"></div>

</div><!-- end .page -->

<script>
// Detect shop or prefilled URL from params
(function() {
  var params = new URLSearchParams(window.location.search);
  var shop = params.get('shop');
  var urlParam = params.get('url');
  if (shop) {
    var banner = document.getElementById('shopBanner');
    var label = document.getElementById('shopLabel');
    if (banner) banner.classList.add('visible');
    if (label) label.textContent = shop;
    document.getElementById('storeUrl').value = shop;
  } else if (urlParam) {
    document.getElementById('storeUrl').value = urlParam;
  }
})();

function runShopScan() {
  runScan();
}

const FIX_HINTS = {
  'Brand': 'Shopify Admin -> Products -> [product] -> Vendor field',
  'Aggregate Rating': 'Install a reviews app (e.g. Judge.me, Loox) and enable structured data',
  'GTIN / Barcode': 'Shopify Admin -> Products -> [variant] -> Barcode field (enter ISBN, UPC, GTIN, etc.)',
  'MPN (Manufacturer Part No)': 'Add metafield: namespace=product, key=mpn, or use SKU field',
  'Material': 'Add a product option named "Material" or add a metafield',
  'Color': 'Add a product option named "Color" or "Colour"',
  'Size': 'Add a product option named "Size"',
  'Availability Status': 'Enable inventory tracking in Shopify Admin -> Products -> [variant]',
  'Description': 'Shopify Admin -> Products -> [product] -> Description (add detailed text)',
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
  results.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Scanning up to 20 products - this may take 20-30 seconds...</p></div>';
  try {
    const res = await fetch('/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (data.error) {
      results.innerHTML = `<div class="error-banner">Error: ${escapeHtml(data.error)}</div>`;
    } else {
      lastData = data;
      renderResults(data);
    }
  } catch(e) {
    results.innerHTML = '<div class="error-banner">Could not connect to scanner. Make sure the store URL is correct.</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Scan Store';
}

function scoreClass(s) {
  if (s >= 70) return 'score-high';
  if (s >= 40) return 'score-mid';
  return 'score-low';
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function jsArg(value) {
  return escapeHtml(JSON.stringify(value ?? ''));
}

function toggleFix(el) {
  el.classList.toggle('expanded');
}

function renderResults(data) {
  const results = document.getElementById('results');
  const s = data.summary;
  const totalIssues = data.products.reduce((a,p) => a + p.missing.length, 0);
  const maxCount = s.top_issues.length ? s.top_issues[0].count : 1;
  const scoreCol = s.avg_score >= 70 ? 'metric-green' : s.avg_score >= 40 ? 'metric-yellow' : 'metric-red';

  // Priority fixes
  const weightMap = {};
  for (const p of data.products) {
    for (const f of p.missing) {
      if (!weightMap[f.label] || weightMap[f.label].weight < f.weight) weightMap[f.label] = f;
    }
  }
  const priorityFixes = Object.values(weightMap).sort((a,b) => b.weight - a.weight).slice(0,3);

  // Usage badge (fetch async, inject after render)
  fetch('/api/usage?shop=' + encodeURIComponent(s.store))
    .then(r => r.json())
    .then(u => {
      const el = document.getElementById('usageBadge');
      if (!el) return;
      if (u.paid) {
        el.innerHTML = '<span class="usage-badge paid">&#10003; Unlimited plan</span>';
      } else {
        const rem = u.remaining;
        el.innerHTML = `<span class="usage-badge">${rem} free action${rem===1?'':'s'} remaining &mdash; <a href="#" onclick="showUpgradeModal(${jsArg(s.store)});return false;" style="color:var(--yellow);text-decoration:underline;">Upgrade $9</a></span>`;
      }
    }).catch(() => {});

  let html = `<div id="usageBadge" style="margin-bottom:12px;"></div>
  <div class="metrics">
    <div class="metric-card">
      <div class="metric-value">${s.total_products}</div>
      <div class="metric-label">Products scanned</div>
    </div>
    <div class="metric-card">
      <div class="metric-value ${scoreCol}">${s.avg_score}<span style="font-size:16px;font-weight:400;color:var(--text-sub)">/100</span></div>
      <div class="metric-label">Average AI Readiness Score</div>
    </div>
    <div class="metric-card">
      <div class="metric-value metric-red">${totalIssues}</div>
      <div class="metric-label">Total missing fields</div>
    </div>
  </div>`;

  // Top issues card
  if (s.top_issues.length) {
    html += `<div class="card">
      <div class="card-header">
        <div class="card-title">Most common issues across your store</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          ${s.has_token ? `<button class="btn-primary" onclick="bulkFix(this)">Fix All Products</button>` : ''}
          <button class="btn-secondary" onclick="shareScore(${s.avg_score},${jsArg(s.store)})">Share Score</button>
          <button class="btn-secondary" onclick="downloadPDF()">Download PDF</button>
        </div>
      </div>
      <div class="card-body">`;
    for (const issue of s.top_issues) {
      const pct = Math.round((issue.count / maxCount) * 100);
      html += `<div class="issue-row">
        <span class="issue-name">${escapeHtml(issue.field)}</span>
        <div class="issue-bar-wrap"><div class="issue-bar" style="width:${pct}%"></div></div>
        <span class="issue-count">${issue.count} of ${s.total_products} products</span>
      </div>`;
    }
    html += `</div>
      <div id="bulkProgress" style="display:none;padding:12px 20px;border-top:1px solid var(--border);">
        <div style="font-size:13px;color:var(--text-sub);margin-bottom:8px;">Generating and saving descriptions...</div>
        <div style="background:#F1F1F1;border-radius:4px;height:8px;overflow:hidden;">
          <div id="bulkBar" style="background:var(--green);height:8px;border-radius:4px;width:0%;transition:width 0.3s;"></div>
        </div>
      </div>
    </div>`;
  }

  // Priority fixes card
  if (priorityFixes.length) {
    html += `<div class="card">
      <div class="card-header"><div class="card-title">Priority fixes - biggest score impact</div></div>
      <div class="card-body">`;
    for (const f of priorityFixes) {
      const hint = FIX_HINTS[f.label] || 'Add this field to improve AI discoverability';
      html += `<div class="fix-item">
        <span class="fix-pts">+${Math.round(f.weight)} pts</span>
        <div><div class="fix-label">${escapeHtml(f.label)}</div><div class="fix-hint">${escapeHtml(hint)}</div></div>
      </div>`;
    }
    html += `</div></div>`;
  }

  // Products table card
  html += `<div class="card">
    <div class="card-header"><div class="card-title">Product breakdown</div></div>
    <table class="product-table">
      <thead><tr>
        <th>Product</th>
        <th>Score</th>
        <th>Fields</th>
      </tr></thead>
      <tbody>`;

  data.products.forEach((p, idx) => {
    const sc = scoreClass(p.score);
    const passCount = p.present.length;
    const failCount = p.missing.length;
    const sanitize = s => String(s||'').replace(/['"><]/g,' ').slice(0,200);
    const en = sanitize(p.name).slice(0,60);
    const eb = sanitize(p.brand).slice(0,40);
    const ed = sanitize(p.description);
    const ml = p.missing.map(f => f.label.replace(/['"`]/g,'')).join('|');

    html += `<tr class="product-row" onclick="toggleRow(${idx})">
      <td>
        <div class="product-name-cell">${escapeHtml(p.name)}<span class="expand-icon">&#9654;</span></div>
        <div class="product-url-cell">${escapeHtml(p.url)}</div>
      </td>
      <td><span class="score-pill ${sc}">${p.score}/100</span></td>
      <td><span style="color:var(--green);font-size:13px;font-weight:500;">${passCount} passed</span> &nbsp; <span style="color:var(--red);font-size:13px;">${failCount} missing</span></td>
    </tr>
    <tr class="detail-row" id="detail-${idx}">
      <td colspan="3" class="detail-cell">
        <div class="chips">`;
    for (const f of p.present) {
      html += `<span class="chip chip-ok">${escapeHtml(f)}</span>`;
    }
    for (const f of p.missing) {
      const hint = FIX_HINTS[f.label] || 'Add this field to improve AI discoverability';
      html += `<span class="chip chip-miss" title="${escapeHtml(hint)}">${escapeHtml(f.label)} - missing</span>`;
    }
    const pid = p.product_id || '';
    const pvendor = (p.vendor||'').split("'").join("");
    const hasToken = data.summary.has_token && pid;
    html += `</div>
        <div class="detail-actions">
          <button class="btn-secondary" onclick="event.stopPropagation();analyzeContent(this,${jsArg(en)},${jsArg(eb)},${jsArg(ed)})">Analyze Content</button>
          <button class="btn-primary" onclick="event.stopPropagation();generateDesc(this,${jsArg(en)},${jsArg(eb)},${jsArg(ed)},${jsArg(ml)})">Generate AI Description</button>
          ${hasToken && !p.vendor ? `<button class="btn-secondary" onclick="event.stopPropagation();autoFillBrand(this,${jsArg(pid)},${jsArg(en)},${jsArg(s.store)})">Auto-fill Brand</button>` : ''}
        </div>
        <div class="analyze-result" style="display:none;margin-top:12px;"></div>
        <div class="generate-result" style="display:none;margin-top:12px;"></div>
      </td>
    </tr>`;
  });

  html += `</tbody></table></div>`;

  // Email capture
  const shopDomain = s.store || '';
  html += `<div class="email-card">
    <h3>Get your weekly AI Readiness Report</h3>
    <p>We scan your store every week and email you the score + what to fix.</p>
    <div class="email-row">
      <input type="email" id="subEmail" class="email-input" placeholder="your@email.com" />
      <button class="btn-primary" onclick="subscribe(${jsArg(shopDomain)})">Get Weekly Report</button>
    </div>
    <div id="subMsg" style="margin-top:10px;font-size:13px;color:#95BF47;display:none;">Subscribed! You'll get your first report within a week.</div>
  </div>`;

  results.innerHTML = html;
}

function toggleRow(idx) {
  const row = document.getElementById('detail-' + idx);
  const productRows = document.querySelectorAll('.product-row');
  if (row.classList.contains('visible')) {
    row.classList.remove('visible');
    productRows[idx].classList.remove('expanded');
  } else {
    row.classList.add('visible');
    productRows[idx].classList.add('expanded');
  }
}

function shareScore(score, store) {
  const text = `My Shopify store scored ${score}/100 on AI Readiness - meaning AI engines like ChatGPT and Perplexity may not be recommending my products. Check your store free: https://aiready-checker.onrender.com`;
  navigator.clipboard.writeText(text).then(() => {
    alert('Score text copied! Paste it anywhere to share.');
  }).catch(() => {
    prompt('Copy this:', text);
  });
}

async function analyzeContent(btn, name, brand, description) {
  const cell = btn.closest('.detail-cell');
  const resultBox = cell.querySelector('.analyze-result');
  btn.disabled = true;
  btn.textContent = 'Analyzing...';
  resultBox.style.display = 'block';
  resultBox.innerHTML = '<span class="spinner"></span> Running GEO content analysis...';

  try {
    const res = await fetch('/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, brand, description})
    });
    const data = await res.json();
    if (data.error) {
      resultBox.innerHTML = `<div class="error-banner">${escapeHtml(data.error)}</div>`;
    } else {
      const sc = data.content_score >= 70 ? 'score-high' : data.content_score >= 40 ? 'score-mid' : 'score-low';
      let html = `<div class="ai-result"><div class="ai-result-header"><span>Content GEO Analysis</span></div><div class="ai-result-body">
        <div class="ai-score-row">
          <div><div class="ai-score-num"><span class="score-pill ${sc}">${data.content_score}/100</span></div></div>
          <div style="font-size:13px;color:var(--text-sub);align-self:center;">${data.word_count || 0} words in current description</div>
        </div>
        <div style="font-size:12px;color:var(--muted);">${data.word_count || 0} words in description</div>
      </div>`;
      if (data.issues && data.issues.length) {
        html += `<div class="ai-issues">`;
        for (const issue of data.issues) {
          html += `<div class="ai-issue">&#9888; ${escapeHtml(issue)}</div>`;
        }
        html += `</div>`;
      }
      if (data.suggestions && data.suggestions.length) {
        html += `<div style="font-size:12px;font-weight:600;color:var(--text-sub);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Suggestions</div>`;
        for (const sg of data.suggestions) {
          html += `<div class="ai-sugg">&#8594; ${escapeHtml(sg)}</div>`;
        }
      }
      html += `</div></div>`;
      resultBox.innerHTML = html;
    }
  } catch(e) {
    resultBox.innerHTML = `<div class="error-banner">Could not connect to analysis service.</div>`;
  }
  btn.disabled = false;
  btn.textContent = 'Analyze Content';
}

async function generateDesc(btn, name, brand, description, missingLabels) {
  const cell = btn.closest('.detail-cell');
  const resultBox = cell.querySelector('.generate-result');
  btn.disabled = true;
  btn.textContent = 'Generating...';
  resultBox.style.display = 'block';
  resultBox.innerHTML = '<span class="spinner"></span> Writing optimized description...';

  const missing = missingLabels ? missingLabels.split('|').filter(Boolean) : [];

  const shop = lastData && lastData.summary ? lastData.summary.store : '';
  try {
    const res = await fetch('/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, brand, description, missing, shop})
    });
    const data = await res.json();
    if (data.error === 'LIMIT_REACHED') {
      resultBox.style.display = 'none';
      showUpgradeModal(shop);
      btn.disabled = false; btn.textContent = 'Generate AI Description'; return;
    }
    if (data.error) {
      resultBox.innerHTML = `<div class="error-banner">${escapeHtml(data.error)}</div>`;
    } else {
      // Get product_id and shop from the row context
      const row = btn.closest('tr');
      const idx = row ? row.id.replace('detail-','') : '';
      const p = lastData && lastData.products ? lastData.products[parseInt(idx)] : null;
      const productId = p ? p.product_id : '';
      const shop = lastData && lastData.summary ? lastData.summary.store : '';
      const hasToken = lastData && lastData.summary && lastData.summary.has_token;

      const saveBtn = (hasToken && productId)
        ? `<button class="btn-primary" style="font-size:12px;padding:5px 14px;" onclick="saveToShopify(this,${jsArg(productId)},${jsArg(shop)},this.closest('.ai-result').querySelector('.ai-result-body').textContent)">Save to Shopify</button>`
        : '';
      resultBox.innerHTML = `
        <div class="ai-result">
          <div class="ai-result-header">
            <span>AI-Optimized Description</span>
            <div style="display:flex;gap:8px;">
              <button class="btn-secondary" style="font-size:12px;padding:5px 12px;" onclick="copyText(this,this.closest('.ai-result').querySelector('.ai-result-body').textContent)">Copy</button>
              ${saveBtn}
            </div>
          </div>
          <div class="ai-result-body">${escapeHtml(data.description)}</div>
        </div>`;
    }
  } catch(e) {
    resultBox.innerHTML = `<div class="error-banner">Could not connect to generation service.</div>`;
  }
  btn.disabled = false;
  btn.textContent = 'Generate AI Description';
}

async function saveToShopify(btn, productId, shop, description) {
  if (!productId || !shop) { alert('Store not connected. Install the app via Shopify to enable saving.'); return; }
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const res = await fetch('/api/update_product', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({product_id: productId, shop, description})
    });
    const data = await res.json();
    if (data.error === 'LIMIT_REACHED') {
      btn.disabled = false; btn.textContent = 'Save to Shopify';
      showUpgradeModal(shop); return;
    }
    if (data.success) {
      btn.textContent = 'Saved!';
      btn.style.background = 'var(--green)';
    } else {
      btn.textContent = 'Failed';
      alert(data.error || 'Could not save to Shopify.');
    }
  } catch(e) {
    btn.textContent = 'Error';
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Save to Shopify'; btn.style.background = ''; }, 3000);
}

async function autoFillBrand(btn, productId, productName, shop) {
  btn.disabled = true;
  btn.textContent = 'Filling...';
  // Extract brand from product name (first word or phrase before dash/comma)
  const brandGuess = productName.split('-')[0].split(',')[0].trim().split(' ').slice(0,2).join(' ');
  try {
    const res = await fetch('/api/update_vendor', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({product_id: productId, shop, vendor: brandGuess})
    });
    const data = await res.json();
    if (data.success) {
      btn.textContent = 'Done - ' + brandGuess;
      btn.style.color = 'var(--green)';
    } else {
      btn.textContent = 'Failed';
    }
  } catch(e) {
    btn.textContent = 'Error';
  }
}

async function bulkFix(btn) {
  if (!lastData || !lastData.summary.has_token) {
    alert('Connect your Shopify store first by installing the app.');
    return;
  }
  const shop = lastData.summary.store;
  const products = lastData.products.filter(p => p.product_id && p.missing.length > 0);
  if (!products.length) { alert('No products need fixing.'); return; }

  btn.disabled = true;
  const total = products.length;
  let done = 0;
  let saved = 0;
  let failed = 0;

  const progressEl = document.getElementById('bulkProgress');
  if (progressEl) progressEl.style.display = 'block';

  for (const p of products) {
    const progressBar = document.getElementById('bulkBar');
    if (progressBar) progressBar.style.width = Math.round((done/total)*100) + '%';

    // Generate description
    try {
      const genRes = await fetch('/generate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          name: p.name,
          brand: p.brand || '',
          description: p.description || '',
          missing: p.missing.map(f => f.label),
          shop
        })
      });
      const genData = await genRes.json();
      if (genData.error === 'LIMIT_REACHED') {
        showUpgradeModal(shop);
        break;
      }
      if (genData.description && p.product_id) {
        const saveRes = await fetch('/api/update_product', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({product_id: p.product_id, shop, description: genData.description})
        });
        const saveData = await saveRes.json();
        if (saveData.error === 'LIMIT_REACHED') {
          failed++;
          showUpgradeModal(shop);
          break;
        } else if (saveData.success) {
          saved++;
        } else {
          failed++;
        }
      } else {
        failed++;
      }
    } catch(e) {
      failed++;
    }
    done++;
  }

  if (progressEl) progressEl.style.display = 'none';
  btn.disabled = false;
  btn.textContent = 'Fix All Products';
  alert(`Done. Saved ${saved} products to Shopify${failed ? `; ${failed} failed` : ''}.`);
}

function showUpgradeModal(shop) {
  document.getElementById('paypalShop').value = shop || '';
  document.getElementById('modalStep1').style.display = 'block';
  document.getElementById('modalStep2').style.display = 'none';
  document.getElementById('upgradeModal').classList.add('visible');
}
function closeUpgradeModal() {
  document.getElementById('upgradeModal').classList.remove('visible');
}
function showPaidStep() {
  setTimeout(() => {
    document.getElementById('modalStep1').style.display = 'none';
    document.getElementById('modalStep2').style.display = 'block';
  }, 1500);
}
async function submitUnlockRequest() {
  const email = document.getElementById('unlockEmail').value.trim();
  const shop = document.getElementById('paypalShop').value;
  const msg = document.getElementById('unlockMsg');
  if (!email) { alert('Please enter your PayPal email.'); return; }
  try {
    const res = await fetch('/request-unlock', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, shop})
    });
    const data = await res.json();
    msg.style.display = 'block';
    msg.style.color = 'var(--green)';
    msg.textContent = 'Request received! Your store will be unlocked within a few hours.';
  } catch(e) {
    msg.style.display = 'block';
    msg.style.color = 'var(--red)';
    msg.textContent = 'Error sending request. Please email us directly.';
  }
}
document.addEventListener('click', e => {
  if (e.target.id === 'upgradeModal') closeUpgradeModal();
});

async function subscribe(shop) {
  const email = document.getElementById('subEmail').value.trim();
  if (!email) { alert('Please enter your email.'); return; }
  if (!shop) { alert('Please scan a store first.'); return; }
  try {
    const res = await fetch('/subscribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, shop})
    });
    const data = await res.json();
    if (data.success) {
      document.getElementById('subMsg').style.display = 'block';
      document.getElementById('subEmail').value = '';
    } else {
      alert(data.error || 'Subscription failed.');
    }
  } catch(e) {
    alert('Could not connect. Please try again.');
  }
}

function copyText(btn, text) {
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 2000);
  }).catch(() => prompt('Copy this:', text));
}

function downloadPDF() {
  if (!lastData) return;
  const { jsPDF } = window.jspdf;
  const doc = new jsPDF();
  const s = lastData.summary;
  const date = new Date().toLocaleDateString();

  // White PDF
  doc.setFillColor(255, 255, 255);
  doc.rect(0, 0, 210, 297, 'F');

  // Header bar
  doc.setFillColor(26, 26, 26);
  doc.rect(0, 0, 210, 20, 'F');
  doc.setTextColor(149, 191, 71);
  doc.setFontSize(13);
  doc.setFont('helvetica', 'bold');
  doc.text('AiReady', 14, 13);
  doc.setTextColor(180, 180, 180);
  doc.setFontSize(9);
  doc.setFont('helvetica', 'normal');
  doc.text('AI Readiness Report', 40, 13);
  doc.setTextColor(150, 150, 150);
  doc.text(date, 170, 13);

  doc.setTextColor(32, 34, 35);
  doc.setFontSize(18);
  doc.setFont('helvetica', 'bold');
  doc.text(s.store, 14, 34);

  const scoreColor = s.avg_score >= 70 ? [0,128,96] : s.avg_score >= 40 ? [185,137,0] : [215,44,13];
  doc.setTextColor(...scoreColor);
  doc.setFontSize(32);
  doc.text(`${s.avg_score}/100`, 14, 50);
  doc.setTextColor(109, 113, 117);
  doc.setFontSize(10);
  doc.setFont('helvetica', 'normal');
  doc.text(`Average AI Readiness Score  |  ${s.total_products} products scanned`, 14, 58);

  doc.setDrawColor(228, 229, 231);
  doc.line(14, 64, 196, 64);

  doc.setTextColor(32, 34, 35);
  doc.setFontSize(12);
  doc.setFont('helvetica', 'bold');
  doc.text('Top Issues', 14, 74);

  let y = 82;
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(10);
  for (const issue of s.top_issues) {
    doc.setTextColor(215, 44, 13);
    doc.text('- ' + issue.field, 16, y);
    doc.setTextColor(109, 113, 117);
    doc.text(`${issue.count} of ${s.total_products} products`, 110, y);
    y += 7;
  }

  y += 6;
  doc.setDrawColor(228, 229, 231);
  doc.line(14, y, 196, y);
  y += 8;

  doc.setTextColor(32, 34, 35);
  doc.setFontSize(12);
  doc.setFont('helvetica', 'bold');
  doc.text('Product Breakdown', 14, y);
  y += 8;

  for (const p of lastData.products) {
    if (y > 265) { doc.addPage(); doc.setFillColor(255,255,255); doc.rect(0,0,210,297,'F'); y = 20; }
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(10);
    const scoreCol = p.score >= 70 ? [0,128,96] : p.score >= 40 ? [185,137,0] : [215,44,13];
    doc.setTextColor(...scoreCol);
    doc.text(`${p.score}/100`, 14, y);
    doc.setTextColor(32, 34, 35);
    doc.text(p.name.length > 62 ? p.name.slice(0,59)+'...' : p.name, 38, y);
    y += 6;
    if (p.missing.length) {
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(9);
      doc.setTextColor(109, 113, 117);
      const missingText = 'Missing: ' + p.missing.map(f => f.label).join(', ');
      const lines = doc.splitTextToSize(missingText, 172);
      doc.text(lines, 16, y);
      y += lines.length * 5 + 3;
    } else {
      y += 3;
    }
  }

  doc.setTextColor(140, 145, 150);
  doc.setFontSize(8);
  doc.text('Generated by AiReady - aiready-checker.onrender.com', 20, 290);

  doc.save('aiready-report-' + s.store + '-' + date.split('/').join('-') + '.pdf');
}

document.getElementById('storeUrl').addEventListener('keydown', e => {
  if (e.key === 'Enter') runScan();
});
window.addEventListener('load', function() {
  var u = new URLSearchParams(window.location.search).get('url');
  if (u) { document.getElementById('storeUrl').value = u; runScan(); }
});
</script>

<!-- UPGRADE MODAL -->
<div class="modal-overlay" id="upgradeModal">
  <div class="modal-box">
    <div class="modal-icon">&#128274;</div>
    <div class="modal-title">You've used your 5 free actions</div>
    <div class="modal-sub">Upgrade once to unlock unlimited AI fixes, descriptions, and saves for your store.</div>
    <div class="modal-price">$9</div>
    <div class="modal-price-sub">one-time payment &mdash; unlimited forever</div>
    <div class="modal-features">
      <div class="modal-feature">&#10003; &nbsp; Unlimited AI description generation</div>
      <div class="modal-feature">&#10003; &nbsp; Save directly to Shopify</div>
      <div class="modal-feature">&#10003; &nbsp; Bulk fix all products at once</div>
      <div class="modal-feature">&#10003; &nbsp; Weekly score reports via email</div>
    </div>
    <div id="modalStep1">
      <button class="btn-primary" style="width:100%;padding:14px;font-size:15px;" onclick="window.open('https://paypal.me/MingkunYang/9','_blank');showPaidStep();">Pay $9 via PayPal &rarr;</button>
    </div>
    <div id="modalStep2" style="display:none;margin-top:16px;">
      <p style="font-size:13px;color:var(--text-sub);margin-bottom:10px;">Enter your PayPal email so we can verify and unlock your store:</p>
      <input type="email" id="unlockEmail" class="scan-input" placeholder="your@paypal.email" style="margin-bottom:8px;" />
      <button class="btn-primary" style="width:100%;padding:12px;" onclick="submitUnlockRequest()">Confirm &amp; Unlock</button>
      <div id="unlockMsg" style="margin-top:10px;font-size:13px;display:none;"></div>
    </div>
    <input type="hidden" id="paypalShop" value="">
    <div class="modal-close" onclick="closeUpgradeModal()">Maybe later</div>
  </div>
</div>

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

def has_value(value):
    if value is None:
        return False
    if isinstance(value, (dict, list)):
        return bool(value)
    return bool(str(value).strip())

def find_product_schema(data):
    if isinstance(data, list):
        for item in data:
            found = find_product_schema(item)
            if found:
                return found
        return None
    if not isinstance(data, dict):
        return None

    schema_type = data.get('@type')
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    if any(str(t).lower() == 'product' for t in types if t):
        return data

    for key in ('@graph', 'mainEntity', 'itemListElement', 'item'):
        found = find_product_schema(data.get(key))
        if found:
            return found
    return None

def normalize_schema_value(value):
    if isinstance(value, list):
        return normalize_schema_value(value[0]) if value else ''
    if isinstance(value, dict):
        for key in ('name', 'url', 'src', '@id'):
            if has_value(value.get(key)):
                return value[key]
    return value

def normalize_product_schema(schema):
    if not schema:
        return None
    schema = dict(schema)
    for key in ('brand', 'image', 'color', 'size', 'material'):
        if key in schema:
            schema[key] = normalize_schema_value(schema[key])
    return schema

def merge_product_schema(primary, secondary):
    merged = dict(primary or {})
    for key, value in (secondary or {}).items():
        if key == '@type':
            continue
        if has_value(value) and not has_value(merged.get(key)):
            merged[key] = value

    if isinstance((primary or {}).get('offers'), dict) and isinstance((secondary or {}).get('offers'), dict):
        offers = dict(secondary['offers'])
        offers.update({k: v for k, v in primary['offers'].items() if has_value(v)})
        merged['offers'] = offers

    merged['@type'] = 'Product'
    return normalize_product_schema(merged)

def extract_schema(url):
    """Extract product data from Shopify JSON and page JSON-LD."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/html, */*',
    }
    shopify_schema = None
    jsonld_schema = None

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
                shopify_schema = {
                    '@type': 'Product',
                    '_shopify_id': str(data.get('id', '')),
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
                            shopify_schema['color'] = values[0]
                        elif 'size' in opt_name:
                            shopify_schema['size'] = values[0]
                        elif 'material' in opt_name or 'fabric' in opt_name:
                            shopify_schema['material'] = values[0]
                # Check for GTIN/MPN in variants
                for v in variants:
                    if not shopify_schema.get('gtin') and v.get('barcode'):
                        shopify_schema['gtin'] = v['barcode']
                    if not shopify_schema.get('mpn') and v.get('sku'):
                        shopify_schema['mpn'] = v['sku']
                    if shopify_schema.get('gtin') and shopify_schema.get('mpn'):
                        break
    except Exception:
        pass

    # Method 2: Parse JSON-LD from static HTML and merge it with Shopify JSON.
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    data = json.loads(script.string or '{}')
                    jsonld_schema = find_product_schema(data)
                    if jsonld_schema:
                        break
                except Exception:
                    continue
    except Exception:
        pass

    if shopify_schema or jsonld_schema:
        return merge_product_schema(shopify_schema, jsonld_schema)
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
    missing = []  # list of {label, weight}
    for field, meta in REQUIRED_FIELDS.items():
        if check_field(schema, field):
            earned += meta['weight']
            present.append(meta['label'])
        else:
            missing.append({'label': meta['label'], 'weight': meta['weight']})
    # Sort missing by weight descending (biggest impact first)
    missing.sort(key=lambda x: x['weight'], reverse=True)
    score = round((earned / total_weight) * 100)
    return score, present, missing

LANDING_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AiReady - Is Your Shopify Store Visible to AI?</title>
  <meta name="description" content="Free tool to check how visible your Shopify products are to ChatGPT, Perplexity, and Gemini."/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --green: #008060; --green-dark: #006E52; --green-bg: #F1F8F5; --green-border: #AEE9D1;
      --text: #1A1A1A; --text-sub: #555; --border: #E4E5E7; --page-bg: #FAFAFA;
      --yellow: #F5A623; --red: #D72C0D; --yellow-bg: #FFF5EA; --yellow-border: #F1C84B;
    }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif; color: var(--text); background: #fff; line-height: 1.6; }
    nav { display:flex; align-items:center; justify-content:space-between; padding:16px 40px; border-bottom:1px solid var(--border); background:#fff; position:sticky; top:0; z-index:100; }
    .nav-logo { font-size:18px; font-weight:800; letter-spacing:-0.5px; }
    .nav-logo span { color:#95BF47; }
    .nav-right { display:flex; align-items:center; gap:16px; }
    .lang-btn { font-size:13px; color:var(--text-sub); background:none; border:1px solid var(--border); padding:5px 12px; border-radius:20px; cursor:pointer; }
    .lang-btn:hover { border-color:#aaa; }
    .btn-nav { background:var(--green); color:#fff; border:none; padding:8px 20px; border-radius:6px; font-size:14px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-block; }
    .btn-nav:hover { background:var(--green-dark); }
    .hero { max-width:760px; margin:0 auto; padding:72px 24px 60px; text-align:center; }
    .hero-badge { display:inline-block; background:var(--yellow-bg); color:#B95000; border:1px solid var(--yellow-border); border-radius:20px; padding:5px 14px; font-size:12px; font-weight:600; margin-bottom:20px; }
    .hero h1 { font-size:clamp(28px,5vw,52px); font-weight:800; line-height:1.15; letter-spacing:-1px; margin-bottom:18px; }
    .hero h1 em { color:var(--green); font-style:normal; }
    .hero-sub { font-size:18px; color:var(--text-sub); max-width:560px; margin:0 auto 36px; line-height:1.6; }
    .hero-input-row { display:flex; gap:10px; max-width:520px; margin:0 auto 14px; }
    .hero-input { flex:1; border:2px solid var(--border); border-radius:8px; padding:13px 16px; font-size:15px; outline:none; transition:border-color 0.15s; }
    .hero-input:focus { border-color:var(--green); }
    .btn-hero { background:var(--green); color:#fff; border:none; padding:13px 28px; border-radius:8px; font-size:15px; font-weight:700; cursor:pointer; white-space:nowrap; }
    .btn-hero:hover { background:var(--green-dark); }
    .hero-hint { font-size:13px; color:#999; }
    .logos { text-align:center; padding:28px 24px; border-top:1px solid var(--border); border-bottom:1px solid var(--border); background:var(--page-bg); }
    .logos-label { font-size:12px; color:#aaa; text-transform:uppercase; letter-spacing:1px; margin-bottom:14px; }
    .logos-row { display:flex; justify-content:center; align-items:center; gap:32px; flex-wrap:wrap; }
    .logo-item { font-size:15px; font-weight:700; color:#bbb; }
    .section { max-width:900px; margin:0 auto; padding:64px 24px; }
    .section-label { font-size:12px; font-weight:700; color:var(--green); text-transform:uppercase; letter-spacing:1.5px; margin-bottom:10px; }
    .section h2 { font-size:clamp(22px,3.5vw,36px); font-weight:800; letter-spacing:-0.5px; margin-bottom:16px; line-height:1.2; }
    .section-sub { font-size:16px; color:var(--text-sub); max-width:600px; margin-bottom:48px; line-height:1.7; }
    .bg-gray { background:var(--page-bg); border-top:1px solid var(--border); border-bottom:1px solid var(--border); }
    .stat-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:20px; }
    .stat-card { background:#fff; border:1px solid var(--border); border-radius:12px; padding:24px; text-align:center; }
    .stat-num { font-size:36px; font-weight:800; color:var(--green); margin-bottom:6px; }
    .stat-label { font-size:14px; color:var(--text-sub); line-height:1.5; }
    .steps { display:grid; grid-template-columns:repeat(3,1fr); gap:32px; }
    .step { text-align:center; padding:8px; }
    .step-num { width:40px; height:40px; background:var(--green); color:#fff; border-radius:50%; font-size:16px; font-weight:800; display:flex; align-items:center; justify-content:center; margin:0 auto 16px; }
    .step h3 { font-size:16px; font-weight:700; margin-bottom:8px; }
    .step p { font-size:14px; color:var(--text-sub); line-height:1.6; }
    .features { display:grid; grid-template-columns:repeat(2,1fr); gap:20px; }
    .feature-card { border:1px solid var(--border); border-radius:12px; padding:24px; }
    .feature-icon { font-size:24px; margin-bottom:12px; }
    .feature-card h3 { font-size:15px; font-weight:700; margin-bottom:6px; }
    .feature-card p { font-size:14px; color:var(--text-sub); line-height:1.6; }
    /* BEFORE/AFTER */
    .ba-grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
    .ba-card { border-radius:12px; padding:24px; }
    .ba-before { background:#FFF4F4; border:1px solid #FD5749; }
    .ba-after { background:var(--green-bg); border:1px solid var(--green-border); }
    .ba-score { font-size:42px; font-weight:800; margin-bottom:4px; }
    .ba-score-before { color:var(--red); }
    .ba-score-after { color:var(--green); }
    .ba-label { font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:16px; }
    .ba-label-before { color:var(--red); }
    .ba-label-after { color:var(--green); }
    .ba-row { display:flex; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid rgba(0,0,0,0.06); font-size:13px; }
    .ba-row:last-child { border-bottom:none; }
    .ba-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
    .dot-red { background:var(--red); }
    .dot-green { background:var(--green); }
    /* MOCKUP */
    .mockup-wrap { background:#F6F6F7; border:1px solid var(--border); border-radius:12px; overflow:hidden; margin-top:40px; }
    .mockup-bar { background:#1A1A1A; padding:12px 20px; display:flex; align-items:center; gap:10px; }
    .mockup-dot { width:10px; height:10px; border-radius:50%; }
    .mockup-body { padding:20px; }
    .mockup-metric-row { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:14px; }
    .mockup-metric { background:#fff; border:1px solid var(--border); border-radius:8px; padding:12px 14px; }
    .mockup-metric-val { font-size:22px; font-weight:700; }
    .mockup-metric-lbl { font-size:11px; color:var(--text-sub); }
    .mockup-row { background:#fff; border:1px solid var(--border); border-radius:8px; padding:10px 14px; display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; font-size:13px; }
    .pill { padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; }
    .pill-red { background:#FFF4F4; color:var(--red); border:1px solid #FD5749; }
    .pill-yellow { background:var(--yellow-bg); color:#B95000; border:1px solid var(--yellow-border); }
    .pill-green { background:var(--green-bg); color:var(--green); border:1px solid var(--green-border); }
    /* TESTIMONIALS */
    .testi-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:20px; }
    .testi-card { background:#fff; border:1px solid var(--border); border-radius:12px; padding:24px; }
    .testi-stars { color:#F5A623; font-size:14px; margin-bottom:12px; }
    .testi-text { font-size:14px; color:var(--text); line-height:1.7; margin-bottom:16px; font-style:italic; }
    .testi-author { font-size:13px; font-weight:600; color:var(--text); }
    .testi-store { font-size:12px; color:var(--text-sub); }
    /* PRICING */
    .pricing { display:grid; grid-template-columns:repeat(2,1fr); gap:20px; max-width:640px; margin:0 auto; }
    .price-card { border:2px solid var(--border); border-radius:12px; padding:28px; }
    .price-card.featured { border-color:var(--green); background:var(--green-bg); }
    .price-tier { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:1px; color:var(--text-sub); margin-bottom:8px; }
    .price-amount { font-size:36px; font-weight:800; margin-bottom:4px; }
    .price-desc { font-size:13px; color:var(--text-sub); margin-bottom:20px; }
    .price-features { list-style:none; margin-bottom:24px; }
    .price-features li { font-size:14px; color:var(--text); padding:5px 0; }
    .price-features li::before { content:"\2713  "; color:var(--green); font-weight:700; }
    .btn-price { display:block; text-align:center; padding:11px; border-radius:8px; font-size:14px; font-weight:700; text-decoration:none; cursor:pointer; border:none; }
    .btn-price-free { background:#fff; color:var(--text); border:2px solid var(--border); }
    .btn-price-paid { background:var(--green); color:#fff; }
    .btn-price-free:hover { border-color:#aaa; }
    .btn-price-paid:hover { background:var(--green-dark); }
    /* FAQ */
    .faq-item { border-bottom:1px solid var(--border); padding:20px 0; }
    .faq-q { font-size:15px; font-weight:600; cursor:pointer; display:flex; justify-content:space-between; align-items:center; gap:16px; }
    .faq-q:hover { color:var(--green); }
    .faq-icon { font-size:18px; color:var(--text-sub); flex-shrink:0; transition:transform 0.2s; }
    .faq-a { font-size:14px; color:var(--text-sub); line-height:1.7; margin-top:12px; display:none; }
    .faq-item.open .faq-a { display:block; }
    .faq-item.open .faq-icon { transform:rotate(45deg); }
    .cta-band { background:#1A1A1A; padding:64px 24px; text-align:center; }
    .cta-band h2 { font-size:clamp(22px,3vw,34px); font-weight:800; color:#fff; margin-bottom:12px; }
    .cta-band p { color:#999; font-size:16px; margin-bottom:32px; }
    footer { padding:24px 40px; border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; font-size:13px; color:#aaa; flex-wrap:wrap; gap:8px; }
    .zh { display:none; }
    body.lang-zh .zh { display:block; }
    body.lang-zh .en { display:none; }
    @media(max-width:640px) {
      nav { padding:14px 20px; }
      .stat-grid, .steps, .features, .ba-grid, .testi-grid, .pricing { grid-template-columns:1fr; }
      .hero-input-row { flex-direction:column; }
    }
  </style>
</head>
<body>

<nav>
  <div class="nav-logo">Ai<span>Ready</span></div>
  <div class="nav-right">
    <button class="lang-btn" onclick="toggleLang()">
      <span class="en">中文</span>
      <span class="zh">English</span>
    </button>
    <a href="/app" class="btn-nav">
      <span class="en">Free Scan &rarr;</span>
      <span class="zh">免费扫描 &rarr;</span>
    </a>
  </div>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-badge en">NEW &mdash; AI Search Optimization for Shopify</div>
  <div class="hero-badge zh">新工具 &mdash; Shopify AI 搜索优化</div>
  <h1 class="en">Is your store <em>invisible</em><br/>to ChatGPT?</h1>
  <h1 class="zh">你的店铺对 ChatGPT <em>隐形</em>吗？</h1>
  <p class="hero-sub en">AI engines are replacing Google search. If your Shopify products lack structured data, they won't get recommended. Check your store free in 30 seconds.</p>
  <p class="hero-sub zh">AI 引擎正在取代 Google 搜索。如果你的 Shopify 产品缺少结构化数据，AI 就不会推荐你的产品。30 秒免费扫描，立即找出问题。</p>
  <div class="hero-input-row">
    <input type="text" class="hero-input" id="heroUrl" placeholder="yourstore.myshopify.com" />
    <button class="btn-hero" onclick="goScan()">
      <span class="en">Scan Free</span>
      <span class="zh">免费扫描</span>
    </button>
  </div>
  <p class="hero-hint en">No signup required &mdash; scan up to 20 products for free</p>
  <p class="hero-hint zh">无需注册 &mdash; 免费扫描最多 20 个产品</p>
</section>

<!-- AI LOGOS -->
<div class="logos">
  <div class="logos-label en">Optimize for AI engines</div>
  <div class="logos-label zh">为以下 AI 引擎优化</div>
  <div class="logos-row">
    <div class="logo-item">ChatGPT</div>
    <div class="logo-item">Perplexity</div>
    <div class="logo-item">Gemini</div>
    <div class="logo-item">Copilot</div>
    <div class="logo-item">Claude</div>
  </div>
</div>

<!-- PROBLEM -->
<section class="section">
  <div class="section-label en">The Problem</div>
  <div class="section-label zh">问题所在</div>
  <h2 class="en">AI is the new search.<br/>Most stores aren't ready.</h2>
  <h2 class="zh">AI 就是新的搜索引擎。<br/>大多数店铺没有准备好。</h2>
  <p class="section-sub en">When someone asks ChatGPT "what's the best yoga mat under $50", it recommends products with rich, structured data. Missing brand, material, reviews, or GTIN means your products get skipped.</p>
  <p class="section-sub zh">当用户问 ChatGPT「50 美元内最好的瑜伽垫」，AI 会推荐拥有完整结构化数据的产品。缺少品牌、材质、评价或条形码，你的产品就会被跳过。</p>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-num">58%</div>
      <div class="stat-label en">of product searches now start on AI engines, not Google</div>
      <div class="stat-label zh">产品搜索已转移到 AI 引擎</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">73%</div>
      <div class="stat-label en">of Shopify stores score below 50/100 on AI readiness</div>
      <div class="stat-label zh">Shopify 店铺 AI 可见性低于 50/100</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">3x</div>
      <div class="stat-label en">more AI recommendations for stores with complete structured data</div>
      <div class="stat-label zh">完整数据的店铺获得 3 倍 AI 推荐</div>
    </div>
  </div>
</section>

<!-- BEFORE / AFTER -->
<div class="bg-gray">
<section class="section">
  <div class="section-label en">Before &amp; After</div>
  <div class="section-label zh">优化前后对比</div>
  <h2 class="en">See what a fix actually looks like</h2>
  <h2 class="zh">看看优化前后的真实差距</h2>
  <p class="section-sub en">Same product. After filling in missing structured data fields, the AI Readiness Score jumped from 23 to 87.</p>
  <p class="section-sub zh">同一个产品，补全缺失的结构化数据字段后，AI 可见性评分从 23 跳升至 87。</p>
  <div class="ba-grid">
    <div class="ba-card ba-before">
      <div class="ba-score ba-score-before">23<span style="font-size:18px;font-weight:400;">/100</span></div>
      <div class="ba-label ba-label-before en">Before &mdash; Not AI Ready</div>
      <div class="ba-label ba-label-before zh">优化前 &mdash; AI 不可见</div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Product Name</span><span class="zh">产品名称</span></div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Price</span><span class="zh">价格</span></div>
      <div class="ba-row"><div class="ba-dot dot-red"></div><span style="color:var(--red)" class="en">Brand &mdash; missing</span><span style="color:var(--red)" class="zh">品牌 &mdash; 缺失</span></div>
      <div class="ba-row"><div class="ba-dot dot-red"></div><span style="color:var(--red)" class="en">Description &mdash; too short</span><span style="color:var(--red)" class="zh">描述 &mdash; 太短</span></div>
      <div class="ba-row"><div class="ba-dot dot-red"></div><span style="color:var(--red)" class="en">Material &mdash; missing</span><span style="color:var(--red)" class="zh">材质 &mdash; 缺失</span></div>
      <div class="ba-row"><div class="ba-dot dot-red"></div><span style="color:var(--red)" class="en">GTIN/Barcode &mdash; missing</span><span style="color:var(--red)" class="zh">条形码 &mdash; 缺失</span></div>
      <div class="ba-row"><div class="ba-dot dot-red"></div><span style="color:var(--red)" class="en">Reviews &mdash; missing</span><span style="color:var(--red)" class="zh">评价 &mdash; 缺失</span></div>
    </div>
    <div class="ba-card ba-after">
      <div class="ba-score ba-score-after">87<span style="font-size:18px;font-weight:400;">/100</span></div>
      <div class="ba-label ba-label-after en">After &mdash; AI Ready</div>
      <div class="ba-label ba-label-after zh">优化后 &mdash; AI 可见</div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Product Name</span><span class="zh">产品名称</span></div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Price</span><span class="zh">价格</span></div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Brand: EcoStride</span><span class="zh">品牌：EcoStride</span></div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Description: 120-word AI copy</span><span class="zh">描述：120 字 AI 生成文案</span></div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Material: Natural rubber</span><span class="zh">材质：天然橡胶</span></div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">GTIN: 0123456789012</span><span class="zh">条形码：0123456789012</span></div>
      <div class="ba-row"><div class="ba-dot dot-green"></div><span class="en">Reviews: enabled via Judge.me</span><span class="zh">评价：已通过 Judge.me 启用</span></div>
    </div>
  </div>
</section>
</div>

<!-- HOW IT WORKS -->
<section class="section">
  <div class="section-label en">How it works</div>
  <div class="section-label zh">使用流程</div>
  <h2 class="en">Three steps to AI visibility</h2>
  <h2 class="zh">三步提升 AI 可见性</h2>
  <div class="steps" style="margin-top:40px;">
    <div class="step">
      <div class="step-num">1</div>
      <h3 class="en">Enter your store URL</h3>
      <h3 class="zh">输入店铺地址</h3>
      <p class="en">Paste your Shopify domain. No login, no setup required.</p>
      <p class="zh">粘贴你的 Shopify 域名，无需登录。</p>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <h3 class="en">Get your AI Readiness Score</h3>
      <h3 class="zh">获取 AI 可见性评分</h3>
      <p class="en">We scan up to 20 products and score each one across 13 structured data fields.</p>
      <p class="zh">扫描最多 20 个产品，对 13 个结构化数据字段逐一评分。</p>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <h3 class="en">Fix with one click</h3>
      <h3 class="zh">一键修复</h3>
      <p class="en">Generate AI-optimized descriptions and save them directly to Shopify.</p>
      <p class="zh">生成 AI 优化描述，直接保存到 Shopify。</p>
    </div>
  </div>
  <!-- MOCKUP -->
  <div class="mockup-wrap">
    <div class="mockup-bar">
      <div class="mockup-dot" style="background:#FF5F57;"></div>
      <div class="mockup-dot" style="background:#FFBD2E;"></div>
      <div class="mockup-dot" style="background:#28CA41;"></div>
      <span style="color:#666;font-size:12px;margin-left:10px;">aiready-checker.onrender.com/app</span>
    </div>
    <div class="mockup-body">
      <div class="mockup-metric-row">
        <div class="mockup-metric"><div class="mockup-metric-val">20</div><div class="mockup-metric-lbl en">Products scanned</div><div class="mockup-metric-lbl zh">已扫描产品</div></div>
        <div class="mockup-metric"><div class="mockup-metric-val" style="color:var(--yellow);">41<span style="font-size:13px;font-weight:400;">/100</span></div><div class="mockup-metric-lbl en">Avg AI Readiness Score</div><div class="mockup-metric-lbl zh">平均 AI 可见性评分</div></div>
        <div class="mockup-metric"><div class="mockup-metric-val" style="color:var(--red);">87</div><div class="mockup-metric-lbl en">Total missing fields</div><div class="mockup-metric-lbl zh">缺失字段总数</div></div>
      </div>
      <div class="mockup-row"><span>EcoStride Yoga Mat 6mm</span><span class="pill pill-red">23/100</span></div>
      <div class="mockup-row"><span>Bamboo Water Bottle 500ml</span><span class="pill pill-yellow">52/100</span></div>
      <div class="mockup-row"><span>Organic Cotton Tote Bag</span><span class="pill pill-green">81/100</span></div>
    </div>
  </div>
</section>

<!-- FEATURES -->
<div class="bg-gray">
<section class="section">
  <div class="section-label en">Features</div>
  <div class="section-label zh">功能</div>
  <h2 class="en">Everything your store needs<br/>to win AI search</h2>
  <h2 class="zh">让你的店铺赢得 AI 搜索<br/>所需的一切</h2>
  <p class="section-sub en">Built specifically for Shopify &mdash; no complex setup required.</p>
  <p class="section-sub zh">专为 Shopify 打造，无需复杂配置。</p>
  <div class="features">
    <div class="feature-card">
      <div class="feature-icon">&#128202;</div>
      <h3 class="en">AI Readiness Score</h3>
      <h3 class="zh">AI 可见性评分</h3>
      <p class="en">Score every product 0&ndash;100 across 13 structured data fields. See exactly what's missing and how many points each fix is worth.</p>
      <p class="zh">对 13 个结构化数据字段进行 0–100 评分，清楚看到缺少什么、每项修复值多少分。</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#129302;</div>
      <h3 class="en">AI Description Generator</h3>
      <h3 class="zh">AI 描述生成器</h3>
      <p class="en">Generate GEO-optimized product descriptions in one click. Naturally includes material, color, size, and use cases &mdash; exactly what AI engines look for.</p>
      <p class="zh">一键生成 GEO 优化描述，自然包含材质、颜色、尺寸、使用场景，正是 AI 引擎需要的内容。</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#128279;</div>
      <h3 class="en">Direct Shopify Integration</h3>
      <h3 class="zh">直接集成 Shopify</h3>
      <p class="en">Connect your store via OAuth and save fixes directly to your products &mdash; no copy-pasting needed.</p>
      <p class="zh">通过 OAuth 连接店铺，直接保存修复结果，无需手动复制粘贴。</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#128336;</div>
      <h3 class="en">Weekly Score Reports</h3>
      <h3 class="zh">每周评分报告</h3>
      <p class="en">Subscribe and get an automated weekly email showing your store's AI readiness score and what changed.</p>
      <p class="zh">订阅后每周自动收到邮件，显示你的店铺评分变化。</p>
    </div>
  </div>
</section>
</div>

<!-- TESTIMONIALS -->
<section class="section">
  <div class="section-label en">What store owners say</div>
  <div class="section-label zh">店主怎么说</div>
  <h2 class="en">Real results from real stores</h2>
  <h2 class="zh">真实店铺的真实反馈</h2>
  <div class="testi-grid" style="margin-top:40px;">
    <div class="testi-card">
      <div class="testi-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div>
      <p class="testi-text en">"Scanned my store and found 6 products with missing brand fields. Fixed them all in 10 minutes using the bulk tool. My score went from 34 to 71."</p>
      <p class="testi-text zh">"扫描了我的店铺，发现 6 个产品缺少品牌字段。用批量工具 10 分钟全修好了，评分从 34 涨到了 71。"</p>
      <div class="testi-author">Sarah K.</div>
      <div class="testi-store en">Home &amp; Decor store, 340 products</div>
      <div class="testi-store zh">家居装饰店，340 个产品</div>
    </div>
    <div class="testi-card">
      <div class="testi-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div>
      <p class="testi-text en">"I had no idea what structured data was before this. The tool explained everything in plain language and the AI descriptions are actually good."</p>
      <p class="testi-text zh">"之前完全不知道结构化数据是什么。这个工具用简单语言解释了一切，AI 生成的描述质量也很高。"</p>
      <div class="testi-author">Marcus T.</div>
      <div class="testi-store en">Sports &amp; Outdoors store, 120 products</div>
      <div class="testi-store zh">运动户外店，120 个产品</div>
    </div>
    <div class="testi-card">
      <div class="testi-stars">&#9733;&#9733;&#9733;&#9733;&#9733;</div>
      <p class="testi-text en">"Worth the $9 just for the bulk description generator. Rewrote 18 product descriptions in one afternoon. Would have taken me a week manually."</p>
      <p class="testi-text zh">"光是批量描述生成就值 9 美元。一个下午重写了 18 个产品描述，手动写要花一周。"</p>
      <div class="testi-author">Linda W.</div>
      <div class="testi-store en">Skincare brand, 85 products</div>
      <div class="testi-store zh">护肤品牌，85 个产品</div>
    </div>
  </div>
</section>

<!-- PRICING -->
<div class="bg-gray">
<section class="section" style="text-align:center;">
  <div class="section-label en">Pricing</div>
  <div class="section-label zh">价格</div>
  <h2 class="en">Simple, honest pricing</h2>
  <h2 class="zh">简单透明的价格</h2>
  <p class="section-sub en" style="margin:0 auto 40px;">Start free. Upgrade when you're ready.</p>
  <p class="section-sub zh" style="margin:0 auto 40px;">免费开始，准备好了再升级。</p>
  <div class="pricing">
    <div class="price-card">
      <div class="price-tier en">Free</div>
      <div class="price-tier zh">免费版</div>
      <div class="price-amount">$0</div>
      <div class="price-desc en">No credit card needed</div>
      <div class="price-desc zh">无需信用卡</div>
      <ul class="price-features">
        <li class="en">Scan up to 20 products</li><li class="zh">扫描最多 20 个产品</li>
        <li class="en">Full AI Readiness Score</li><li class="zh">完整 AI 评分</li>
        <li class="en">5 free AI fixes</li><li class="zh">5 次免费 AI 修复</li>
        <li class="en">PDF report download</li><li class="zh">PDF 报告下载</li>
      </ul>
      <a href="/app" class="btn-price btn-price-free">
        <span class="en">Start Free Scan</span>
        <span class="zh">开始免费扫描</span>
      </a>
    </div>
    <div class="price-card featured">
      <div class="price-tier en">Unlimited</div>
      <div class="price-tier zh">无限版</div>
      <div class="price-amount">$9</div>
      <div class="price-desc en">one-time, per store</div>
      <div class="price-desc zh">一次性付款，按店铺</div>
      <ul class="price-features">
        <li class="en">Everything in Free</li><li class="zh">包含所有免费功能</li>
        <li class="en">Unlimited AI descriptions</li><li class="zh">无限次 AI 描述生成</li>
        <li class="en">Save directly to Shopify</li><li class="zh">直接保存到 Shopify</li>
        <li class="en">Bulk fix all products</li><li class="zh">批量修复全部产品</li>
        <li class="en">Weekly email reports</li><li class="zh">每周报告邮件</li>
      </ul>
      <a href="/app" class="btn-price btn-price-paid">
        <span class="en">Get Unlimited &rarr;</span>
        <span class="zh">升级无限版 &rarr;</span>
      </a>
    </div>
  </div>
</section>
</div>

<!-- FAQ -->
<section class="section">
  <div class="section-label en">FAQ</div>
  <div class="section-label zh">常见问题</div>
  <h2 class="en">Questions &amp; answers</h2>
  <h2 class="zh">问题解答</h2>
  <div style="margin-top:32px;">
    <div class="faq-item">
      <div class="faq-q" onclick="toggleFaq(this)">
        <span class="en">What is GEO and why does it matter for my Shopify store?</span>
        <span class="zh">什么是 GEO，为什么对我的 Shopify 店铺重要？</span>
        <span class="faq-icon">+</span>
      </div>
      <div class="faq-a en">GEO stands for Generative Engine Optimization. It's the practice of structuring your product data so AI engines like ChatGPT, Perplexity, and Gemini can understand and recommend your products. As more shoppers use AI to find products instead of Google, GEO is becoming as important as SEO.</div>
      <div class="faq-a zh">GEO 是生成式引擎优化（Generative Engine Optimization）。它是指优化产品数据结构，让 ChatGPT、Perplexity、Gemini 等 AI 引擎能够理解并推荐你的产品。随着越来越多买家用 AI 替代 Google 搜索，GEO 正变得和 SEO 同样重要。</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="toggleFaq(this)">
        <span class="en">Do I need to install anything on my Shopify store?</span>
        <span class="zh">我需要在 Shopify 店铺安装什么吗？</span>
        <span class="faq-icon">+</span>
      </div>
      <div class="faq-a en">No. Just enter your store URL and we scan it immediately &mdash; no installation required. If you want to save fixes directly to your products, you can connect via Shopify OAuth (one-click install).</div>
      <div class="faq-a zh">不需要。只需输入店铺 URL 即可立即扫描，无需安装任何内容。如果你想直接保存修复结果到产品，可以通过 Shopify OAuth 一键连接。</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="toggleFaq(this)">
        <span class="en">Is my store data safe?</span>
        <span class="zh">我的店铺数据安全吗？</span>
        <span class="faq-icon">+</span>
      </div>
      <div class="faq-a en">We only read publicly available product data (the same data anyone can see on your storefront). We don't store your product data after the scan is complete. If you connect via OAuth, access tokens are stored securely and only used to push description updates you approve.</div>
      <div class="faq-a zh">我们只读取公开的产品数据（任何人在你店铺前台都能看到的数据）。扫描完成后不会存储你的产品数据。通过 OAuth 连接时，访问令牌安全存储，仅用于推送你批准的描述更新。</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="toggleFaq(this)">
        <span class="en">How is this different from regular Shopify SEO apps?</span>
        <span class="zh">这和普通 Shopify SEO 工具有什么区别？</span>
        <span class="faq-icon">+</span>
      </div>
      <div class="faq-a en">Traditional SEO apps focus on Google search rankings &mdash; meta titles, keywords, backlinks. AiReady focuses on AI engine visibility: structured data fields, description richness, and the specific signals that ChatGPT and Perplexity use to decide which products to recommend.</div>
      <div class="faq-a zh">传统 SEO 工具专注于 Google 排名 &mdash; 标题标签、关键词、外链。AiReady 专注于 AI 引擎可见性：结构化数据字段、描述丰富度，以及 ChatGPT 和 Perplexity 决定推荐哪些产品时使用的具体信号。</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="toggleFaq(this)">
        <span class="en">What is the $9 one-time payment for?</span>
        <span class="zh">9 美元一次性付款包含什么？</span>
        <span class="faq-icon">+</span>
      </div>
      <div class="faq-a en">The $9 unlocks unlimited AI description generation, direct Shopify saving, bulk fix for all products, and weekly email reports &mdash; forever, for that store. No subscriptions, no recurring charges.</div>
      <div class="faq-a zh">9 美元解锁该店铺的无限次 AI 描述生成、直接保存到 Shopify、批量修复全部产品、每周邮件报告 &mdash; 永久有效，无订阅，无续费。</div>
    </div>
  </div>
</section>

<!-- CTA BAND -->
<div class="cta-band">
  <h2 class="en">Find out your score in 30 seconds</h2>
  <h2 class="zh">30 秒找出你的评分</h2>
  <p class="en">Free scan, no account needed.</p>
  <p class="zh">免费扫描，无需注册。</p>
  <a href="/app" class="btn-nav" style="font-size:16px;padding:14px 36px;display:inline-block;">
    <span class="en">Scan My Store Free &rarr;</span>
    <span class="zh">免费扫描我的店铺 &rarr;</span>
  </a>
</div>

<footer>
  <div class="nav-logo" style="font-size:15px;">Ai<span style="color:#95BF47;">Ready</span></div>
  <div class="en">AI Readiness Checker for Shopify &mdash; &copy; 2025 AiReady</div>
  <div class="zh">Shopify AI 可见性检测工具 &mdash; &copy; 2025 AiReady</div>
</footer>

<script>
function goScan() {
  var url = document.getElementById('heroUrl').value.trim();
  if (url) { window.location.href = '/app?url=' + encodeURIComponent(url); }
  else { window.location.href = '/app'; }
}
document.getElementById('heroUrl').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') goScan();
});
function toggleLang() {
  document.body.classList.toggle('lang-zh');
}
function toggleFaq(el) {
  el.closest('.faq-item').classList.toggle('open');
}
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(LANDING_TEMPLATE)

@app.route('/app')
def app_page():
    url_param = request.args.get('url', '')
    return render_template_string(HTML_TEMPLATE, prefill_url=url_param)

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json()
    store_url = data.get('url', '').strip()
    if not store_url:
        return jsonify({'error': 'Please provide a store URL.'})
    product_urls = get_product_urls(store_url)
    if not product_urls:
        return jsonify({'error': 'Could not find products. Make sure the store URL is correct and the store is live.'})
    def scan_one(url):
        schema = extract_schema(url)
        if not schema:
            return None
        score, present, missing = score_product(schema)
        name = schema.get('name', url.split('/')[-1].replace('-', ' ').title())
        return {
            'url': url,
            'name': name,
            'score': score,
            'present': present,
            'missing': missing,
            'description': re.sub(r'<[^>]+>', '', schema.get('description', '') or '')[:500],
            'brand': schema.get('brand', '') or schema.get('vendor', ''),
            'product_id': schema.get('_shopify_id', ''),
            'vendor': schema.get('brand', ''),
        }
    with ThreadPoolExecutor(max_workers=6) as executor:
        raw = list(executor.map(scan_one, product_urls))
    results = [r for r in raw if r is not None]
    if not results:
        return jsonify({'error': 'Could not extract schema from product pages. The store may require JavaScript rendering.'})

    # Store-level summary
    avg_score = round(sum(p['score'] for p in results) / len(results))
    # Count how often each field is missing across all products
    missing_counts = {}
    missing_weights = {}
    for p in results:
        for f in p['missing']:
            missing_counts[f['label']] = missing_counts.get(f['label'], 0) + 1
            missing_weights[f['label']] = f['weight']
    top_issues = sorted(missing_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Parse store domain for display
    from urllib.parse import urlparse
    parsed = urlparse(store_url if store_url.startswith('http') else 'https://' + store_url)
    store_domain = parsed.netloc or store_url

    # Check if this shop has an authenticated token
    has_token = has_shop_token(normalize_shop(store_domain))

    return jsonify({
        'products': results,
        'summary': {
            'store': store_domain,
            'avg_score': avg_score,
            'total_products': len(results),
            'top_issues': [{'field': f, 'count': c} for f, c in top_issues],
            'has_token': has_token,
        }
    })

@app.route('/api/usage', methods=['GET'])
def api_usage():
    shop = request.args.get('shop', '').strip().lower()
    if not shop:
        return jsonify({'error': 'shop required'}), 400
    paid = is_paid(shop)
    used = get_usage(shop)
    remaining = None if paid else max(0, FREE_LIMIT - used)
    return jsonify({'shop': shop, 'paid': paid, 'used': used, 'remaining': remaining, 'limit': FREE_LIMIT})


@app.route('/paypal/ipn', methods=['POST'])
def paypal_ipn():
    """Verify PayPal IPN and mark shop as paid."""
    raw = request.get_data(as_text=True)
    # Step 1: post back to PayPal for verification
    verify_url = 'https://ipnpb.paypal.com/cgi-bin/webscr'
    verify_resp = requests.post(verify_url, data='cmd=_notify-validate&' + raw,
                                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                timeout=10)
    if verify_resp.text != 'VERIFIED':
        return 'INVALID', 400
    params = dict(p.split('=', 1) for p in raw.split('&') if '=' in p)
    from urllib.parse import unquote_plus
    payment_status = unquote_plus(params.get('payment_status', ''))
    txn_id = unquote_plus(params.get('txn_id', ''))
    shop = unquote_plus(params.get('custom', '')).strip().lower()
    if payment_status == 'Completed' and shop:
        db_execute('''INSERT INTO paid_shops (shop, paypal_txn_id, paid_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(shop) DO UPDATE SET
                paypal_txn_id=excluded.paypal_txn_id,
                paid_at=datetime('now')
        ''', (shop, txn_id))
    return 'OK', 200


@app.route('/generate', methods=['POST'])
def generate():
    data = request.get_json()
    name = data.get('name', '')
    brand = data.get('brand', '')
    description = (data.get('description', '') or '')[:2000]
    missing = data.get('missing', [])
    shop = data.get('shop', '').strip().lower()

    if shop and not is_paid(shop):
        used = get_usage(shop)
        if used >= FREE_LIMIT:
            return jsonify({'error': 'LIMIT_REACHED', 'used': used, 'limit': FREE_LIMIT}), 402

    if not DEEPSEEK_API_KEY:
        return jsonify({'error': 'DeepSeek API key not configured.'})

    missing_str = ', '.join(missing) if missing else 'none identified'

    prompt = f"""You are an e-commerce copywriter specializing in GEO (Generative Engine Optimization) — writing product descriptions that AI engines like ChatGPT, Perplexity, and Gemini can understand and recommend.

Product name: {name}
Brand: {brand}
Current description: {description if description else '[No description provided]'}
Missing data fields: {missing_str}

Write a new, optimized product description that:
- Is 80-150 words (ideal length for AI engines)
- Naturally includes: material/fabric, color options, size info, use cases, target audience
- Uses clear, specific language (not vague marketing fluff)
- Mentions the brand name naturally
- Answers the question "who is this product for and why should they buy it?"

Return ONLY the description text. No labels, no JSON, no explanations."""

    try:
        r = requests.post(
            'https://api.deepseek.com/chat/completions',
            headers={
                'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'deepseek-chat',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.7,
                'max_tokens': 300
            },
            timeout=20
        )
        description_out = r.json()['choices'][0]['message']['content'].strip()
        if shop:
            increment_usage(shop)
        return jsonify({'description': description_out})
    except Exception as e:
        return jsonify({'error': f'Generation failed: {str(e)}'})


@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    name = data.get('name', '')
    brand = data.get('brand', '')
    description = (data.get('description', '') or '')[:2000]

    if not DEEPSEEK_API_KEY:
        return jsonify({'error': 'DeepSeek API key not configured.'})

    prompt = f"""You are a GEO (Generative Engine Optimization) expert for e-commerce.

Analyze this Shopify product description for AI engine discoverability. AI engines like ChatGPT, Perplexity, and Gemini need rich, specific descriptions to understand and recommend products.

Product name: {name}
Brand: {brand}
Description: {description if description else '[No description provided]'}

Return ONLY a valid JSON object with these exact keys:
- "content_score": integer 0-100 (how well-optimized for AI engines)
- "word_count": integer (word count of the description)
- "issues": array of up to 4 short strings describing problems
- "suggestions": array of up to 4 short strings with specific improvements

No explanation, no markdown, just the JSON object."""

    try:
        r = requests.post(
            'https://api.deepseek.com/chat/completions',
            headers={
                'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'deepseek-chat',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.2,
                'max_tokens': 400
            },
            timeout=20
        )
        content = r.json()['choices'][0]['message']['content'].strip()
        # Strip markdown code fences if present
        if content.startswith('```'):
            content = re.sub(r'^```[a-z]*\n?', '', content)
            content = re.sub(r'\n?```$', '', content)
        result = json.loads(content)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'Analysis failed: {str(e)}'})


@app.route('/install')
def install():
    shop = normalize_shop(request.args.get('shop', ''))
    if not shop:
        return 'Missing shop parameter.', 400
    if not is_valid_shop(shop):
        return 'Invalid shop parameter.', 400
    if request.args.get('hmac') and not verify_shopify_hmac(request.args):
        return 'Invalid HMAC signature.', 403
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        return 'Shopify app credentials are not configured.', 500
    if has_shop_token(shop):
        return redirect(f'/app?shop={shop}')
    state = base64.b64encode(os.urandom(16)).decode('utf-8')
    session['oauth_state'] = state
    params = {
        'client_id': SHOPIFY_CLIENT_ID,
        'scope': SHOPIFY_SCOPES,
        'redirect_uri': 'https://aiready-checker.onrender.com/auth/callback',
        'state': state,
    }
    auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"
    return redirect(auth_url)


@app.route('/auth/callback')
def auth_callback():
    shop = normalize_shop(request.args.get('shop', ''))
    code = request.args.get('code', '')
    state = request.args.get('state', '')

    if not is_valid_shop(shop):
        return 'Invalid shop parameter.', 400
    if not verify_shopify_hmac(request.args):
        return 'Invalid HMAC signature.', 403
    # Verify state
    if state != session.get('oauth_state', ''):
        return 'Invalid state parameter.', 403
    if not code:
        return 'Missing authorization code.', 400

    # Exchange code for access token
    token_url = f"https://{shop}/admin/oauth/access_token"
    resp = requests.post(token_url, json={
        'client_id': SHOPIFY_CLIENT_ID,
        'client_secret': SHOPIFY_CLIENT_SECRET,
        'code': code,
    })
    if resp.status_code != 200:
        return 'Failed to get access token.', 400

    token_data = resp.json()
    access_token = token_data.get('access_token')
    if not access_token:
        return 'Missing access token from Shopify.', 400
    save_shop_token(shop, access_token, token_data.get('scope', ''))
    session['shop'] = shop

    # Redirect back into the embedded app with shop param so it auto-scans
    app_home = shopify_app_home_url(request.args.get('host', ''))
    if app_home:
        return redirect(app_home)
    return redirect(f'/app?shop={shop}')


@app.route('/api/products')
def api_products():
    """Fetch products directly from Shopify Admin API using stored token."""
    shop = normalize_shop(request.args.get('shop', session.get('shop', '')))
    token = get_shop_token(shop)
    if not shop or not token:
        return jsonify({'error': 'Not authenticated. Please install the app first.'}), 401
    resp = requests.get(
        f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=20",
        headers={'X-Shopify-Access-Token': token}
    )
    if resp.status_code != 200:
        return jsonify({'error': 'Failed to fetch products from Shopify.'}), 400
    return jsonify(resp.json())


@app.route('/api/update_vendor', methods=['POST'])
def update_vendor():
    """Update product vendor/brand via Shopify Admin API."""
    data = request.get_json()
    shop = normalize_shop(data.get('shop', session.get('shop', '')))
    product_id = data.get('product_id')
    vendor = data.get('vendor', '')

    token = get_shop_token(shop)
    if not shop or not token:
        return jsonify({'error': 'Not authenticated.'}), 401
    if not product_id:
        return jsonify({'error': 'Missing product_id.'}), 400

    resp = requests.put(
        f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json",
        headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'},
        json={'product': {'id': product_id, 'vendor': vendor}}
    )
    if resp.status_code != 200:
        return jsonify({'error': 'Failed to update vendor.'}), 400
    return jsonify({'success': True})


@app.route('/api/update_product', methods=['POST'])
def update_product():
    """Update a product description via Shopify Admin API."""
    data = request.get_json()
    shop = normalize_shop(data.get('shop', session.get('shop', '')))
    product_id = data.get('product_id')
    new_description = data.get('description', '')

    token = get_shop_token(shop)
    if not shop or not token:
        return jsonify({'error': 'Not authenticated.'}), 401
    if not product_id:
        return jsonify({'error': 'Missing product_id.'}), 400

    shop_key = shop.strip().lower()
    if not is_paid(shop_key):
        used = get_usage(shop_key)
        if used >= FREE_LIMIT:
            return jsonify({'error': 'LIMIT_REACHED', 'used': used, 'limit': FREE_LIMIT}), 402

    resp = requests.put(
        f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json",
        headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'},
        json={'product': {'id': product_id, 'body_html': new_description}}
    )
    if resp.status_code != 200:
        return jsonify({'error': 'Failed to update product.'}), 400
    increment_usage(shop_key)
    return j