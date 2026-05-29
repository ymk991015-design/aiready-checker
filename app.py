from flask import Flask, request, jsonify, render_template_string, redirect, session
import requests
from bs4 import BeautifulSoup
import json
import re
import os
import hmac
import hashlib
import base64
from urllib.parse import urljoin, urlparse, urlencode

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'aiready-secret-key-2025')

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
SHOPIFY_CLIENT_ID = os.environ.get('SHOPIFY_CLIENT_ID', '')
SHOPIFY_CLIENT_SECRET = os.environ.get('SHOPIFY_CLIENT_SECRET', '')
SHOPIFY_SCOPES = 'read_products,write_products'

# In-memory token store (fine for MVP; replace with DB later)
shop_tokens = {}

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

    @media(max-width: 640px) {
      .metrics { grid-template-columns: 1fr; }
      .issue-name { min-width: 120px; }
      .page { padding: 16px 16px 40px; }
    }
  </style>
</head>
<body>

<div class="topbar">
  <div class="topbar-logo">Ai<span>Ready</span></div>
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
// Detect shop from URL params (Shopify passes this when loading embedded app)
(function() {
  var params = new URLSearchParams(window.location.search);
  var shop = params.get('shop');
  if (shop) {
    var banner = document.getElementById('shopBanner');
    var label = document.getElementById('shopLabel');
    if (banner) banner.classList.add('visible');
    if (label) label.textContent = shop;
    document.getElementById('storeUrl').value = shop;
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
  const scoreCol = s.avg_score >= 70 ? 'metric-green' : s.avg_score >= 40 ? 'metric-yellow' : 'metric-red';

  // Priority fixes
  const weightMap = {};
  for (const p of data.products) {
    for (const f of p.missing) {
      if (!weightMap[f.label] || weightMap[f.label].weight < f.weight) weightMap[f.label] = f;
    }
  }
  const priorityFixes = Object.values(weightMap).sort((a,b) => b.weight - a.weight).slice(0,3);

  let html = `
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
        <div style="display:flex;gap:8px;">
          <button class="btn-secondary" onclick="shareScore(${s.avg_score},'${s.store}')">Share Score</button>
          <button class="btn-secondary" onclick="downloadPDF()">Download PDF</button>
        </div>
      </div>
      <div class="card-body">`;
    for (const issue of s.top_issues) {
      const pct = Math.round((issue.count / maxCount) * 100);
      html += `<div class="issue-row">
        <span class="issue-name">${issue.field}</span>
        <div class="issue-bar-wrap"><div class="issue-bar" style="width:${pct}%"></div></div>
        <span class="issue-count">${issue.count} of ${s.total_products} products</span>
      </div>`;
    }
    html += `</div></div>`;
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
        <div><div class="fix-label">${f.label}</div><div class="fix-hint">${hint}</div></div>
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
    const en = (p.name||'').split("'").join("").split('"').join('').slice(0,60);
    const eb = (p.brand||'').split("'").join("").slice(0,40);
    const ed = (p.description||'').split("'").join("").slice(0,200);
    const ml = p.missing.map(f => f.label).join('|');

    html += `<tr class="product-row" onclick="toggleRow(${idx})">
      <td>
        <div class="product-name-cell">${p.name}<span class="expand-icon">&#9654;</span></div>
        <div class="product-url-cell">${p.url}</div>
      </td>
      <td><span class="score-pill ${sc}">${p.score}/100</span></td>
      <td><span style="color:var(--green);font-size:13px;font-weight:500;">${passCount} passed</span> &nbsp; <span style="color:var(--red);font-size:13px;">${failCount} missing</span></td>
    </tr>
    <tr class="detail-row" id="detail-${idx}">
      <td colspan="3" class="detail-cell">
        <div class="chips">`;
    for (const f of p.present) {
      html += `<span class="chip chip-ok">${f}</span>`;
    }
    for (const f of p.missing) {
      const hint = FIX_HINTS[f.label] || 'Add this field to improve AI discoverability';
      html += `<span class="chip chip-miss" title="${hint}">${f.label} - missing</span>`;
    }
    html += `</div>
        <div class="detail-actions">
          <button class="btn-secondary" onclick="event.stopPropagation();analyzeContent(this,'${en}','${eb}','${ed}')">Analyze Content</button>
          <button class="btn-primary" onclick="event.stopPropagation();generateDesc(this,'${en}','${eb}','${ed}','${ml}')">Generate Optimized Description</button>
        </div>
        <div class="analyze-result" style="display:none;margin-top:12px;"></div>
        <div class="generate-result" style="display:none;margin-top:12px;"></div>
      </td>
    </tr>`;
  });

  html += `</tbody></table></div>`;

  // Email capture
  html += `<div class="email-card">
    <h3>Get weekly AI visibility tips</h3>
    <p>Actionable GEO tips for Shopify stores - free.</p>
    <form class="email-row" action="https://formspree.io/f/xqejnzbb" method="POST">
      <input type="email" name="email" class="email-input" placeholder="your@email.com" required />
      <button type="submit" class="btn-primary">Subscribe</button>
    </form>
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
      resultBox.innerHTML = `<div class="error-banner">${data.error}</div>`;
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
          html += `<div class="ai-issue">&#9888; ${issue}</div>`;
        }
        html += `</div>`;
      }
      if (data.suggestions && data.suggestions.length) {
        html += `<div style="font-size:12px;font-weight:600;color:var(--text-sub);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Suggestions</div>`;
        for (const sg of data.suggestions) {
          html += `<div class="ai-sugg">&#8594; ${sg}</div>`;
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

  try {
    const res = await fetch('/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, brand, description, missing})
    });
    const data = await res.json();
    if (data.error) {
      resultBox.innerHTML = `<div class="error-banner">${data.error}</div>`;
    } else {
      const descText = data.description.split('`').join("'");
      resultBox.innerHTML = `
        <div class="ai-result">
          <div class="ai-result-header">
            <span>AI-Optimized Description</span>
            <button class="btn-secondary" style="font-size:12px;padding:5px 12px;" onclick="copyText(this,'${descText.split("'").join('')}')">Copy</button>
          </div>
          <div class="ai-result-body">${data.description}</div>
        </div>`;
    }
  } catch(e) {
    resultBox.innerHTML = `<span style="color:var(--red);font-size:13px;">Could not connect to generation service.</span>`;
  }
  btn.disabled = false;
  btn.textContent = 'Generate Optimized Description';
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
        results.append({
            'url': url,
            'name': name,
            'score': score,
            'present': present,
            'missing': missing,  # list of {label, weight}
            'description': re.sub(r'<[^>]+>', '', schema.get('description', '') or '')[:500],
            'brand': schema.get('brand', '') or schema.get('vendor', ''),
        })
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

    return jsonify({
        'products': results,
        'summary': {
            'store': store_domain,
            'avg_score': avg_score,
            'total_products': len(results),
            'top_issues': [{'field': f, 'count': c} for f, c in top_issues],
        }
    })

@app.route('/generate', methods=['POST'])
def generate():
    data = request.get_json()
    name = data.get('name', '')
    brand = data.get('brand', '')
    description = (data.get('description', '') or '')[:2000]
    missing = data.get('missing', [])  # list of missing field labels

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

  