from flask import Flask, request, jsonify, render_template_string, redirect, session, Response
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
import time
import html
import csv
import io
from urllib.parse import urljoin, urlparse, urlencode, unquote_plus
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.environ.get('FLASK_SECRET') or os.urandom(32)

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
REPORT_FROM_EMAIL = os.environ.get('REPORT_FROM_EMAIL', 'AiReady <onboarding@resend.dev>')
CRON_SECRET = os.environ.get('CRON_SECRET', '')
USD_PRICE = float(os.environ.get('USD_PRICE', '9.99'))
SHOPIFY_MONTHLY_PRICE = float(os.environ.get('SHOPIFY_MONTHLY_PRICE', '9.99'))
SHOPIFY_BILLING_TEST = os.environ.get('SHOPIFY_BILLING_TEST', '').lower() in ('1', 'true', 'yes')
SHOPIFY_BILLING_NAME = os.environ.get('SHOPIFY_BILLING_NAME', 'AiReady Unlimited')
PAYPAL_HOSTED_BUTTON_ID = os.environ.get('PAYPAL_HOSTED_BUTTON_ID', 'VA8TFCR6A8NMY')
PAYPAL_RECEIVER_EMAIL = os.environ.get('PAYPAL_RECEIVER_EMAIL', '').strip().lower()
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'https://aiready-checker.onrender.com').rstrip('/')
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
DB_PATH = os.environ.get('DB_PATH', os.path.join(tempfile.gettempdir(), 'aiready.db'))
USE_POSTGRES = bool(DATABASE_URL)

FREE_PRODUCT_LIMIT = 20
PAID_PRODUCT_LIMIT = 250

def display_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    if value == int(value):
        return str(int(value))
    return f'{value:.2f}'.rstrip('0').rstrip('.')

@app.after_request
def add_security_headers(response):
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.shopify.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://www.paypalobjects.com; "
        "connect-src 'self'; "
        "form-action 'self' https://www.paypal.com https://www.sandbox.paypal.com; "
        "frame-ancestors https://admin.shopify.com https://*.myshopify.com; "
        "base-uri 'self'; "
        "object-src 'none'"
    )
    response.headers.setdefault('Content-Security-Policy', csp)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    return response

@app.errorhandler(500)
def handle_server_error(error):
    if request.path == '/scan':
        original = getattr(error, 'original_exception', None)
        app.logger.error('Scan failed with server error: %s', original or error)
        return jsonify({
            'error': 'Temporary server error while scanning this store. Please reopen AiReady from Shopify Admin and try again.',
        }), 500
    return error

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
        refresh_token TEXT DEFAULT '',
        expires_at INTEGER DEFAULT 0,
        refresh_token_expires_at INTEGER DEFAULT 0,
        scope TEXT DEFAULT '',
        updated_at {now_type}
    )''')
    for col, col_type in (
        ('refresh_token', "TEXT DEFAULT ''"),
        ('expires_at', 'INTEGER DEFAULT 0'),
        ('refresh_token_expires_at', 'INTEGER DEFAULT 0'),
    ):
        try:
            db_execute(f'ALTER TABLE shop_tokens ADD COLUMN {col} {col_type}')
        except Exception:
            pass
    db_execute(f'''CREATE TABLE IF NOT EXISTS unlock_requests (
        id {id_type},
        email TEXT,
        shop TEXT,
        created_at {now_type}
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS paypal_intents (
        id {id_type},
        shop TEXT NOT NULL,
        created_at {now_type}
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS scan_events (
        id {id_type},
        shop TEXT NOT NULL,
        source TEXT DEFAULT '',
        avg_score INTEGER DEFAULT 0,
        total_products INTEGER DEFAULT 0,
        created_at {now_type}
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS email_suppression (
        email TEXT PRIMARY KEY,
        reason TEXT DEFAULT 'unsubscribe',
        created_at {now_type}
    )''')
    db_execute(f'''CREATE TABLE IF NOT EXISTS lead_events (
        id {id_type},
        email TEXT NOT NULL,
        shop TEXT NOT NULL,
        source TEXT DEFAULT '',
        avg_score INTEGER DEFAULT 0,
        total_products INTEGER DEFAULT 0,
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

def is_valid_email(email):
    return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email or ''))

def unsubscribe_token(email):
    email = (email or '').strip().lower()
    secret = (ADMIN_SECRET or app.secret_key or 'aiready').encode('utf-8')
    return hmac.new(secret, email.encode('utf-8'), hashlib.sha256).hexdigest()[:32]

def unsubscribe_url(email):
    email = (email or '').strip().lower()
    return f"{APP_BASE_URL}/unsubscribe?email={email}&token={unsubscribe_token(email)}"

def is_suppressed_email(email):
    row = db_execute('SELECT 1 FROM email_suppression WHERE email=?', ((email or '').strip().lower(),), fetchone=True)
    return bool(row)

def suppress_email(email, reason='unsubscribe'):
    email = (email or '').strip().lower()
    if not email:
        return
    db_execute('''INSERT INTO email_suppression (email, reason)
        VALUES (?, ?)
        ON CONFLICT(email) DO UPDATE SET reason=excluded.reason
    ''', (email, reason))
    db_execute('DELETE FROM subscriptions WHERE email=?', (email,))

def clean_source(source):
    source = re.sub(r'[^a-zA-Z0-9_.:-]', '', source or '').strip().lower()
    return source[:80]

def record_scan_event(shop, source, avg_score, total_products):
    db_execute(
        '''INSERT INTO scan_events (shop, source, avg_score, total_products)
           VALUES (?, ?, ?, ?)''',
        (shop[:255], clean_source(source), int(avg_score or 0), int(total_products or 0))
    )

def record_lead_event(email, shop, source, avg_score, total_products):
    db_execute(
        '''INSERT INTO lead_events (email, shop, source, avg_score, total_products)
           VALUES (?, ?, ?, ?, ?)''',
        (
            (email or '').strip().lower()[:255],
            (shop or '').strip().lower()[:255],
            clean_source(source),
            int(avg_score or 0),
            int(total_products or 0),
        )
    )

def lead_priority(avg_score, total_products, paid):
    score = 0
    if not paid:
        score += 30
    if avg_score and avg_score < 40:
        score += 40
    elif avg_score and avg_score < 65:
        score += 25
    elif avg_score:
        score += 10
    if total_products >= 100:
        score += 25
    elif total_products >= 20:
        score += 15
    elif total_products:
        score += 5
    if score >= 70:
        return 'high'
    if score >= 45:
        return 'medium'
    return 'low'

def send_scan_report_email(email, shop, summary=None, products=None):
    if not RESEND_API_KEY:
        return False

    summary = summary or {}
    products = products or []
    avg_score = summary.get('avg_score', 0)
    top_issues = summary.get('top_issues') or []
    score_color = '#008060' if avg_score >= 70 else '#B98900' if avg_score >= 40 else '#D72C0D'

    issue_rows = ''.join(
        f"<li>{html.escape(str(issue.get('field', 'Issue')))} "
        f"<span style='color:#6D7175;'>({int(issue.get('count', 0))} products)</span></li>"
        for issue in top_issues[:5] if isinstance(issue, dict)
    ) or '<li>No major missing fields found.</li>'

    product_rows = ''.join(
        "<tr>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;'>{html.escape(str(p.get('name', 'Product'))[:70])}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;font-weight:700;color:{'#008060' if int(p.get('score', 0)) >= 70 else '#B98900' if int(p.get('score', 0)) >= 40 else '#D72C0D'};'>{int(p.get('score', 0))}/100</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;color:#6D7175;font-size:12px;'>{html.escape(', '.join((p.get('missing') or [])[:4]))}</td>"
        "</tr>"
        for p in products[:10] if isinstance(p, dict)
    ) or "<tr><td colspan='3' style='padding:8px;color:#6D7175;'>Scan details are available on AiReady.</td></tr>"

    safe_shop = html.escape(shop)
    report_url = f"{APP_BASE_URL}/app?url={safe_shop}&source=email_report"
    unsub_url = html.escape(unsubscribe_url(email))
    html_body = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px;margin:0 auto;color:#202223;">
  <div style="background:#1A1A1A;padding:18px 22px;border-radius:8px 8px 0 0;">
    <span style="color:#fff;font-size:18px;font-weight:700;">Ai<span style="color:#95BF47;">Ready</span></span>
    <span style="color:#999;font-size:13px;margin-left:12px;">AI Readiness Report</span>
  </div>
  <div style="border:1px solid #E4E5E7;border-top:none;padding:22px;border-radius:0 0 8px 8px;">
    <h2 style="margin:0 0 6px;font-size:20px;">{safe_shop}</h2>
    <p style="margin:0 0 20px;color:#6D7175;">Current AI visibility score</p>
    <div style="font-size:38px;font-weight:800;color:{score_color};margin-bottom:18px;">{int(avg_score)}/100</div>
    <h3 style="font-size:14px;margin:0 0 8px;">Top fixes</h3>
    <ul style="margin:0 0 18px;padding-left:20px;color:#202223;">{issue_rows}</ul>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:10px;">
      <thead><tr>
        <th style="text-align:left;padding:8px;background:#F6F6F7;color:#6D7175;font-size:11px;text-transform:uppercase;">Product</th>
        <th style="text-align:left;padding:8px;background:#F6F6F7;color:#6D7175;font-size:11px;text-transform:uppercase;">Score</th>
        <th style="text-align:left;padding:8px;background:#F6F6F7;color:#6D7175;font-size:11px;text-transform:uppercase;">Missing</th>
      </tr></thead>
      <tbody>{product_rows}</tbody>
    </table>
    <div style="text-align:center;margin-top:24px;">
      <a href="{report_url}" style="display:inline-block;background:#008060;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600;">Open full report</a>
    </div>
    <p style="font-size:12px;color:#8C9196;text-align:center;margin-top:18px;">
      You requested this report from AiReady. <a href="{unsub_url}" style="color:#8C9196;">Unsubscribe</a>
    </p>
  </div>
</div>"""

    response = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
        json={
            'from': REPORT_FROM_EMAIL,
            'to': [email],
            'subject': f'AiReady report: {shop} scored {int(avg_score)}/100',
            'html': html_body,
        },
        timeout=12
    )
    return response.status_code < 400

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

def _b64url_encode(value):
    if not isinstance(value, bytes):
        value = value.encode('utf-8')
    return base64.urlsafe_b64encode(value).decode('utf-8').rstrip('=')

def create_oauth_state(shop):
    payload = {
        'shop': normalize_shop(shop),
        'iat': int(time.time()),
        'nonce': _b64url_encode(os.urandom(16)),
    }
    body = _b64url_encode(json.dumps(payload, separators=(',', ':'), sort_keys=True))
    sig = hmac.new(SHOPIFY_CLIENT_SECRET.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
    return f'{body}.{_b64url_encode(sig)}'

def verify_oauth_state(state, shop, max_age=600):
    if not state or not SHOPIFY_CLIENT_SECRET:
        return False
    try:
        body, sig = state.split('.', 1)
        expected = hmac.new(SHOPIFY_CLIENT_SECRET.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
        actual = _b64url_decode(sig)
        if not hmac.compare_digest(expected, actual):
            return False
        payload = json.loads(_b64url_decode(body).decode('utf-8'))
        if normalize_shop(payload.get('shop', '')) != normalize_shop(shop):
            return False
        issued_at = int(payload.get('iat', 0))
        now = int(time.time())
        return now - max_age <= issued_at <= now + 30
    except Exception:
        return False

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

def verify_shopify_webhook(raw_body):
    incoming_hmac = request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not incoming_hmac or not SHOPIFY_CLIENT_SECRET:
        return False
    digest = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode('utf-8'),
        raw_body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(expected, incoming_hmac)

def _b64url_decode(value):
    value = value.encode('utf-8') if isinstance(value, str) else value
    value += b'=' * (-len(value) % 4)
    return base64.urlsafe_b64decode(value)

def verify_shopify_session_token(token):
    if not token or not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        return None
    try:
        header_b64, payload_b64, sig_b64 = token.split('.', 2)
        signed = f'{header_b64}.{payload_b64}'.encode('utf-8')
        expected = hmac.new(SHOPIFY_CLIENT_SECRET.encode('utf-8'), signed, hashlib.sha256).digest()
        actual = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, actual):
            return None
        header = json.loads(_b64url_decode(header_b64).decode('utf-8'))
        payload = json.loads(_b64url_decode(payload_b64).decode('utf-8'))
        now = int(time.time())
        if header.get('alg') != 'HS256':
            return None
        if payload.get('aud') != SHOPIFY_CLIENT_ID:
            return None
        if int(payload.get('exp', 0)) < now:
            return None
        if int(payload.get('nbf', 0)) > now + 5:
            return None
        dest = payload.get('dest', '')
        shop = normalize_shop(dest)
        if not is_valid_shop(shop):
            return None
        payload['shop'] = shop
        return payload
    except Exception:
        return None

def current_shopify_session():
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.lower().startswith('bearer '):
        return None
    return verify_shopify_session_token(auth_header.split(None, 1)[1].strip())

def delete_shop_data(shop):
    shop = normalize_shop(shop)
    if not shop or not is_valid_shop(shop):
        return False
    db_execute('DELETE FROM shop_tokens WHERE shop=?', (shop,))
    db_execute('DELETE FROM subscriptions WHERE shop=?', (shop,))
    db_execute('DELETE FROM usage_counts WHERE shop=?', (shop,))
    db_execute('DELETE FROM paid_shops WHERE shop=?', (shop,))
    db_execute('DELETE FROM unlock_requests WHERE shop=?', (shop,))
    db_execute('DELETE FROM paypal_intents WHERE shop=?', (shop,))
    return True

def save_shop_token(shop, access_token, scope='', refresh_token='', expires_in=0, refresh_token_expires_in=0):
    now = int(time.time())
    try:
        expires_at = now + int(expires_in or 0)
    except (TypeError, ValueError):
        expires_at = 0
    try:
        refresh_token_expires_at = now + int(refresh_token_expires_in or 0)
    except (TypeError, ValueError):
        refresh_token_expires_at = 0
    db_execute('''INSERT INTO shop_tokens (shop, access_token, refresh_token, expires_at, refresh_token_expires_at, scope, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(shop) DO UPDATE SET
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at,
            refresh_token_expires_at=excluded.refresh_token_expires_at,
            scope=excluded.scope,
            updated_at=datetime('now')
    ''', (shop, access_token, refresh_token or '', expires_at, refresh_token_expires_at, scope or ''))

def refresh_shop_token(shop):
    shop = normalize_shop(shop)
    row = db_execute('SELECT refresh_token FROM shop_tokens WHERE shop=?', (shop,), fetchone=True)
    refresh_token = row[0] if row else ''
    if not refresh_token:
        return ''
    resp = requests.post(
        f'https://{shop}/admin/oauth/access_token',
        data={
            'client_id': SHOPIFY_CLIENT_ID,
            'client_secret': SHOPIFY_CLIENT_SECRET,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        },
        headers={'Accept': 'application/json'},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f'Shopify token refresh {resp.status_code}: {resp.text[:500]}')
    token_data = resp.json()
    access_token = token_data.get('access_token', '')
    if not access_token:
        raise RuntimeError('Shopify token refresh did not return access_token')
    save_shop_token(
        shop,
        access_token,
        token_data.get('scope', ''),
        token_data.get('refresh_token', refresh_token),
        token_data.get('expires_in', 0),
        token_data.get('refresh_token_expires_in', 0),
    )
    return access_token

def shopify_product_gid(product_id):
    product_id = str(product_id or '').strip()
    if product_id.startswith('gid://shopify/Product/'):
        return product_id
    if product_id.isdigit():
        return f'gid://shopify/Product/{product_id}'
    return product_id

def register_app_uninstalled_webhook(shop, access_token):
    shop = normalize_shop(shop)
    if not shop or not access_token:
        return False
    target = f'{APP_BASE_URL}/webhooks/app/uninstalled'
    mutation = """
    mutation RegisterAppUninstalledWebhook($topic: WebhookSubscriptionTopic!, $webhookSubscription: WebhookSubscriptionInput!) {
      webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
        webhookSubscription {
          id
          topic
          uri
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    try:
        data = shopify_graphql(
            shop,
            access_token,
            mutation,
            {
                'topic': 'APP_UNINSTALLED',
                'webhookSubscription': {'uri': target},
            },
        )
        payload = data.get('webhookSubscriptionCreate') or {}
        errors = payload.get('userErrors') or []
        if payload.get('webhookSubscription'):
            return True
        if errors and any('already' in (err.get('message', '').lower()) for err in errors):
            return True
        if errors:
            app.logger.warning('Shopify webhook registration returned errors for %s: %s', shop, errors)
    except Exception as exc:
        app.logger.warning('Failed to register app/uninstalled webhook for %s: %s', shop, exc)
    return False

def shopify_graphql(shop, access_token, query, variables=None):
    resp = requests.post(
        f'https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json',
        headers={
            'X-Shopify-Access-Token': access_token,
            'Content-Type': 'application/json',
        },
        json={'query': query, 'variables': variables or {}},
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f'Shopify GraphQL {resp.status_code}: {resp.text[:500]}')
    data = resp.json()
    if data.get('errors'):
        raise RuntimeError(json.dumps(data['errors'])[:500])
    return data.get('data') or {}

def fetch_shopify_admin_products(shop, limit=FREE_PRODUCT_LIMIT):
    shop = normalize_shop(shop)
    token = get_shop_token(shop)
    if not shop or not token:
        return []
    query = """
    query AiReadyProducts($first: Int!) {
      products(first: $first) {
        edges {
          node {
            id
            legacyResourceId
            title
            handle
            vendor
            descriptionHtml
            options {
              name
              values
            }
            variants(first: 20) {
              edges {
                node {
                  id
                  legacyResourceId
                  sku
                  barcode
                  price
                }
              }
            }
          }
        }
      }
    }
    """
    data = shopify_graphql(shop, token, query, {'first': limit})
    products = []
    for edge in (((data.get('products') or {}).get('edges')) or []):
        node = edge.get('node') or {}
        image_url = (((node.get('featuredMedia') or {}).get('preview') or {}).get('image') or {}).get('url')
        variants = []
        for variant_edge in (((node.get('variants') or {}).get('edges')) or []):
            variant = variant_edge.get('node') or {}
            variants.append({
                'id': str(variant.get('legacyResourceId') or variant.get('id') or ''),
                'admin_graphql_api_id': variant.get('id', ''),
                'sku': variant.get('sku') or '',
                'barcode': variant.get('barcode') or '',
                'price': str(variant.get('price') or ''),
                'available': True,
            })
        products.append({
            'id': str(node.get('legacyResourceId') or node.get('id') or ''),
            'admin_graphql_api_id': node.get('id', ''),
            'title': node.get('title') or '',
            'handle': node.get('handle') or '',
            'vendor': node.get('vendor') or '',
            'body_html': node.get('descriptionHtml') or '',
            'online_store_url': node.get('onlineStoreUrl') or '',
            'images': [{'src': image_url}] if image_url else [],
            'options': node.get('options') or [],
            'variants': variants,
        })
    return products

def mark_shop_paid(shop, source):
    db_execute('''INSERT INTO paid_shops (shop, paypal_txn_id, paid_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(shop) DO UPDATE SET
            paypal_txn_id=excluded.paypal_txn_id,
            paid_at=datetime('now')
    ''', (shop, source))

def clear_shopify_paid(shop):
    shop = normalize_shop(shop)
    if not shop:
        return
    db_execute("DELETE FROM paid_shops WHERE shop=? AND paypal_txn_id LIKE ?", (shop, 'shopify_subscription:%'))

def fetch_shopify_active_subscriptions(shop, token=None):
    shop = normalize_shop(shop)
    token = token or get_shop_token(shop)
    if not shop or not token:
        return []
    query = """
    query CurrentAppSubscriptions {
      currentAppInstallation {
        activeSubscriptions {
          id
          name
          status
          test
          currentPeriodEnd
          lineItems {
            plan {
              pricingDetails {
                __typename
                ... on AppRecurringPricing {
                  interval
                  price { amount currencyCode }
                }
              }
            }
          }
        }
      }
    }
    """
    data = shopify_graphql(shop, token, query)
    return ((data.get('currentAppInstallation') or {}).get('activeSubscriptions') or [])

def sync_shopify_billing_status(shop):
    shop = normalize_shop(shop)
    try:
        token = get_shop_token(shop)
    except Exception as exc:
        app.logger.warning('Failed to load Shopify token while syncing billing for %s: %s', shop, exc)
        return False
    if not shop or not token:
        return False
    try:
        subscriptions = fetch_shopify_active_subscriptions(shop, token)
    except Exception as exc:
        app.logger.warning('Failed to sync Shopify billing for %s: %s', shop, exc)
        return False
    for subscription in subscriptions:
        if subscription.get('name') != SHOPIFY_BILLING_NAME or subscription.get('status') != 'ACTIVE':
            continue
        for item in subscription.get('lineItems') or []:
            pricing = (((item.get('plan') or {}).get('pricingDetails')) or {})
            price = pricing.get('price') or {}
            try:
                amount = float(price.get('amount') or 0)
            except (TypeError, ValueError):
                amount = 0
            if (
                pricing.get('__typename') == 'AppRecurringPricing'
                and pricing.get('interval') == 'EVERY_30_DAYS'
                and price.get('currencyCode') == 'USD'
                and amount + 0.01 >= SHOPIFY_MONTHLY_PRICE
            ):
                mark_shop_paid(shop, 'shopify_subscription:' + subscription.get('id', 'monthly-subscription'))
                return True
    clear_shopify_paid(shop)
    return False

def get_shopify_subscription_summary(shop):
    shop = normalize_shop(shop)
    try:
        token = get_shop_token(shop)
        subscriptions = fetch_shopify_active_subscriptions(shop, token)
    except Exception as exc:
        app.logger.warning('Failed to load Shopify subscription summary for %s: %s', shop, exc)
        return {}
    for subscription in subscriptions:
        if subscription.get('name') == SHOPIFY_BILLING_NAME and subscription.get('status') == 'ACTIVE':
            return {
                'id': subscription.get('id', ''),
                'name': subscription.get('name', ''),
                'status': subscription.get('status', ''),
                'test': bool(subscription.get('test')),
                'current_period_end': subscription.get('currentPeriodEnd') or '',
            }
    return {}

def get_shop_token(shop):
    shop = normalize_shop(shop)
    row = db_execute('SELECT access_token, expires_at, refresh_token FROM shop_tokens WHERE shop=?', (shop,), fetchone=True)
    if not row:
        return ''
    token, expires_at, refresh_token = row[0], row[1] or 0, row[2] or ''
    try:
        expires_at = int(expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if refresh_token and expires_at and expires_at <= int(time.time()) + 120:
        return refresh_shop_token(shop)
    return token or ''

def get_shop_token_info(shop):
    row = db_execute('SELECT access_token, refresh_token, expires_at, refresh_token_expires_at, scope, updated_at FROM shop_tokens WHERE shop=?', (shop,), fetchone=True)
    if not row:
        return {'has_token': False, 'scope': '', 'updated_at': ''}
    token = row[0] or ''
    refresh_token = row[1] or ''
    return {
        'has_token': bool(token),
        'token_length': len(token),
        'token_tail': token[-6:] if token else '',
        'has_refresh_token': bool(refresh_token),
        'expires_at': row[2] or 0,
        'refresh_token_expires_at': row[3] or 0,
        'scope': row[4] or '',
        'updated_at': row[5] or '',
    }

def has_shop_token(shop):
    try:
        return bool(get_shop_token(shop))
    except Exception as exc:
        app.logger.warning('Failed to load Shopify token for %s: %s', normalize_shop(shop), exc)
        return False

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
  {% if shopify_client_id %}
  <meta name="shopify-api-key" content="{{ shopify_client_id }}"/>
  <script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
  {% endif %}
  <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js" defer></script>
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
    .topbar-links { margin-left: auto; display: flex; align-items: center; gap: 16px; }
    .topbar-link { color: #D2D5D8; text-decoration: none; font-size: 13px; }
    .topbar-link:hover { color: #fff; }
    .app-legal-footer { max-width: 900px; margin: -32px auto 32px; padding: 0 24px; display: flex; justify-content: center; gap: 18px; font-size: 13px; }
    .app-legal-footer a { color: var(--text-sub); text-decoration: none; }
    .app-legal-footer a:hover { color: var(--green); }

    /* PAGE */
    .page { max-width: 900px; margin: 0 auto; padding: 24px 24px 60px; }

    /* PAGE HEADER */
    .page-header { margin-bottom: 20px; }
    .feature-cards { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:20px; }
    .fcard { background:#fff; border:1px solid var(--border); border-radius:8px; padding:14px 16px; display:flex; gap:10px; align-items:flex-start; }
    .fcard-icon { font-size:18px; line-height:1.4; flex-shrink:0; }
    .fcard-title { font-size:13px; font-weight:600; color:var(--text); margin-bottom:2px; }
    .fcard-desc { font-size:12px; color:var(--text-sub); line-height:1.5; }
    @media(max-width:640px) { .feature-cards { grid-template-columns:1fr; } }
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

    /* CONVERSION HELPERS */
    .opportunity-card { background: #fff; border: 1px solid var(--green-border); border-radius: var(--radius); margin-bottom: 16px; box-shadow: var(--shadow); overflow: hidden; }
    .opportunity-head { background: var(--green-bg); padding: 14px 18px; border-bottom: 1px solid var(--green-border); display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap; }
    .opportunity-title { font-size: 14px; font-weight: 700; color: #005E45; }
    .opportunity-sub { font-size: 13px; color: var(--text-sub); margin-top: 2px; }
    .opportunity-body { padding: 16px 18px; display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
    .opportunity-stat { border:1px solid var(--border); border-radius:8px; padding:12px; background:#FAFAFA; }
    .opportunity-num { font-size:20px; font-weight:700; color:var(--text); line-height:1; margin-bottom:4px; }
    .opportunity-label { font-size:12px; color:var(--text-sub); line-height:1.4; }
    .preview-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 16px; box-shadow: var(--shadow); overflow:hidden; }
    .preview-head { padding:16px 20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap; }
    .preview-title { font-size:14px; font-weight:600; color:var(--text); }
    .preview-sub { font-size:13px; color:var(--text-sub); margin-top:2px; }
    .preview-body { padding:16px 20px; display:none; }
    .preview-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
    .preview-box { border:1px solid var(--border); border-radius:8px; background:#FAFAFA; overflow:hidden; }
    .preview-box-title { padding:8px 10px; background:#fff; border-bottom:1px solid var(--border); font-size:12px; font-weight:600; color:var(--text-sub); text-transform:uppercase; }
    .preview-copy { padding:12px; font-size:13px; color:var(--text); line-height:1.6; max-height:220px; overflow:auto; white-space:pre-wrap; }
    .preview-gain { margin-top:12px; background:var(--green-bg); border:1px solid var(--green-border); border-radius:8px; padding:12px; font-size:13px; color:#005E45; }

    /* USAGE BADGE */
    .usage-badge { display:inline-flex; align-items:center; gap:6px; background:var(--yellow-bg); border:1px solid var(--yellow-border); color:var(--yellow); border-radius:20px; padding:4px 12px; font-size:12px; font-weight:600; }
    .usage-badge.paid { background:var(--green-bg); border-color:var(--green-border); color:var(--green); }
    .plan-manager { display:flex; align-items:center; justify-content:space-between; gap:14px; border:1px solid var(--green-border); background:var(--green-bg); border-radius:10px; padding:14px 16px; margin-bottom:12px; flex-wrap:wrap; }
    .plan-manager-title { font-size:14px; font-weight:700; color:#005E45; margin-bottom:3px; }
    .plan-manager-copy { font-size:13px; color:var(--text-sub); line-height:1.45; }
    .plan-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .modal-plan-manager { justify-content:center; text-align:center; }
    .modal-plan-manager .plan-actions { width:100%; justify-content:center; }
    .btn-danger-outline { background:#fff; color:#D72C0D; border:1px solid #FDA29B; border-radius:7px; padding:9px 12px; font-size:13px; font-weight:600; cursor:pointer; font-family:inherit; }
    .btn-danger-outline:hover { background:#FFF4F2; }
    .plan-message { width:100%; font-size:13px; color:var(--text-sub); display:none; margin-top:2px; }

    /* UPGRADE MODAL */
    .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center; padding:20px; }
    .modal-overlay.visible { display:flex; }
    .modal-box { background:#fff; border-radius:12px; padding:32px; max-width:440px; width:90%; text-align:center; box-shadow:0 8px 40px rgba(0,0,0,0.18); position:relative; z-index:1001; pointer-events:auto; }
    .modal-icon { font-size:36px; margin-bottom:12px; }
    .modal-title { font-size:20px; font-weight:700; color:var(--text); margin-bottom:8px; }
    .modal-sub { font-size:14px; color:var(--text-sub); margin-bottom:24px; line-height:1.6; }
    .modal-price { font-size:32px; font-weight:800; color:var(--green); margin-bottom:4px; }
    .modal-price.is-hidden, .modal-price-sub.is-hidden, .modal-features.is-hidden, .pay-store-row.is-hidden { display:none; }
    .modal-price-cny { font-size:22px; color:var(--text-sub); font-weight:700; margin-left:6px; }
    .modal-price-sub { font-size:13px; color:var(--text-sub); margin-bottom:24px; }
    .modal-rate-note { font-size:12px; color:var(--text-hint); margin-top:-18px; margin-bottom:20px; }
    .modal-features { text-align:left; background:var(--green-bg); border:1px solid var(--green-border); border-radius:8px; padding:14px 18px; margin-bottom:24px; }
    .modal-feature { font-size:13px; color:#005E45; padding:3px 0; }
    .modal-close { margin-top:14px; font-size:13px; color:var(--text-hint); cursor:pointer; background:none; border:none; font-family:inherit; width:100%; padding:8px; }
    .modal-close:hover { color:var(--text-sub); text-decoration:underline; }
    .pay-btn { width:100%; padding:14px; font-size:15px; display:block; text-align:center; text-decoration:none; box-sizing:border-box; color:#fff; margin-bottom:12px; border:none; cursor:pointer; font-family:inherit; }
    .paypal-form { margin-bottom:12px; }
    .paypal-form input[type="image"] { width:100%; max-width:240px; height:auto; cursor:pointer; }
    .pay-divider { font-size:12px; color:var(--text-hint); margin:14px 0 10px; }
    .pay-option { background:#F6F6F7; border:1px solid var(--border); border-radius:8px; padding:12px; margin-bottom:10px; text-align:left; }
    .pay-option-title { font-size:13px; font-weight:600; color:var(--text); margin-bottom:6px; }
    .pay-option-id { font-size:14px; color:var(--green); font-weight:600; word-break:break-all; margin-bottom:8px; }
    .pay-qr { display:block; max-width:200px; width:100%; margin:0 auto 8px; border-radius:6px; }
    .pay-copy-btn { width:100%; padding:8px; font-size:13px; background:#fff; border:1px solid var(--border); border-radius:6px; cursor:pointer; color:var(--text); }
    .pay-copy-btn:hover { background:#F1F8F5; border-color:var(--green-border); }
    .pay-done-btn { width:100%; padding:12px; font-size:14px; background:#fff; border:1px solid var(--border-strong); border-radius:6px; cursor:pointer; color:var(--text); margin-top:4px; }
    .pay-done-btn:hover { background:var(--green-bg); border-color:var(--green); color:#005E45; }
    .pay-amount-row { text-align:left; margin-bottom:12px; }
    .pay-amount-label { font-size:12px; color:var(--text-sub); margin-bottom:6px; display:block; }
    .pay-amount-hint { font-size:11px; color:var(--text-hint); margin-top:6px; line-height:1.5; }
    .pay-store-row { text-align:left; margin-bottom:14px; }
    .success-banner { background:var(--green-bg); border:1px solid var(--green-border); color:#005E45; padding:14px 18px; border-radius:8px; margin-bottom:16px; font-size:14px; text-align:center; }
    body.paywall-open { overflow: hidden; }
    body.paywall-open:has(#upgradeModal.visible) .page { filter: blur(2px); }

    @media(max-width: 640px) {
      .metrics { grid-template-columns: 1fr; }
      .issue-name { min-width: 120px; }
      .page { padding: 16px 16px 40px; }
      .opportunity-body { grid-template-columns: 1fr; }
      .preview-grid { grid-template-columns: 1fr; }
      .topbar-links { gap: 10px; }
      .topbar-link { font-size: 12px; }
    }
  </style>
</head>
<body{% if open_upgrade %} class="paywall-open"{% endif %}>

<div class="topbar">
  <a href="/" style="text-decoration:none;"><div class="topbar-logo">Ai<span>Ready</span></div></a>
  <div class="topbar-badge">AI Readiness Checker</div>
  <div class="topbar-links">
    <a class="topbar-link" href="/privacy">Privacy</a>
    <a class="topbar-link" href="/terms">Terms</a>
  </div>
</div>

<div class="page">

  <div class="page-header">
    <div class="page-title">{% if open_upgrade %}Upgrade to Unlimited{% else %}AI Readiness Scanner{% endif %}</div>
    <div class="page-subtitle">Check how visible your Shopify products are to AI engines like ChatGPT, Perplexity, and Gemini.</div>
  </div>

  <div id="shopBanner" class="shop-banner">
    <div class="shop-banner-text">Store detected: <strong id="shopLabel"></strong></div>
    <button class="btn-primary" onclick="runShopScan()">Scan My Store</button>
  </div>

  <div class="scan-card">
    <form class="scan-row" id="scanForm">
      <input type="text" class="scan-input" id="storeUrl" name="url" placeholder="yourstore.myshopify.com or yourstore.com" value="{{ prefill_url or shop_prefill or '' }}" />
      <button type="submit" class="btn-primary" id="scanBtn">Scan Store</button>
    </form>
  </div>

  <div id="planStatusCard" style="display:none;"></div>
  <div id="unlimitedBanner" style="display:none;" class="success-banner"></div>
  <div class="feature-cards" id="featureCards">
    <div class="fcard"><div class="fcard-icon">&#128202;</div><div><div class="fcard-title">Score every product</div><div class="fcard-desc">13 structured data fields. See what&apos;s missing and the GEO score impact.</div></div></div>
    <div class="fcard"><div class="fcard-icon">&#129302;</div><div><div class="fcard-title">Generate GEO descriptions</div><div class="fcard-desc">One-click AI copy that ChatGPT, Perplexity &amp; Gemini can understand.</div></div></div>
    <div class="fcard"><div class="fcard-icon">&#128279;</div><div><div class="fcard-title">Save to Shopify</div><div class="fcard-desc">Connect your store and push fixes without leaving this page.</div></div></div>
  </div>
  <div id="results"></div>

</div><!-- end .page -->

<!-- UPGRADE MODAL -->
<div class="modal-overlay{% if open_upgrade %} visible{% endif %}" id="upgradeModal">
  <div class="modal-box">
    <div class="modal-icon">&#128274;</div>
    <div class="modal-title">Upgrade to Unlimited</div>
    <div class="modal-sub">{% if shopify_app_context %}Monthly Shopify billing unlocks unlimited AI fixes, descriptions, and saves for your store.{% elif open_upgrade %}One-time payment unlocks unlimited AI fixes, descriptions, and saves for your store.{% else %}Upgrade once to unlock unlimited AI fixes, descriptions, and saves for your store.{% endif %}</div>
    <div class="modal-price">{% if shopify_app_context %}${{ shopify_monthly_price }}{% else %}${{ paypal_price }}{% endif %}</div>
    <div class="modal-price-sub" id="billingSubtitle">{% if shopify_app_context %}per month via Shopify billing{% else %}one-time via PayPal - unlimited forever{% endif %}</div>
    <div class="pay-store-row">
      <label class="pay-amount-label">Store URL <span style="color:#D72C0D">*</span></label>
      <input type="text" id="upgradeStoreUrl" class="scan-input" placeholder="yourstore.myshopify.com" value="{{ shop_prefill or '' }}" oninput="var paypalShop=document.getElementById('paypalShop'); if (paypalShop) paypalShop.value=this.value; var shopifyBilling=document.getElementById('btnShopifyBilling'); if (shopifyBilling) shopifyBilling.href='/shopify/billing/approve?shop=' + encodeURIComponent(this.value)" />
    </div>
    <div class="modal-features">
      <div class="modal-feature">&#10003; &nbsp; Unlimited AI description generation</div>
      <div class="modal-feature">&#10003; &nbsp; Save directly to Shopify</div>
      <div class="modal-feature">&#10003; &nbsp; Bulk fix all products at once</div>
      <div class="modal-feature">&#10003; &nbsp; Weekly score reports via email</div>
    </div>
    <div id="modalPlanManager" style="display:none;"></div>
    <div id="modalStep1">
      <div id="shopifyBillingBox" style="display:{% if shopify_app_context %}block{% else %}none{% endif %};margin-bottom:12px;">
        <a id="btnShopifyBilling" href="/shopify/billing/approve?shop={{ shop_prefill|urlencode }}" target="_top" class="btn-primary" style="display:block;width:100%;padding:14px;font-size:15px;text-align:center;text-decoration:none;box-sizing:border-box;">Approve monthly plan in Shopify</a>
        <div class="pay-amount-hint">Opens Shopify's secure billing approval page in this tab.</div>
        <div id="billingError" style="display:none;margin-top:10px;color:#D72C0D;font-size:13px;line-height:1.45;"></div>
      </div>
      {% if not shopify_app_context %}
      <div id="paypalBillingBox">
        <form id="paypalForm" class="paypal-form" action="https://www.paypal.com/cgi-bin/webscr" method="post" target="_top" onsubmit="return handlePayPalSubmit(event)">
          <input type="hidden" name="cmd" value="_s-xclick" />
          <input type="hidden" name="hosted_button_id" value="{{ paypal_hosted_button_id }}" />
          <input type="hidden" name="currency_code" value="USD" />
          <input type="hidden" name="custom" id="paypalCustom" value="{{ shop_prefill or '' }}" />
          <input type="hidden" name="notify_url" value="{{ app_base_url }}/paypal/ipn" />
          <input type="hidden" name="return" id="paypalReturn" value="{{ app_base_url }}/paypal/return?shop={{ shop_prefill or '' }}" />
          <input type="hidden" name="cancel_return" id="paypalCancelReturn" value="{{ app_base_url }}/upgrade?shop={{ shop_prefill or '' }}" />
          <input type="hidden" name="cbt" value="Return to AiReady" />
          <input type="image" src="https://www.paypalobjects.com/en_US/i/btn/btn_buynowCC_LG.gif" border="0" name="submit" title="PayPal - The safer, easier way to pay online!" alt="Buy Now" style="width:100%;max-width:240px;height:auto;" />
        </form>
        <div class="pay-amount-hint">Fixed ${{ paypal_price }} USD via PayPal. You will return here after payment.</div>
        <button type="button" id="btnPaidStep" class="pay-done-btn">I paid on PayPal &rarr;</button>
      </div>
      {% endif %}
    </div>
    {% if not shopify_app_context %}
    <div id="modalStep2" style="display:none;margin-top:16px;">
      <p style="font-size:13px;color:var(--text-sub);margin-bottom:10px;">Enter the PayPal email you used to pay ${{ paypal_price }}:</p>
      <input type="email" id="unlockEmail" class="scan-input" placeholder="your@paypal.email" style="margin-bottom:8px;" />
      <button type="button" id="btnConfirmUnlock" class="btn-primary" style="width:100%;padding:12px;">Submit payment email</button>
      <div id="unlockMsg" style="margin-top:10px;font-size:13px;display:none;"></div>
    </div>
    <input type="hidden" id="paypalShop" value="{{ shop_prefill or '' }}">
    {% endif %}
    <button type="button" id="btnMaybeLater" class="modal-close">Maybe later</button>
  </div>
</div>

<div class="app-legal-footer">
  <a href="/privacy">Privacy Policy</a>
  <a href="/terms">Terms of Service</a>
</div>

<script>
/* Paywall controls load first so buttons work before the main app script */
function closeUpgradeModal() {
  var modal = document.getElementById('upgradeModal');
  if (modal) modal.classList.remove('visible');
  document.body.classList.remove('paywall-open');
  if (window.location.pathname === '/upgrade') {
    window.location.href = '/app';
    return;
  }
  try {
    var u = new URL(window.location.href);
    if (u.searchParams.get('upgrade') === '1') {
      u.searchParams.delete('upgrade');
      history.replaceState(null, '', u.pathname + (u.search || ''));
    }
  } catch (e) {}
}
function showPaidStep() {
  var s1 = document.getElementById('modalStep1');
  var s2 = document.getElementById('modalStep2');
  if (s1) s1.style.display = 'none';
  if (s2) s2.style.display = 'block';
  var email = document.getElementById('unlockEmail');
  if (email) email.focus();
}
function setBillingMode(useShopify) {
  var shopifyBox = document.getElementById('shopifyBillingBox');
  var paypalBox = document.getElementById('paypalBillingBox');
  var paidStep = document.getElementById('btnPaidStep');
  var subtitle = document.getElementById('billingSubtitle');
  if (shopifyBox) shopifyBox.style.display = useShopify ? 'block' : 'none';
  if (paypalBox) paypalBox.style.display = useShopify ? 'none' : 'block';
  if (paidStep) paidStep.style.display = useShopify ? 'none' : 'block';
  if (subtitle) subtitle.textContent = useShopify
    ? 'per month via Shopify billing'
    : '{% if shopify_app_context %}per month via Shopify billing{% else %}one-time via PayPal - unlimited forever{% endif %}';
}
async function getShopifySessionToken() {
  if (!window.shopify || typeof window.shopify.idToken !== 'function') return '';
  try {
    if (typeof window.shopify.ready === 'function') {
      await window.shopify.ready();
    }
    return await Promise.race([
      window.shopify.idToken(),
      new Promise(function(resolve) { setTimeout(function() { resolve(''); }, 1500); })
    ]);
  } catch (e) {
    return '';
  }
}
async function appFetch(url, options) {
  var opts = options || {};
  var headers = new Headers(opts.headers || {});
  var token = await getShopifySessionToken();
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', 'Bearer ' + token);
  }
  opts.headers = headers;
  return fetch(url, opts);
}
async function verifyEmbeddedSessionToken() {
  try {
    var token = await getShopifySessionToken();
    if (!token) return;
    var res = await appFetch('/api/session-token-check', {method: 'POST'});
    var data = await res.json();
    if (data && data.shop) {
      window.currentShopHasToken = true;
      var storeInput = document.getElementById('storeUrl');
      var upgradeInput = document.getElementById('upgradeStoreUrl');
      var banner = document.getElementById('shopBanner');
      var label = document.getElementById('shopLabel');
      if (storeInput && !storeInput.value.trim()) storeInput.value = data.shop;
      if (upgradeInput && !upgradeInput.value.trim()) upgradeInput.value = data.shop;
      if (label) label.textContent = data.shop;
      if (banner) banner.classList.add('visible');
      setBillingMode(true);
      refreshPlanStatus(data.shop);
    }
  } catch (e) {}
}
async function refreshBillingMode(shop) {
  var useShopify = !!window.currentShopHasToken;
  var usage = null;
  if (shop) {
    try {
      var res = await appFetch('/api/usage?shop=' + encodeURIComponent(shop));
      usage = cacheBillingUsage(await res.json(), shop);
      useShopify = useShopify || !!usage.has_token;
    } catch(e) {}
  }
  if (usage && usage.paid) {
    renderPaidBillingMode(usage.shop || shop, usage);
    return;
  }
  renderUpgradeBillingMode(useShopify);
}

function renderPaidBillingMode(shop, usage) {
  var planBox = document.getElementById('modalPlanManager');
  var step1 = document.getElementById('modalStep1');
  var icon = document.querySelector('#upgradeModal .modal-icon');
  var title = document.querySelector('#upgradeModal .modal-title');
  var sub = document.querySelector('#upgradeModal .modal-sub');
  var price = document.querySelector('#upgradeModal .modal-price');
  var priceSub = document.getElementById('billingSubtitle');
  var features = document.querySelector('#upgradeModal .modal-features');
  var storeRow = document.querySelector('#upgradeModal .pay-store-row');
  if (icon) icon.innerHTML = '&#10003;';
  if (title) title.textContent = 'Manage subscription';
  if (sub) sub.textContent = 'Your Shopify Billing plan is active. You can keep using AiReady Unlimited or switch back to the free plan.';
  if (price) price.classList.add('is-hidden');
  if (priceSub) priceSub.classList.add('is-hidden');
  if (features) features.classList.add('is-hidden');
  if (storeRow) storeRow.classList.add('is-hidden');
  if (planBox) {
    planBox.innerHTML = billingPlanManagementHtml((usage && usage.shop) || shop, usage || {});
    planBox.style.display = 'block';
  }
  if (step1) step1.style.display = 'none';
  setBillingMode(true);
}

function renderBillingCheckMode(shop) {
  renderPaidBillingMode(shop, {shop: shop, current_period_end: ''});
  var title = document.querySelector('#upgradeModal .modal-title');
  var sub = document.querySelector('#upgradeModal .modal-sub');
  var planBox = document.getElementById('modalPlanManager');
  if (title) title.textContent = 'Checking plan';
  if (sub) sub.textContent = 'Checking your Shopify Billing status.';
  if (planBox) {
    planBox.innerHTML = '<div class="plan-manager modal-plan-manager"><div><div class="plan-manager-title">Checking subscription...</div><div class="plan-manager-copy">This should only take a moment.</div></div></div>';
  }
}

function renderUpgradeBillingMode(useShopify) {
  var planBox = document.getElementById('modalPlanManager');
  var step1 = document.getElementById('modalStep1');
  var icon = document.querySelector('#upgradeModal .modal-icon');
  var title = document.querySelector('#upgradeModal .modal-title');
  var sub = document.querySelector('#upgradeModal .modal-sub');
  var price = document.querySelector('#upgradeModal .modal-price');
  var priceSub = document.getElementById('billingSubtitle');
  var features = document.querySelector('#upgradeModal .modal-features');
  var storeRow = document.querySelector('#upgradeModal .pay-store-row');
  if (icon) icon.innerHTML = '&#128274;';
  if (title) title.textContent = 'Upgrade to Unlimited';
  if (sub) sub.textContent = useShopify
    ? 'Monthly Shopify billing unlocks unlimited AI fixes, descriptions, and saves for your store.'
    : 'Upgrade to scan more products and unlock unlimited AI fixes, descriptions, and saves for your store.';
  if (price) price.classList.remove('is-hidden');
  if (priceSub) priceSub.classList.remove('is-hidden');
  if (features) features.classList.remove('is-hidden');
  if (storeRow) storeRow.classList.remove('is-hidden');
  if (planBox) {
    planBox.innerHTML = '';
    planBox.style.display = 'none';
  }
  if (step1) step1.style.display = 'block';
  setBillingMode(useShopify);
}
function showUpgradeModal(shop, mode) {
  var modal = document.getElementById('upgradeModal');
  if (!modal) return;
  var selectedShop = shop || getPaywallShop();
  var shopInput = document.getElementById('paypalShop');
  if (shopInput) shopInput.value = selectedShop || '';
  var storeUrl = document.getElementById('upgradeStoreUrl');
  if (storeUrl && selectedShop) storeUrl.value = selectedShop;
  var cachedUsage = getCachedBillingUsage(selectedShop);
  if (mode === 'manage') {
    if (cachedUsage && cachedUsage.paid) {
      renderPaidBillingMode(cachedUsage.shop || selectedShop, cachedUsage);
    } else {
      renderBillingCheckMode(selectedShop);
    }
    refreshBillingMode(selectedShop);
    modal.classList.add('visible');
    document.body.classList.add('paywall-open');
    return;
  }
  var fromPricing = new URLSearchParams(window.location.search).get('upgrade') === '1'
    || window.location.pathname === '/upgrade';
  var title = document.querySelector('#upgradeModal .modal-title');
  var sub = document.querySelector('#upgradeModal .modal-sub');
  if (title && sub) {
    if (fromPricing) {
      title.textContent = 'Upgrade to Unlimited';
      sub.textContent = 'One-time payment unlocks unlimited AI fixes, descriptions, and saves for your store.';
    } else {
      title.textContent = 'Manage plan';
      sub.textContent = 'Review your current plan or choose the plan that fits your store.';
    }
  }
  var step1 = document.getElementById('modalStep1');
  var step2 = document.getElementById('modalStep2');
  if (step1) step1.style.display = 'block';
  if (step2) step2.style.display = 'none';
  refreshBillingMode(selectedShop);
  modal.classList.add('visible');
  document.body.classList.add('paywall-open');
}
function getPaywallShop() {
  var el = document.getElementById('upgradeStoreUrl');
  if (el && el.value.trim()) return el.value.trim();
  var hidden = document.getElementById('paypalShop');
  if (hidden && hidden.value.trim()) return hidden.value.trim();
  var scan = document.getElementById('storeUrl');
  if (scan && scan.value.trim()) return scan.value.trim();
  return '';
}
{% if not shopify_app_context %}
async function handlePayPalSubmit(e) {
  e.preventDefault();
  const shop = getPaywallShop();
  if (!shop) {
    alert('Please enter your store URL first (e.g. yourstore.myshopify.com).');
    return false;
  }
  try {
    await appFetch('/paypal/register-intent', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({shop})
    });
  } catch (err) {}
  var normalizedShop = shop.replace(new RegExp('^https?://'), '').split('/')[0].trim().toLowerCase();
  if (normalizedShop && normalizedShop.indexOf('.myshopify.com') === -1) {
    normalizedShop += '.myshopify.com';
  }
  var custom = document.getElementById('paypalCustom');
  var ret = document.getElementById('paypalReturn');
  var cancel = document.getElementById('paypalCancelReturn');
  if (custom) custom.value = normalizedShop;
  if (ret) ret.value = '{{ app_base_url }}/paypal/return?shop=' + encodeURIComponent(normalizedShop);
  if (cancel) cancel.value = '{{ app_base_url }}/upgrade?shop=' + encodeURIComponent(normalizedShop);
  document.getElementById('paypalForm').submit();
  return false;
}
async function submitUnlockRequest() {
  const email = document.getElementById('unlockEmail').value.trim();
  const shop = getPaywallShop();
  const msg = document.getElementById('unlockMsg');
  if (!shop) { alert('Please enter your store URL (e.g. yourstore.myshopify.com).'); return; }
  if (!email) { alert('Please enter the PayPal email you used to pay.'); return; }
  const btn = document.getElementById('btnConfirmUnlock');
  if (btn) { btn.disabled = true; btn.textContent = 'Unlocking...'; }
  try {
    const res = await appFetch('/request-unlock', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, shop, method: 'paypal'})
    });
    const data = await res.json();
    if (data.success && data.redirect) {
      window.location.href = data.redirect;
      return;
    }
    if (data.error) {
      msg.style.display = 'block';
      msg.style.color = 'var(--red)';
      msg.textContent = data.error;
      if (btn) { btn.disabled = false; btn.textContent = 'Submit payment email'; }
      return;
    }
    msg.style.display = 'block';
    msg.style.color = 'var(--green)';
    msg.textContent = 'Request received. We will verify your PayPal payment and unlock your store shortly.';
    if (btn) { btn.disabled = false; btn.textContent = 'Submit payment email'; }
  } catch(e) {
    msg.style.display = 'block';
    msg.style.color = 'var(--red)';
    msg.textContent = 'Error sending request. Please email us directly.';
    if (btn) { btn.disabled = false; btn.textContent = 'Submit payment email'; }
  }
}
{% endif %}
function bindPaywallButtons() {
  var later = document.getElementById('btnMaybeLater');
  var paid = document.getElementById('btnPaidStep');
  var confirmBtn = document.getElementById('btnConfirmUnlock');
  var modal = document.getElementById('upgradeModal');
  if (later) later.addEventListener('click', function(e) { e.preventDefault(); closeUpgradeModal(); });
  if (paid) paid.addEventListener('click', function(e) { e.preventDefault(); showPaidStep(); });
  if (confirmBtn) confirmBtn.addEventListener('click', function(e) { e.preventDefault(); submitUnlockRequest(); });
  if (modal) modal.addEventListener('click', function(e) { if (e.target === modal) closeUpgradeModal(); });
  try {
    var params = new URLSearchParams(window.location.search);
    if (window.top !== window.self || params.get('host')) {
      window.currentShopHasToken = true;
      setBillingMode(true);
    }
  } catch(e) {}
  if (modal && modal.classList.contains('visible')) refreshBillingMode(getPaywallShop());
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bindPaywallButtons);
} else {
  bindPaywallButtons();
}
</script>

<script>
function runShopScan() {
  var input = document.getElementById('storeUrl');
  var label = document.getElementById('shopLabel');
  if (input && !input.value.trim() && label && label.textContent.trim()) {
    input.value = label.textContent.trim();
  }
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

var lastData = null;
window.currentShopHasToken = false;
window.billingUsageCache = window.billingUsageCache || {};

function scoreClass(s) {
  if (s >= 70) return 'score-high';
  if (s >= 40) return 'score-mid';
  return 'score-low';
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function jsArg(value) {
  return escapeHtml(JSON.stringify(value == null ? '' : value));
}

function usageCacheKey(shop) {
  return String(shop || '').trim().toLowerCase();
}

function cacheBillingUsage(usage, fallbackShop) {
  var key = usageCacheKey((usage && usage.shop) || fallbackShop || '');
  if (key && usage) window.billingUsageCache[key] = usage;
  return usage;
}

function getCachedBillingUsage(shop) {
  return window.billingUsageCache[usageCacheKey(shop)] || null;
}

function formatBillingDate(value) {
  if (!value) return '';
  try {
    return new Date(value).toLocaleDateString('en-US', {year: 'numeric', month: 'short', day: 'numeric'});
  } catch (e) {
    return String(value).slice(0, 10);
  }
}

function planManagerHtml(shop, usage) {
  var safeShop = escapeHtml(shop || 'your store');
  var shopArg = jsArg(shop || '');
  var periodEnd = formatBillingDate(usage && (usage.current_period_end || (usage.subscription || {}).current_period_end));
  var periodCopy = periodEnd ? ` Current billing period ends on ${escapeHtml(periodEnd)}.` : '';
  if (usage && usage.has_token) {
    return `<div class="plan-manager">
      <div>
        <div class="plan-manager-title">&#10003; Current plan: AiReady Unlimited</div>
        <div class="plan-manager-copy">$${escapeHtml('{{ shopify_monthly_price }}')} per month through Shopify Billing for ${safeShop}.${periodCopy}</div>
      </div>
      <div class="plan-actions">
        <button type="button" class="btn-secondary" onclick="showUpgradeModal(${shopArg}, 'manage')">Manage plan</button>
      </div>
      <div class="plan-message" id="planMessage-${escapeHtml((shop || '').replace(/[^a-z0-9]/gi, '-'))}"></div>
    </div>`;
  }
  return `<div class="plan-manager">
    <div>
      <div class="plan-manager-title">&#10003; Unlimited plan active</div>
      <div class="plan-manager-copy">Unlimited access is active for ${safeShop}. Reconnect Shopify from the app to manage Shopify Billing changes.</div>
    </div>
    <div class="plan-actions">
      <a class="btn-secondary" href="/install?shop=${encodeURIComponent(shop || '')}&force=1" style="text-decoration:none;">Reconnect Shopify</a>
    </div>
  </div>`;
}

function billingPlanManagementHtml(shop, usage) {
  var safeShop = escapeHtml(shop || 'your store');
  var shopArg = jsArg(shop || '');
  var periodEnd = formatBillingDate(usage && (usage.current_period_end || (usage.subscription || {}).current_period_end));
  var periodCopy = periodEnd
    ? `Your current billing period ends on ${escapeHtml(periodEnd)}. If you downgrade, Shopify will stop future renewals for this subscription.`
    : 'If you downgrade, Shopify will stop future renewals for this subscription.';
  return `<div class="plan-manager modal-plan-manager">
    <div>
      <div class="plan-manager-title">&#10003; Current plan: AiReady Unlimited</div>
      <div class="plan-manager-copy">$${escapeHtml('{{ shopify_monthly_price }}')} per month through Shopify Billing for ${safeShop}. ${periodCopy}</div>
    </div>
    <div class="plan-actions">
      <button type="button" class="btn-danger-outline" onclick="cancelShopifySubscription(${shopArg}, this)">Downgrade to Free</button>
    </div>
  </div>`;
}

function freePlanHtml(shop) {
  var safeShop = escapeHtml(shop || 'your store');
  var shopArg = jsArg(shop || '');
  return `<div class="plan-manager">
    <div>
      <div class="plan-manager-title">Current plan: Free</div>
      <div class="plan-manager-copy">Free scan: up to {{ free_product_limit }} products for ${safeShop}. Upgrade through Shopify Billing when you need more scans and AI fixes.</div>
    </div>
    <div class="plan-actions">
      <button type="button" class="btn-primary" onclick="showUpgradeModal(${shopArg})">Upgrade plan</button>
    </div>
  </div>`;
}

async function refreshPlanStatus(shop) {
  var card = document.getElementById('planStatusCard');
  if (!card) return;
  var normalizedShop = (shop || getPaywallShop() || '').trim();
  if (!normalizedShop) {
    card.style.display = 'none';
    card.innerHTML = '';
    return;
  }
  try {
    var res = await appFetch('/api/usage?shop=' + encodeURIComponent(normalizedShop));
    var usage = cacheBillingUsage(await res.json(), normalizedShop);
    if (!res.ok || usage.error) {
      card.style.display = 'none';
      card.innerHTML = '';
      return;
    }
    card.innerHTML = usage.paid ? planManagerHtml(usage.shop || normalizedShop, usage) : freePlanHtml(usage.shop || normalizedShop);
    card.style.display = 'block';
  } catch (e) {
    card.style.display = 'none';
    card.innerHTML = '';
  }
}

async function shouldAutoOpenUpgrade(shop) {
  var normalizedShop = (shop || getPaywallShop() || '').trim();
  if (!normalizedShop) return true;
  try {
    var res = await appFetch('/api/usage?shop=' + encodeURIComponent(normalizedShop));
    var usage = await res.json();
    if (res.ok && usage && usage.paid) {
      closeUpgradeModal();
      refreshPlanStatus(usage.shop || normalizedShop);
      return false;
    }
  } catch (e) {}
  return true;
}

async function cancelShopifySubscription(shop, button) {
  var normalizedShop = (shop || getPaywallShop() || '').trim();
  if (!normalizedShop) {
    alert('Please enter your store URL first.');
    return;
  }
  var planText = '';
  var manager = button ? button.closest('.plan-manager') : null;
  var copy = manager ? manager.querySelector('.plan-manager-copy') : null;
  if (copy) planText = String.fromCharCode(10, 10) + copy.textContent.trim();
  if (!confirm('Downgrade this store to the free plan and cancel future Shopify Billing renewals?' + planText)) return;
  var oldText = button ? button.textContent : '';
  if (button) {
    button.disabled = true;
    button.textContent = 'Changing plan...';
  }
  try {
    var res = await appFetch('/shopify/billing/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({shop: normalizedShop})
    });
    var data = await res.json();
    if (!res.ok || data.error) {
      if (button) {
        button.disabled = false;
        button.textContent = oldText || 'Downgrade to Free';
      }
      if (data.redirect) {
        window.location.href = data.redirect;
        return;
      }
      alert(data.error || 'Could not change the plan. Please try again.');
      return;
    }
    refreshPlanStatus(normalizedShop);
    window.location.href = data.redirect || ('/app?shop=' + encodeURIComponent(normalizedShop) + '&plan=free');
  } catch (err) {
    if (button) {
      button.disabled = false;
      button.textContent = oldText || 'Downgrade to Free';
    }
    alert('Could not change the plan. Please try again.');
  }
}

function estimateManualMinutes(products) {
  const issueCount = (products || []).reduce((sum, p) => sum + ((p.missing || []).length), 0);
  return Math.max(10, Math.round(issueCount * 4));
}

function findPreviewProduct(products) {
  const candidates = (products || []).filter(p => (p.missing || []).length > 0);
  if (!candidates.length) return (products || [])[0] || null;
  return candidates.sort((a, b) => a.score - b.score)[0];
}

function toggleFix(el) {
  el.classList.toggle('expanded');
}

window.renderResults = function renderResults(data) {
  var fc = document.getElementById('featureCards');
  if (fc) fc.style.display = 'none';
  lastData = data;
  window.lastData = data;
  window.currentShopHasToken = !!(data.summary && data.summary.has_token);
  const results = document.getElementById('results');
  const s = data.summary;
  refreshPlanStatus(s.store);
  const totalIssues = data.products.reduce((a,p) => a + (p.missing || []).length, 0);
  const maxCount = s.top_issues.length ? s.top_issues[0].count : 1;
  const scoreCol = s.avg_score >= 70 ? 'metric-green' : s.avg_score >= 40 ? 'metric-yellow' : 'metric-red';

  // Priority fixes
  const weightMap = {};
  for (const p of data.products) {
    for (const f of (p.missing || [])) {
      if (!weightMap[f.label] || weightMap[f.label].weight < f.weight) weightMap[f.label] = f;
    }
  }
  const priorityFixes = Object.values(weightMap).sort((a,b) => b.weight - a.weight).slice(0,3);

  // Usage badge (fetch async, inject after render)
  appFetch('/api/usage?shop=' + encodeURIComponent(s.store))
    .then(r => r.json())
    .then(u => {
      const el = document.getElementById('usageBadge');
      if (!el) return;
      if (u.paid) {
        el.innerHTML = '';
        el.style.display = 'none';
      } else {
        const limit = u.free_product_limit || 20;
        el.style.display = 'block';
        el.innerHTML = `<span class="usage-badge">Free scan: up to ${limit} products &mdash; <a href="#" onclick="showUpgradeModal(${jsArg(s.store)});return false;" style="color:var(--yellow);text-decoration:underline;">View plan</a></span>`;
      }
    }).catch(() => {});

  let html = `<div id="usageBadge" style="display:none;margin-bottom:12px;"></div>
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

  const manualMinutes = estimateManualMinutes(data.products);
  const lowScoreProducts = data.products.filter(p => p.score < 50).length;
  const previewProduct = findPreviewProduct(data.products);
  html += `<div class="opportunity-card">
    <div class="opportunity-head">
      <div>
        <div class="opportunity-title">What this scan means for your store</div>
        <div class="opportunity-sub">Turn the score into concrete fixes merchants can understand.</div>
      </div>
      <button class="btn-primary" onclick="previewFirstFix(this)">Preview 1 AI Fix</button>
    </div>
    <div class="opportunity-body">
      <div class="opportunity-stat">
        <div class="opportunity-num">${lowScoreProducts}</div>
        <div class="opportunity-label">products need urgent AI visibility work</div>
      </div>
      <div class="opportunity-stat">
        <div class="opportunity-num">~${manualMinutes} min</div>
        <div class="opportunity-label">estimated manual cleanup time</div>
      </div>
      <div class="opportunity-stat">
        <div class="opportunity-num">${s.top_issues.length ? escapeHtml(s.top_issues[0].field) : 'Schema'}</div>
        <div class="opportunity-label">highest priority issue to fix first</div>
      </div>
    </div>
  </div>`;

  if (previewProduct) {
    html += `<div class="preview-card" id="fixPreviewCard">
      <div class="preview-head">
        <div>
          <div class="preview-title">Free AI repair preview</div>
          <div class="preview-sub">See one product before and after before upgrading.</div>
        </div>
        <button class="btn-secondary" onclick="previewFirstFix(this)">Generate Preview</button>
      </div>
      <div class="preview-body" id="fixPreviewBody"></div>
    </div>`;
  }

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
    const present = p.present || [];
    const missing = p.missing || [];
    const passCount = present.length;
    const failCount = missing.length;
    const sanitize = s => (s||'').replace(/\\n|\\r/g,' ').replace(/&[a-z]+;/g,'').slice(0,200);
    const en = sanitize(p.name).slice(0,60);
    const eb = sanitize(p.brand).slice(0,40);
    const ed = sanitize(p.description || '');
    const ml = missing.map(f => (f.label || f).replace(/['"`]/g,'')).join('|');

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
    for (const f of present) {
      const label = typeof f === 'string' ? f : (f.label || '');
      html += `<span class="chip chip-ok">${escapeHtml(label)}</span>`;
    }
    for (const f of missing) {
      const flabel = typeof f === 'string' ? f : (f.label || '');
      const hint = FIX_HINTS[flabel] || 'Add this field to improve AI discoverability';
      html += `<span class="chip chip-miss" title="${escapeHtml(hint)}">${escapeHtml(flabel)} - missing</span>`;
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
    <h3>Email this report to yourself</h3>
    <p>Get the current scan summary now, then receive weekly score changes.</p>
    <div class="email-row">
      <input type="email" id="subEmail" class="email-input" placeholder="your@email.com" />
      <button class="btn-primary" onclick="subscribe(${jsArg(shopDomain)})">Send Report</button>
    </div>
    <div id="subMsg" style="margin-top:10px;font-size:13px;color:#95BF47;display:none;"></div>
  </div>`;

  results.innerHTML = html;
};

function toggleRow(idx) {
  const row = document.getElementById('detail-' + idx);
  const productRows = document.querySelectorAll('.product-row');
  if (!row || !productRows[idx]) return;
  if (row.classList.contains('visible')) {
    row.classList.remove('visible');
    productRows[idx].classList.remove('expanded');
  } else {
    row.classList.add('visible');
    productRows[idx].classList.add('expanded');
  }
}
window.toggleRow = toggleRow;

function shareScore(score, store) {
  const text = `My Shopify store scored ${score}/100 on AI Readiness - meaning AI engines like ChatGPT and Perplexity may not be recommending my products. Check your store free: {{ app_base_url }}`;
  navigator.clipboard.writeText(text).then(() => {
    alert('Score text copied! Paste it anywhere to share.');
  }).catch(() => {
    prompt('Copy this:', text);
  });
}

window.analyzeContent = async function analyzeContent(btn, name, brand, description) {
  const cell = btn.closest('.detail-cell');
  const resultBox = cell.querySelector('.analyze-result');
  btn.disabled = true;
  btn.textContent = 'Analyzing...';
  resultBox.style.display = 'block';
  resultBox.innerHTML = '<span class="spinner"></span> Running GEO content analysis...';

  try {
    const res = await appFetch('/analyze', {
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

window.generateDesc = async function generateDesc(btn, name, brand, description, missingLabels) {
  const cell = btn.closest('.detail-cell');
  const resultBox = cell.querySelector('.generate-result');
  btn.disabled = true;
  btn.textContent = 'Generating...';
  resultBox.style.display = 'block';
  resultBox.innerHTML = '<span class="spinner"></span> Writing optimized description...';

  const missing = missingLabels ? missingLabels.split('|').filter(Boolean) : [];

  const shop = lastData && lastData.summary ? lastData.summary.store : '';
  try {
    const res = await appFetch('/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, brand, description, missing, shop})
    });
    const data = await res.json();
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

window.previewFirstFix = async function previewFirstFix(btn) {
  if (!lastData || !lastData.products || !lastData.products.length) {
    alert('Please scan a store first.');
    return;
  }
  const product = findPreviewProduct(lastData.products);
  if (!product) return;
  const body = document.getElementById('fixPreviewBody');
  if (!body) return;
  const shop = lastData.summary ? lastData.summary.store : '';
  const oldText = product.description || 'No useful product description was found on this product.';
  const missing = (product.missing || []).map(f => f.label || f).filter(Boolean);
  const oldScore = product.score || 0;
  const estimatedScore = Math.min(96, oldScore + missing.slice(0, 4).reduce((sum, label) => {
    const hit = (product.missing || []).find(f => (f.label || f) === label);
    return sum + (hit && hit.weight ? hit.weight : 6);
  }, 0));

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Generating...';
  }
  body.style.display = 'block';
  body.innerHTML = '<span class="spinner"></span> Creating a before/after preview...';

  try {
    const res = await appFetch('/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: product.name || '',
        brand: product.brand || '',
        description: product.description || '',
        missing,
        shop
      })
    });
    const data = await res.json();
    if (data.error) {
      body.innerHTML = `<div class="error-banner">${escapeHtml(data.error)}</div>`;
      return;
    }
    body.innerHTML = `<div class="preview-grid">
      <div class="preview-box">
        <div class="preview-box-title">Before - ${escapeHtml(product.name || 'Product')}</div>
        <div class="preview-copy">${escapeHtml(oldText)}</div>
      </div>
      <div class="preview-box">
        <div class="preview-box-title">After - AI optimized</div>
        <div class="preview-copy">${escapeHtml(data.description || '')}</div>
      </div>
    </div>
    <div class="preview-gain">
      Estimated score lift: <strong>${oldScore}/100 -> ${estimatedScore}/100</strong>.
      Fixes targeted: ${missing.slice(0, 4).map(escapeHtml).join(', ') || 'description quality'}.
    </div>`;
  } catch(e) {
    body.innerHTML = '<div class="error-banner">Could not generate the preview. Please try again.</div>';
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Generate Preview';
    }
  }
}

async function saveToShopify(btn, productId, shop, description) {
  if (!productId || !shop) { alert('Store not connected. Install the app via Shopify to enable saving.'); return; }
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const res = await appFetch('/api/update_product', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({product_id: productId, shop, description})
    });
    const data = await res.json();
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
    const res = await appFetch('/api/update_vendor', {
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
      const genRes = await appFetch('/generate', {
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
      if (genData.description && p.product_id) {
        const saveRes = await appFetch('/api/update_product', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({product_id: p.product_id, shop, description: genData.description})
        });
        const saveData = await saveRes.json();
        if (saveData.success) {
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

async function subscribe(shop) {
  const email = document.getElementById('subEmail').value.trim();
  if (!email) { alert('Please enter your email.'); return; }
  if (!shop) { alert('Please scan a store first.'); return; }
  const msg = document.getElementById('subMsg');
  const payload = {email, shop};
  const params = new URLSearchParams(window.location.search);
  payload.source = params.get('source') || params.get('utm_source') || params.get('ref') || '';
  if (lastData && lastData.summary) {
    payload.summary = lastData.summary;
    payload.products = (lastData.products || []).slice(0, 10).map(p => ({
      name: p.name,
      score: p.score,
      missing: (p.missing || []).map(f => f.label || f).slice(0, 5)
    }));
  }
  try {
    const res = await appFetch('/subscribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success) {
      msg.style.display = 'block';
      msg.textContent = data.sent
        ? 'Report sent. Weekly score tracking is now enabled.'
        : 'Saved. Weekly score tracking is now enabled.';
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
  doc.text('Generated by AiReady - {{ app_base_url }}', 20, 290);

  doc.save('aiready-report-' + s.store + '-' + date.split('/').join('-') + '.pdf');
}

function scanEsc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function renderBasicResults(data) {
  var results = document.getElementById('results');
  if (!results || !data || !data.summary) return;
  lastData = data;
  window.lastData = data;
  var s = data.summary;
  refreshPlanStatus(s.store);
  var products = data.products || [];
  var totalIssues = 0;
  var i, p, sc, passCount, failCount;
  for (i = 0; i < products.length; i++) totalIssues += (products[i].missing || []).length;
  var scoreCol = s.avg_score >= 70 ? 'metric-green' : (s.avg_score >= 40 ? 'metric-yellow' : 'metric-red');
  var html = '<div class="metrics">' +
    '<div class="metric-card"><div class="metric-value">' + s.total_products + '</div><div class="metric-label">Products scanned</div></div>' +
    '<div class="metric-card"><div class="metric-value ' + scoreCol + '">' + s.avg_score + '<span style="font-size:16px;font-weight:400;color:var(--text-sub)">/100</span></div><div class="metric-label">Average AI Readiness Score</div></div>' +
    '<div class="metric-card"><div class="metric-value metric-red">' + totalIssues + '</div><div class="metric-label">Total missing fields</div></div></div>';
  if (s.top_issues && s.top_issues.length) {
    html += '<div class="card"><div class="card-header"><div class="card-title">Most common issues</div></div><div class="card-body">';
    for (i = 0; i < s.top_issues.length; i++) {
      var issue = s.top_issues[i];
      html += '<div class="issue-row"><span class="issue-name">' + scanEsc(issue.field) + '</span>' +
        '<span class="issue-count">' + issue.count + ' of ' + s.total_products + ' products</span></div>';
    }
    html += '</div></div>';
  }
  html += '<div class="card"><div class="card-header"><div class="card-title">Product breakdown</div></div>' +
    '<table class="product-table"><thead><tr><th>Product</th><th>Score</th><th>Fields</th></tr></thead><tbody>';
  for (i = 0; i < products.length; i++) {
    p = products[i];
    var present = p.present || [];
    var missing = p.missing || [];
    sc = p.score >= 70 ? 'score-high' : (p.score >= 40 ? 'score-mid' : 'score-low');
    passCount = present.length;
    failCount = missing.length;
    html += '<tr class="product-row" data-idx="' + i + '"><td><div class="product-name-cell">' + scanEsc(p.name) +
      '<span class="expand-icon">&#9654;</span></div><div class="product-url-cell">' + scanEsc(p.url) + '</div></td>' +
      '<td><span class="score-pill ' + sc + '">' + p.score + '/100</span></td>' +
      '<td><span style="color:var(--green);font-size:13px;font-weight:500;">' + passCount + ' passed</span> &nbsp; ' +
      '<span style="color:var(--red);font-size:13px;">' + failCount + ' missing</span></td></tr>' +
      '<tr class="detail-row" id="detail-' + i + '"><td colspan="3" class="detail-cell"><div class="chips">';
    var j, flabel;
    for (j = 0; j < present.length; j++) {
      flabel = typeof present[j] === 'string' ? present[j] : (present[j].label || '');
      html += '<span class="chip chip-ok">' + scanEsc(flabel) + '</span>';
    }
    for (j = 0; j < missing.length; j++) {
      flabel = typeof missing[j] === 'string' ? missing[j] : (missing[j].label || '');
      html += '<span class="chip chip-miss">' + scanEsc(flabel) + ' - missing</span>';
    }
    html += '</div><div class="detail-actions">' +
      '<button type="button" class="btn-secondary btn-analyze" data-idx="' + i + '">Analyze Content</button>' +
      '<button type="button" class="btn-primary btn-generate" data-idx="' + i + '">Generate AI Description</button>' +
      '</div><div class="analyze-result" style="display:none;margin-top:12px;"></div>' +
      '<div class="generate-result" style="display:none;margin-top:12px;"></div></td></tr>';
  }
  html += '</tbody></table></div>';
  results.innerHTML = html;
  results.querySelectorAll('.product-row').forEach(function(row) {
    row.style.cursor = 'pointer';
    row.addEventListener('click', function(e) {
      if (e.target.closest('button')) return;
      toggleRow(parseInt(row.getAttribute('data-idx'), 10));
    });
  });
  results.querySelectorAll('.btn-analyze').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var idx = parseInt(btn.getAttribute('data-idx'), 10);
      var prod = products[idx];
      if (!prod || !window.analyzeContent) return;
      var nm = String(prod.name || '').slice(0, 60);
      var br = String(prod.brand || '').slice(0, 40);
      var desc = String(prod.description || '').slice(0, 200);
      window.analyzeContent(btn, nm, br, desc);
    });
  });
  results.querySelectorAll('.btn-generate').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var idx = parseInt(btn.getAttribute('data-idx'), 10);
      var prod = products[idx];
      if (!prod || !window.generateDesc) return;
      var nm = String(prod.name || '').slice(0, 60);
      var br = String(prod.brand || '').slice(0, 40);
      var desc = String(prod.description || '').slice(0, 200);
      var ml = (prod.missing || []).map(function(f) { return f.label || f || ''; }).join('|');
      window.generateDesc(btn, nm, br, desc, ml);
    });
  });
}
function showScanResults(data) {
  try {
    if (typeof window.renderResults === 'function') {
      window.renderResults(data);
      return;
    }
  } catch (err) {
    console.error('renderResults error', err);
  }
  renderBasicResults(data);
}
async function runScan() {
  var input = document.getElementById('storeUrl');
  var url = input ? input.value.trim() : '';
  if (!url) {
    alert('Please enter a store URL (e.g. gymshark.com or yourstore.myshopify.com).');
    if (input) { input.focus(); input.style.borderColor = '#D72C0D'; }
    return;
  }
  if (input) input.style.borderColor = '';
  try { history.replaceState(null, '', '/app?url=' + encodeURIComponent(url)); } catch (e) {}
  var btn = document.getElementById('scanBtn');
  var results = document.getElementById('results');
  if (!btn || !results) return;
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  results.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Scanning up to 20 products - this may take 20-30 seconds...</p></div>';
  try {
    var params = new URLSearchParams(window.location.search);
    var source = params.get('source') || params.get('utm_source') || params.get('ref') || '';
    var ctrl = new AbortController();
    var timer = setTimeout(function() { ctrl.abort(); }, 90000);
    var res = await appFetch('/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url, source: source}),
      signal: ctrl.signal
    });
    clearTimeout(timer);
    var data;
    try { data = await res.json(); } catch (e) { data = {error: 'Server error (' + res.status + '). Try gymshark.com or retry.'}; }
    if (data.redirect) {
      results.innerHTML = '<div class="error-banner">Error: ' + scanEsc(data.error || 'Please reconnect AiReady to Shopify.') +
        '<div style="margin-top:12px;"><button class="btn-primary" onclick="window.top.location.href=' + jsArg(data.redirect) + '">Reconnect Shopify</button></div></div>';
      return;
    }
    if (!res.ok && !data.error) data.error = 'Scan failed (' + res.status + '). Try gymshark.com or wait and retry.';
    if (data.error) {
      results.innerHTML = '<div class="error-banner">Error: ' + scanEsc(data.error) + '</div>';
    } else {
      showScanResults(data);
    }
  } catch (e) {
    var msg = (e && e.name === 'AbortError') ? 'Scan timed out (90s). Try a smaller store or retry.' : 'Could not connect to scanner. Check the URL and try again.';
    results.innerHTML = '<div class="error-banner">' + scanEsc(msg) + '</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Scan Store';
}
window.runScan = runScan;
function initScanApp() {
  var params = new URLSearchParams(window.location.search);
  var urlParam = params.get('url');
  var shop = params.get('shop') || {{ shop_prefill|tojson }};
  var storeInput = document.getElementById('storeUrl');
  if (shop && storeInput) {
    var banner = document.getElementById('shopBanner');
    var label = document.getElementById('shopLabel');
    if (banner) banner.classList.add('visible');
    if (label) label.textContent = shop;
    storeInput.value = shop;
  } else if (urlParam && storeInput) {
    storeInput.value = urlParam;
  }
  var scanForm = document.getElementById('scanForm');
  if (scanForm && !scanForm._bound) {
    scanForm._bound = true;
    scanForm.addEventListener('submit', function(e) {
      e.preventDefault();
      runScan();
    });
  }
  var path = window.location.pathname;
  if (path === '/upgrade' || params.get('upgrade') === '1') return;
  if (params.get('paypal') === 'return' || params.get('unlocked') === '1') return;
  if (urlParam) runScan();
}
function initPaywallApp() {
  var params = new URLSearchParams(window.location.search);
  var shop = params.get('shop');
  var storeInput = document.getElementById('storeUrl');
  var path = window.location.pathname;
  var isPaywall = path === '/upgrade' || params.get('upgrade') === '1';
  refreshPlanStatus(shop || (storeInput && storeInput.value.trim()) || '');
  if (params.get('paypal') === 'return') {
    showUpgradeModal(shop || (storeInput && storeInput.value.trim()) || '');
    showPaidStep();
    return;
  }
  if (params.get('unlocked') === '1') {
    shop = shop || '';
    var banner = document.getElementById('unlimitedBanner');
    if (banner) {
      banner.style.display = 'none';
      banner.innerHTML = '';
    }
    if (shop && storeInput) {
      storeInput.value = shop;
      var storeEl = document.getElementById('upgradeStoreUrl');
      if (storeEl) storeEl.value = shop;
      var paypalShop = document.getElementById('paypalShop');
      if (paypalShop) paypalShop.value = shop;
    }
    closeUpgradeModal();
    if (shop && !document.getElementById('results').innerHTML) runScan();
    return;
  }
  if (params.get('plan') === 'free') {
    shop = shop || '';
    var freeBanner = document.getElementById('unlimitedBanner');
    if (freeBanner) {
      freeBanner.style.display = 'block';
      freeBanner.innerHTML = '<strong>Plan changed:</strong> ' + (shop ? escapeHtml(shop) : 'your store') + ' is now on the free plan.';
    }
    if (shop && storeInput) storeInput.value = shop;
  }
  if (isPaywall) {
    var paywallShop = shop || (storeInput && storeInput.value.trim()) || '';
    shouldAutoOpenUpgrade(paywallShop).then(function(open) {
      if (open) showUpgradeModal(paywallShop);
    });
  }
}
function bootApp() {
  verifyEmbeddedSessionToken();
  initScanApp();
  initPaywallApp();
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootApp);
} else {
  bootApp();
}
</script>

</body>
</html>
"""

def get_product_urls(store_url, limit=FREE_PRODUCT_LIMIT):
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
    products = []

    # Method 1: products.json (most reliable)
    try:
        r = requests.get(f"{base}/products.json?limit={limit}", headers=headers, timeout=12)
        if r.status_code == 200:
            data = r.json()
            products = data.get('products', [])
            for p in products:
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
                urls = [l.text.strip() for l in locs if '/products/' in l.text][:limit]
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
                    if len(urls) >= limit:
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
                    if len(urls) >= limit:
                        break
        except Exception:
            pass

    return urls[:limit], products[:limit]


def schema_from_shopify_product(data):
    """Build schema dict from Shopify products.json entry (no extra HTTP)."""
    if not data:
        return None
    variants = data.get('variants', []) if isinstance(data.get('variants', []), list) else []
    options = {}
    for option in (data.get('options') or []):
        if not isinstance(option, dict):
            continue
        name = str(option.get('name') or '').strip().lower()
        if name:
            values = option.get('values') or []
            options[name] = values if isinstance(values, list) else [values]
    images = data.get('images', []) if isinstance(data.get('images', []), list) else []
    first_image = images[0] if images else {}
    image_src = first_image.get('src') if isinstance(first_image, dict) else first_image
    schema = {
        '@type': 'Product',
        '_shopify_id': str(data.get('id', '')),
        'name': data.get('title', ''),
        'description': data.get('body_html', ''),
        'image': image_src,
        'brand': data.get('vendor', ''),
        'sku': variants[0].get('sku', '') if variants else '',
        'offers': {
            'price': variants[0].get('price') if variants else None,
            'availability': 'InStock' if any(v.get('available') for v in variants) else 'OutOfStock',
        } if variants else None,
    }
    for opt_name, values in options.items():
        if values:
            if 'color' in opt_name or 'colour' in opt_name:
                schema['color'] = values[0]
            elif 'size' in opt_name:
                schema['size'] = values[0]
            elif 'material' in opt_name or 'fabric' in opt_name:
                schema['material'] = values[0]
    for v in variants:
        if not schema.get('gtin') and v.get('barcode'):
            schema['gtin'] = v['barcode']
        if not schema.get('mpn') and v.get('sku'):
            schema['mpn'] = v['sku']
        if schema.get('gtin') and schema.get('mpn'):
            break
    return schema

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
  <title>AiReady - Free GEO Scanner for Shopify | AI Readiness Score</title>
  <meta name="description" content="Free GEO (Generative Engine Optimization) scanner for Shopify. Check your AI Readiness Score and get found by ChatGPT, Perplexity, and Gemini."/>
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
    .nav-link { color:var(--text-sub); font-size:13px; text-decoration:none; font-weight:600; }
    .nav-link:hover { color:var(--green); }
    .lang-btn { font-size:13px; color:var(--text-sub); background:none; border:1px solid var(--border); padding:5px 12px; border-radius:20px; cursor:pointer; }
    .lang-btn:hover { border-color:#aaa; }
    .btn-nav { background:var(--green); color:#fff; border:none; padding:8px 20px; border-radius:6px; font-size:14px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-block; }
    .btn-nav:hover { background:var(--green-dark); }
    .hero { max-width:760px; margin:0 auto; padding:72px 24px 60px; text-align:center; }
    .hero-badge { display:inline-block; background:var(--yellow-bg); color:#B95000; border:1px solid var(--yellow-border); border-radius:20px; padding:5px 14px; font-size:12px; font-weight:600; margin-bottom:20px; }
    .hero h1 { font-size:clamp(28px,5vw,52px); font-weight:800; line-height:1.15; letter-spacing:-1px; margin-bottom:18px; }
    .hero h1 em { color:var(--green); font-style:normal; }
    .hero-sub { font-size:18px; color:var(--text-sub); max-width:560px; margin:0 auto 36px; line-height:1.6; }
    .hero-input-row { display:flex; gap:10px; max-width:520px; margin:0 auto 14px; border:none; padding:0; background:transparent; }
    .hero-input-row .btn-hero { border:none; cursor:pointer; }
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
    <a href="/privacy" class="nav-link">Privacy</a>
    <a href="/terms" class="nav-link">Terms</a>
    <button class="lang-btn" onclick="toggleLang()">
      <span class="en">中文</span>
      <span class="zh">English</span>
    </button>
    <a href="/#scan-hero" class="btn-nav">
      <span class="en">Free Scan &rarr;</span>
      <span class="zh">免费扫描 &rarr;</span>
    </a>
  </div>
</nav>

<!-- HERO -->
<section class="hero" id="scan-hero">
  <div class="hero-badge en">Free GEO Scanner for Shopify Stores</div>
  <div class="hero-badge zh">免费 GEO 检测工具 &mdash; 专为 Shopify 出海商家</div>
  <h1 class="en">Is your store <em>invisible</em><br/>to ChatGPT?</h1>
  <h1 class="zh">你的店铺对 ChatGPT <em>隐形</em>吗？</h1>
  <p class="hero-sub en">GEO (Generative Engine Optimization) is the new SEO. If your Shopify products lack structured data, ChatGPT, Perplexity, and Gemini won't recommend them. Get your free GEO score in 30 seconds.</p>
  <p class="hero-sub zh">GEO（生成式引擎优化）是新一代 SEO。如果你的 Shopify 产品缺少结构化数据，ChatGPT、Perplexity、Gemini 就不会推荐你的产品。30 秒免费获取你的 GEO 评分。</p>
  <form class="hero-input-row" action="/app" method="get">
    <input type="text" class="hero-input" id="heroUrl" name="url" placeholder="yourstore.myshopify.com" required />
    <button type="submit" class="btn-hero">
      <span class="en">Scan Free</span>
      <span class="zh">免费扫描</span>
    </button>
  </form>
  <p class="hero-hint en">No signup required &mdash; scan up to 20 products for free</p>
  <p class="hero-hint zh">无需注册 &mdash; 免费扫描最多 20 个产品</p>
</section>

<!-- AI LOGOS -->
<div class="logos">
  <div class="logos-label en">GEO-optimize for these AI engines</div>
  <div class="logos-label zh">为以下 AI 引擎做 GEO 优化</div>
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
  <h2 class="en">GEO is the new SEO.<br/>Most Shopify stores are invisible to AI.</h2>
  <h2 class="zh">GEO 是新一代 SEO。<br/>大多数 Shopify 店铺对 AI 引擎隐形。</h2>
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
  <h2 class="en">Three steps to GEO-ready products</h2>
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
  <h2 class="en">Everything your store needs<br/>to win at GEO</h2>
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
        <li class="en">See missing product fields</li><li class="zh">Missing field report</li>
        <li class="en">PDF report download</li><li class="zh">PDF 报告下载</li>
      </ul>
      <a href="/#scan-hero" class="btn-price btn-price-free">
        <span class="en">Start Free Scan</span>
        <span class="zh">开始免费扫描</span>
      </a>
    </div>
    <div class="price-card featured">
      <div class="price-tier en">Unlimited</div>
      <div class="price-tier zh">无限版</div>
      <div class="price-amount">$9.99</div>
      <div class="price-desc en">per month, per store</div>
      <div class="price-desc zh">一次性付款，按店铺</div>
      <ul class="price-features">
        <li class="en">Everything in Free</li><li class="zh">包含所有免费功能</li>
        <li class="en">Unlimited AI descriptions</li><li class="zh">无限次 AI 描述生成</li>
        <li class="en">Save directly to Shopify</li><li class="zh">直接保存到 Shopify</li>
        <li class="en">Bulk fix all products</li><li class="zh">批量修复全部产品</li>
        <li class="en">Weekly email reports</li><li class="zh">每周报告邮件</li>
      </ul>
      <a href="/upgrade" class="btn-price btn-price-paid">
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
        <span class="en">What is the $9.99 monthly plan for?</span>
        <span class="zh">9 美元一次性付款包含什么？</span>
        <span class="faq-icon">+</span>
      </div>
      <div class="faq-a en">The $9.99 monthly plan unlocks expanded product scanning, unlimited AI description generation, direct Shopify saving, bulk product fixes, and weekly email reports for one store.</div>
      <div class="faq-a zh">9 美元解锁该店铺的无限次 AI 描述生成、直接保存到 Shopify、批量修复全部产品、每周邮件报告 &mdash; 永久有效，无订阅，无续费。</div>
    </div>
  </div>
</section>

<!-- CTA BAND -->
<div class="cta-band">
  <h2 class="en">Get your free GEO score in 30 seconds</h2>
  <h2 class="zh">30 秒获取你的免费 GEO 评分</h2>
  <p class="en">Free scan, no account needed.</p>
  <p class="zh">免费扫描，无需注册。</p>
  <a href="/#scan-hero" class="btn-nav" style="font-size:16px;padding:14px 36px;display:inline-block;">
    <span class="en">Scan My Store Free &rarr;</span>
    <span class="zh">免费扫描我的店铺 &rarr;</span>
  </a>
</div>

<footer>
  <div class="nav-logo" style="font-size:15px;">Ai<span style="color:#95BF47;">Ready</span></div>
  <div class="en">AI Readiness Checker for Shopify &mdash; &copy; 2025 AiReady</div>
  <div class="zh">Shopify AI 可见性检测工具 &mdash; &copy; 2025 AiReady</div>
  <div style="display:flex;gap:16px;">
    <a href="/privacy" style="font-size:12px;color:#aaa;text-decoration:none;">Privacy Policy</a>
    <a href="/terms" style="font-size:12px;color:#aaa;text-decoration:none;">Terms of Service</a>
  </div>
</footer>

<script>
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


PRIVACY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Privacy Policy - AiReady</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 800px; margin: 0 auto; padding: 40px 24px 80px; color: #202223; line-height: 1.7; font-size: 15px; }
    h1 { font-size: 28px; font-weight: 800; margin-bottom: 8px; }
    h2 { font-size: 18px; font-weight: 700; margin-top: 36px; margin-bottom: 8px; }
    p, li { color: #444; }
    ul { padding-left: 20px; }
    .nav { display: flex; align-items: center; justify-content: space-between; margin-bottom: 40px; }
    .logo { font-size: 18px; font-weight: 800; text-decoration: none; color: #1A1A1A; }
    .logo span { color: #95BF47; }
    .date { font-size: 13px; color: #888; margin-bottom: 32px; }
    a { color: #008060; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/" class="logo">Ai<span>Ready</span></a>
    <a href="/" style="font-size:13px;color:#666;text-decoration:none;">&larr; Home</a>
  </div>
  <h1>Privacy Policy</h1>
  <p class="date">Last updated: June 2025</p>

  <p>AiReady ("we", "us", "our") operates the website aiready-checker.onrender.com. This page informs you of our policies regarding the collection, use, and disclosure of personal data when you use our service.</p>

  <h2>1. Information We Collect</h2>
  <p>We collect the following types of information:</p>
  <ul>
    <li><strong>Store URL:</strong> The Shopify store domain you enter for scanning. This is used solely to perform the AI readiness scan.</li>
    <li><strong>Email address:</strong> Only if you subscribe to weekly reports or submit an upgrade request. Used to send you reports and unlock confirmation.</li>
    <li><strong>Usage data:</strong> We track store domains and scan size to enforce the free product scan limit.</li>
    <li><strong>Shopify OAuth token:</strong> If you connect your store via Shopify OAuth, we store an access token to enable saving fixes directly to your products. This token is stored securely and only used for actions you explicitly trigger.</li>
  </ul>

  <h2>2. How We Use Your Information</h2>
  <ul>
    <li>To perform AI readiness scans of your Shopify store</li>
    <li>To generate AI-optimized product descriptions</li>
    <li>To send weekly AI readiness reports (only if subscribed)</li>
    <li>To unlock unlimited access after payment verification</li>
    <li>To save product description updates to your Shopify store (only with your explicit action)</li>
  </ul>

  <h2>3. Data Sharing</h2>
  <p>We do not sell, trade, or rent your personal data to third parties. We use the following third-party services:</p>
  <ul>
    <li><strong>DeepSeek AI:</strong> Product descriptions are generated using DeepSeek's API. Product name and description text is sent to DeepSeek for generation. No personal data is included.</li>
    <li><strong>Resend:</strong> Used to send weekly email reports to subscribers.</li>
    <li><strong>Render:</strong> Our hosting provider. Your data is stored on Render's infrastructure.</li>
    <li><strong>Shopify:</strong> If you connect your store via OAuth, we interact with Shopify's Admin API on your behalf.</li>
  </ul>

  <h2>4. Data Retention</h2>
  <p>We retain your data for as long as your account is active or as needed to provide services. You may request deletion of your data at any time by contacting us. If you uninstall the Shopify app or Shopify sends a shop redaction webhook, we remove stored app tokens and shop-level records associated with that store.</p>

  <h2>5. Security</h2>
  <p>We take reasonable measures to protect your information. Shopify access tokens are stored securely and never exposed publicly. However, no method of transmission over the internet is 100% secure.</p>

  <h2>6. Your Rights</h2>
  <p>You have the right to access, correct, or delete your personal data. To exercise these rights, contact us at the email below.</p>

  <h2>7. Cookies</h2>
  <p>We use session cookies only for Shopify OAuth authentication flow. We do not use tracking or advertising cookies.</p>

  <h2>8. Changes to This Policy</h2>
  <p>We may update this policy from time to time. We will notify you of any changes by updating the date at the top of this page.</p>

  <h2>9. Contact</h2>
  <p>If you have questions about this Privacy Policy, please contact us at: <a href="mailto:ymk991015@gmail.com">ymk991015@gmail.com</a></p>
</body>
</html>
"""

TERMS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Terms of Service - AiReady</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 800px; margin: 0 auto; padding: 40px 24px 80px; color: #202223; line-height: 1.7; font-size: 15px; }
    h1 { font-size: 28px; font-weight: 800; margin-bottom: 8px; }
    h2 { font-size: 18px; font-weight: 700; margin-top: 36px; margin-bottom: 8px; }
    p, li { color: #444; }
    ul { padding-left: 20px; }
    .nav { display: flex; align-items: center; justify-content: space-between; margin-bottom: 40px; }
    .logo { font-size: 18px; font-weight: 800; text-decoration: none; color: #1A1A1A; }
    .logo span { color: #95BF47; }
    .date { font-size: 13px; color: #888; margin-bottom: 32px; }
    a { color: #008060; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/" class="logo">Ai<span>Ready</span></a>
    <a href="/" style="font-size:13px;color:#666;text-decoration:none;">&larr; Home</a>
  </div>
  <h1>Terms of Service</h1>
  <p class="date">Last updated: June 2025</p>

  <p>Please read these Terms of Service ("Terms") carefully before using AiReady at aiready-checker.onrender.com ("Service"). By accessing or using the Service, you agree to be bound by these Terms.</p>

  <h2>1. Use of Service</h2>
  <p>AiReady provides a tool to scan Shopify stores for AI search visibility and generate optimized product descriptions. You may use the Service only for lawful purposes and in accordance with these Terms.</p>
  <p>You agree not to:</p>
  <ul>
    <li>Use the Service to scan stores you do not own or have permission to scan</li>
    <li>Attempt to reverse engineer or circumvent usage limits</li>
    <li>Use automated tools to make excessive requests to the Service</li>
    <li>Use the Service for any illegal or unauthorized purpose</li>
  </ul>

  <h2>2. Free Tier and Paid Access</h2>
  <p>The Service offers a free tier that scans up to 20 products per store. Installed Shopify apps use Shopify Billing for paid access, currently $9.99 USD per month per store. Standalone web scanner payments, when offered outside the Shopify app, may use PayPal. Payments are non-refundable once paid access has been activated.</p>

  <h2>3. Shopify Integration</h2>
  <p>If you connect your Shopify store via OAuth, you grant AiReady permission to read and update product information on your behalf. You may revoke this permission at any time through your Shopify admin panel. AiReady will only make changes to your store when you explicitly trigger an action.</p>

  <h2>4. Intellectual Property</h2>
  <p>The AI-generated product descriptions produced by the Service are provided for your use. You are responsible for reviewing and editing generated content before publishing it to your store. AiReady does not claim ownership of content generated on your behalf.</p>

  <h2>5. Disclaimer of Warranties</h2>
  <p>The Service is provided "as is" without warranties of any kind. We do not guarantee that the Service will be uninterrupted, error-free, or that the results will meet your specific requirements. AI-generated content may require editing before use.</p>

  <h2>6. Limitation of Liability</h2>
  <p>To the maximum extent permitted by law, AiReady shall not be liable for any indirect, incidental, special, or consequential damages resulting from your use of the Service, including but not limited to loss of profits, data, or business opportunities.</p>

  <h2>7. Changes to Terms</h2>
  <p>We reserve the right to modify these Terms at any time. Continued use of the Service after changes constitutes acceptance of the new Terms.</p>

  <h2>8. Governing Law</h2>
  <p>These Terms shall be governed by applicable law. Any disputes shall be resolved through good-faith negotiation.</p>

  <h2>9. Contact</h2>
  <p>For questions about these Terms, contact us at: <a href="mailto:ymk991015@gmail.com">ymk991015@gmail.com</a></p>
</body>
</html>
"""


@app.route('/privacy')
def privacy():
    return render_template_string(PRIVACY_TEMPLATE)

@app.route('/terms')
def terms():
    return render_template_string(TERMS_TEMPLATE)

@app.route('/')
def index():
    if request.args.get('host') or request.args.get('shop'):
        return redirect('/app?' + urlencode(request.args))
    return render_template_string(LANDING_TEMPLATE)

def _render_app_page(open_upgrade=False, shop_prefill='', url_param='', shopify_app_context=False):
    normalized_prefill = normalize_shop(shop_prefill)
    shopify_app_context = bool(
        shopify_app_context
        or (normalized_prefill and is_valid_shop(normalized_prefill))
        or request.args.get('host')
    )
    if open_upgrade and normalized_prefill and is_valid_shop(normalized_prefill) and is_paid(normalized_prefill):
        open_upgrade = False
    return render_template_string(
        HTML_TEMPLATE,
        prefill_url=url_param,
        open_upgrade=open_upgrade,
        shop_prefill=shop_prefill,
        shopify_app_context=shopify_app_context,
        shopify_client_id=SHOPIFY_CLIENT_ID,
        paypal_hosted_button_id=PAYPAL_HOSTED_BUTTON_ID,
        app_base_url=APP_BASE_URL,
        usd_price=display_price(USD_PRICE),
        paypal_price=display_price(USD_PRICE),
        shopify_monthly_price=display_price(SHOPIFY_MONTHLY_PRICE),
    )


@app.route('/upgrade')
def upgrade_page():
    shop = normalize_shop(request.args.get('shop', session.get('shop', '')))
    return _render_app_page(
        open_upgrade=True,
        shop_prefill=shop,
        shopify_app_context=bool(request.args.get('host')),
    )


@app.route('/app')
def app_page():
    url_param = request.args.get('url', '')
    open_upgrade = request.args.get('upgrade') == '1'
    shop_prefill = normalize_shop(request.args.get('shop', session.get('shop', '')))
    if shop_prefill and is_valid_shop(shop_prefill) and not has_shop_token(shop_prefill):
        install_params = {'shop': shop_prefill}
        host = request.args.get('host', '')
        if host:
            install_params['host'] = host
        return redirect('/install?' + urlencode(install_params))
    return _render_app_page(
        open_upgrade=open_upgrade,
        shop_prefill=shop_prefill,
        url_param=url_param,
        shopify_app_context=bool(request.args.get('host')),
    )

@app.route('/api/session-token-check', methods=['POST'])
def session_token_check():
    token_payload = current_shopify_session()
    if not token_payload:
        return jsonify({'error': 'Invalid Shopify session token'}), 401
    shop = token_payload.get('shop', '')
    if shop:
        session['shop'] = shop
    return jsonify({'ok': True, 'shop': shop})

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json() or {}
    store_url = data.get('url', '').strip()
    source = clean_source(data.get('source', '') or request.args.get('source', ''))
    if not store_url:
        return jsonify({'error': 'Please provide a store URL.'})
    normalized_shop = normalize_shop(store_url)
    token_payload = current_shopify_session()
    session_shop = normalize_shop((token_payload or {}).get('shop', ''))
    if session_shop and is_valid_shop(session_shop):
        normalized_shop = session_shop
    shopify_products = []
    product_urls = []
    if normalized_shop and is_valid_shop(normalized_shop) and not has_shop_token(normalized_shop) and session_shop == normalized_shop:
        return jsonify({
            'error': 'Please reconnect AiReady to Shopify so it can read products from this store.',
            'redirect': f'/install?shop={normalized_shop}&force=1',
        }), 401
    paid = bool(normalized_shop and is_valid_shop(normalized_shop) and (is_paid(normalized_shop) or sync_shopify_billing_status(normalized_shop)))
    product_limit = PAID_PRODUCT_LIMIT if paid else FREE_PRODUCT_LIMIT
    if normalized_shop and is_valid_shop(normalized_shop) and has_shop_token(normalized_shop):
        try:
            shopify_products = fetch_shopify_admin_products(normalized_shop, limit=product_limit)
            for product in shopify_products:
                handle = product.get('handle', '')
                url = product.get('online_store_url') or (f'https://{normalized_shop}/products/{handle}' if handle else '')
                if url:
                    product_urls.append(url)
        except Exception as exc:
            app.logger.warning('Falling back to storefront scan for %s: %s', normalized_shop, exc)
            if 'GraphQL 401' in str(exc) or 'GraphQL 403' in str(exc):
                return jsonify({
                    'error': 'Please reconnect AiReady to Shopify so it can read products from this store.',
                    'redirect': f'/install?shop={normalized_shop}&force=1',
                }), 401
            return jsonify({
                'error': 'Could not read products from Shopify Admin API. Please reopen AiReady from Shopify Admin and try again.',
                'detail': str(exc)[:300],
            }), 400
            shopify_products = []
            product_urls = []
    if not product_urls:
        product_urls, shopify_products = get_product_urls(store_url, limit=product_limit)
    if not product_urls:
        return jsonify({'error': 'Could not find products. Make sure the store has products available to this app.'})
    product_by_handle = {p.get('handle', ''): p for p in shopify_products if p.get('handle')}

    def scan_one(url):
        handle = url.rstrip('/').split('/products/')[-1].split('?')[0]
        schema = None
        if handle and handle in product_by_handle:
            schema = schema_from_shopify_product(product_by_handle[handle])
        if not schema:
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
    if product_by_handle:
        raw = []
        for url in product_urls:
            try:
                raw.append(scan_one(url))
            except Exception as exc:
                app.logger.warning('Skipping Shopify product during scan for %s: %s', normalized_shop or store_url, exc)
    else:
        def safe_scan_one(url):
            try:
                return scan_one(url)
            except Exception as exc:
                app.logger.warning('Skipping storefront product during scan for %s: %s', store_url, exc)
                return None
        with ThreadPoolExecutor(max_workers=6) as executor:
            raw = list(executor.map(safe_scan_one, product_urls))
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
    record_scan_event(store_domain, source, avg_score, len(results))

    return jsonify({
        'products': results,
        'summary': {
            'store': store_domain,
            'avg_score': avg_score,
            'total_products': len(results),
            'top_issues': [{'field': f, 'count': c} for f, c in top_issues],
            'has_token': has_token,
            'paid': paid,
            'product_limit': product_limit,
        }
    })

@app.route('/api/usage', methods=['GET'])
def api_usage():
    shop = request.args.get('shop', '').strip().lower()
    if not shop:
        return jsonify({'error': 'shop required'}), 400
    normalized = normalize_shop(shop)
    if normalized and is_valid_shop(normalized):
        sync_shopify_billing_status(normalized)
        shop = normalized
    paid = is_paid(shop)
    subscription = get_shopify_subscription_summary(shop) if paid and has_shop_token(shop) else {}
    return jsonify({
        'shop': shop,
        'paid': paid,
        'subscription': subscription,
        'current_period_end': subscription.get('current_period_end', ''),
        'product_limit': PAID_PRODUCT_LIMIT if paid else FREE_PRODUCT_LIMIT,
        'free_product_limit': FREE_PRODUCT_LIMIT,
        'has_token': has_shop_token(shop),
    })


@app.route('/paypal/register-intent', methods=['POST'])
def paypal_register_intent():
    """Remember which shop is paying before user opens PayPal."""
    data = request.get_json() or {}
    shop = normalize_shop(data.get('shop', ''))
    if not shop or not is_valid_shop(shop):
        return jsonify({'error': 'Invalid store URL. Use your myshopify.com domain.'}), 400
    db_execute('INSERT INTO paypal_intents (shop) VALUES (?)', (shop,))
    return jsonify({'success': True})


@app.route('/paypal/return')
def paypal_return():
    """Landing page after PayPal payment (configure in PayPal if available)."""
    shop = normalize_shop(request.args.get('shop', ''))
    if shop and is_valid_shop(shop):
        for _ in range(6):
            if is_paid(shop):
                return redirect(f'/app?shop={shop}&unlocked=1')
            time.sleep(1)
    qs = f'shop={shop}' if shop else ''
    return redirect(f'/upgrade?paypal=return&{qs}' if qs else '/upgrade?paypal=return')


@app.route('/paypal/ipn', methods=['POST'])
def paypal_ipn():
    """Verify PayPal IPN and mark shop as paid automatically."""
    raw = request.get_data(as_text=True)
    verify_url = 'https://ipnpb.paypal.com/cgi-bin/webscr'
    verify_resp = requests.post(verify_url, data='cmd=_notify-validate&' + raw,
                                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                timeout=10)
    if verify_resp.text != 'VERIFIED':
        return 'INVALID', 400
    params = dict(p.split('=', 1) for p in raw.split('&') if '=' in p)
    payment_status = unquote_plus(params.get('payment_status', ''))
    txn_id = unquote_plus(params.get('txn_id', ''))
    receiver_email = unquote_plus(params.get('receiver_email', '')).strip().lower()
    mc_currency = unquote_plus(params.get('mc_currency', '')).strip().upper()
    mc_gross = unquote_plus(params.get('mc_gross', '0'))
    try:
        amount = float(mc_gross)
    except ValueError:
        amount = 0.0
    shop = normalize_shop(unquote_plus(params.get('custom', '')))
    receiver_ok = not PAYPAL_RECEIVER_EMAIL or receiver_email == PAYPAL_RECEIVER_EMAIL
    if payment_status == 'Completed' and mc_currency == 'USD' and amount + 0.01 >= USD_PRICE and receiver_ok:
        if not shop or not is_valid_shop(shop):
            row = db_execute(
                'SELECT shop FROM paypal_intents ORDER BY id DESC LIMIT 1',
                fetchone=True,
            )
            if row:
                shop = row[0]
        if shop and is_valid_shop(shop):
            mark_shop_paid(shop, txn_id or 'paypal-ipn')
    return 'OK', 200


def create_shopify_billing_confirmation(shop):
    shop = normalize_shop(shop)
    try:
        token = get_shop_token(shop)
    except Exception as exc:
        app.logger.warning('Failed to load Shopify token for billing %s: %s', shop, exc)
        return {
            'error': 'Please reconnect AiReady to Shopify before approving the charge.',
            'redirect': f'/install?shop={shop}&force=1',
            'status': 401,
        }
    if not shop or not token:
        return {
            'error': 'Install or reconnect the Shopify app before upgrading through Shopify.',
            'redirect': f'/install?shop={shop}&force=1' if shop else '',
            'status': 401,
        }
    if is_paid(shop) or sync_shopify_billing_status(shop):
        return {'success': True, 'already_paid': True, 'redirect': f'/app?shop={shop}&unlocked=1', 'status': 200}

    mutation = """
    mutation AppSubscriptionCreate($name: String!, $lineItems: [AppSubscriptionLineItemInput!]!, $returnUrl: URL!, $test: Boolean) {
      appSubscriptionCreate(name: $name, returnUrl: $returnUrl, lineItems: $lineItems, test: $test) {
        userErrors { field message }
        appSubscription { id status }
        confirmationUrl
      }
    }
    """
    variables = {
        'name': SHOPIFY_BILLING_NAME,
        'returnUrl': f'{APP_BASE_URL}/shopify/billing/return?shop={shop}',
        'lineItems': [{
            'plan': {
                'appRecurringPricingDetails': {
                    'price': {'amount': SHOPIFY_MONTHLY_PRICE, 'currencyCode': 'USD'},
                    'interval': 'EVERY_30_DAYS',
                },
            },
        }],
        'test': SHOPIFY_BILLING_TEST,
    }
    try:
        result = shopify_graphql(shop, token, mutation, variables)
        payload = result.get('appSubscriptionCreate') or {}
        errors = payload.get('userErrors') or []
        if errors:
            return {'error': '; '.join(e.get('message', 'Billing error') for e in errors), 'status': 400}
        confirmation_url = payload.get('confirmationUrl')
        if not confirmation_url:
            return {'error': 'Shopify did not return a billing confirmation URL.', 'status': 400}
        return {'success': True, 'confirmationUrl': confirmation_url, 'status': 200}
    except Exception as exc:
        app.logger.warning('Failed to create Shopify billing charge for %s: %s', shop, exc)
        if (
            'GraphQL 401' in str(exc)
            or 'GraphQL 403' in str(exc)
            or 'Non-expiring access tokens' in str(exc)
        ):
            return {
                'error': 'Please reconnect AiReady to Shopify before approving the charge.',
                'redirect': f'/install?shop={shop}&force=1',
                'status': 401,
            }
        return {'error': f'Could not start Shopify billing: {str(exc)[:220]}', 'status': 500}


@app.route('/shopify/billing/start', methods=['POST'])
def shopify_billing_start():
    data = request.get_json() or {}
    payload = create_shopify_billing_confirmation(data.get('shop', session.get('shop', '')))
    status = payload.pop('status', 200)
    return jsonify(payload), status

def cancel_shopify_billing_subscription(shop):
    shop = normalize_shop(shop)
    try:
        token = get_shop_token(shop)
    except Exception as exc:
        app.logger.warning('Failed to load Shopify token for cancellation %s: %s', shop, exc)
        return {
            'error': 'Please reconnect AiReady to Shopify before changing the plan.',
            'redirect': f'/install?shop={shop}&force=1',
            'status': 401,
        }
    if not shop or not token:
        return {
            'error': 'Install or reconnect the Shopify app before changing the plan.',
            'redirect': f'/install?shop={shop}&force=1' if shop else '',
            'status': 401,
        }
    try:
        subscriptions = fetch_shopify_active_subscriptions(shop, token)
        active_subscription = next(
            (
                subscription for subscription in subscriptions
                if subscription.get('name') == SHOPIFY_BILLING_NAME
                and subscription.get('status') == 'ACTIVE'
            ),
            None,
        )
        if not active_subscription:
            clear_shopify_paid(shop)
            return {'success': True, 'already_free': True, 'status': 200}
        current_period_end = active_subscription.get('currentPeriodEnd') or ''

        mutation = """
        mutation AppSubscriptionCancel($id: ID!, $prorate: Boolean) {
          appSubscriptionCancel(id: $id, prorate: $prorate) {
            userErrors { field message }
            appSubscription { id status currentPeriodEnd }
          }
        }
        """
        data = shopify_graphql(shop, token, mutation, {
            'id': active_subscription.get('id'),
            'prorate': False,
        })
        payload = data.get('appSubscriptionCancel') or {}
        errors = payload.get('userErrors') or []
        if errors:
            return {'error': '; '.join(e.get('message', 'Billing error') for e in errors), 'status': 400}
        clear_shopify_paid(shop)
        return {
            'success': True,
            'subscription': payload.get('appSubscription') or {},
            'current_period_end': current_period_end,
            'redirect': f'/app?shop={shop}&plan=free',
            'status': 200,
        }
    except Exception as exc:
        app.logger.warning('Failed to cancel Shopify billing for %s: %s', shop, exc)
        if (
            'GraphQL 401' in str(exc)
            or 'GraphQL 403' in str(exc)
            or 'Non-expiring access tokens' in str(exc)
        ):
            return {
                'error': 'Please reconnect AiReady to Shopify before changing the plan.',
                'redirect': f'/install?shop={shop}&force=1',
                'status': 401,
            }
        return {'error': f'Could not change the Shopify plan: {str(exc)[:220]}', 'status': 500}


@app.route('/shopify/billing/cancel', methods=['POST'])
def shopify_billing_cancel():
    data = request.get_json() or {}
    shop = normalize_shop(data.get('shop', session.get('shop', '')))
    token_payload = current_shopify_session()
    token_shop = normalize_shop((token_payload or {}).get('dest', '').replace('https://', ''))
    if token_shop and shop and token_shop != shop:
        return jsonify({'error': 'Session token shop does not match requested shop.'}), 403
    payload = cancel_shopify_billing_subscription(shop)
    status = payload.pop('status', 200)
    return jsonify(payload), status


@app.route('/shopify/billing/approve')
def shopify_billing_approve():
    shop = normalize_shop(request.args.get('shop', session.get('shop', '')))
    payload = create_shopify_billing_confirmation(shop)
    if payload.get('confirmationUrl'):
        return redirect(payload['confirmationUrl'])
    if payload.get('redirect'):
        return redirect(payload['redirect'])
    qs = urlencode({
        'shop': shop,
        'billing_error': payload.get('error', 'Could not start Shopify billing.'),
    })
    return redirect(f'/upgrade?{qs}')


@app.route('/shopify/billing/return')
def shopify_billing_return():
    shop = normalize_shop(request.args.get('shop', session.get('shop', '')))
    if shop and sync_shopify_billing_status(shop):
        return redirect(f'/app?shop={shop}&unlocked=1')
    return redirect(f'/upgrade?shop={shop}&billing=pending' if shop else '/upgrade?billing=pending')


@app.route('/generate', methods=['POST'])
def generate():
    data = request.get_json()
    name = data.get('name', '')
    brand = data.get('brand', '')
    description = (data.get('description', '') or '')[:2000]
    missing = data.get('missing', [])
    shop = data.get('shop', '').strip().lower()

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
    if has_shop_token(shop) and request.args.get('force') != '1':
        return redirect(f'/app?shop={shop}')
    state = create_oauth_state(shop)
    session['oauth_state'] = state
    params = {
        'client_id': SHOPIFY_CLIENT_ID,
        'scope': SHOPIFY_SCOPES,
        'redirect_uri': f'{APP_BASE_URL}/auth/callback',
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
    # Verify signed state. Keep the session fallback for older in-flight installs.
    if not verify_oauth_state(state, shop) and state != session.get('oauth_state', ''):
        return 'Invalid state parameter.', 403
    if not code:
        return 'Missing authorization code.', 400

    # Exchange code for access token
    token_url = f"https://{shop}/admin/oauth/access_token"
    resp = requests.post(token_url, json={
        'client_id': SHOPIFY_CLIENT_ID,
        'client_secret': SHOPIFY_CLIENT_SECRET,
        'code': code,
        'expiring': 1,
    })
    if resp.status_code != 200:
        return 'Failed to get access token.', 400

    token_data = resp.json()
    access_token = token_data.get('access_token')
    if not access_token:
        return 'Missing access token from Shopify.', 400
    save_shop_token(
        shop,
        access_token,
        token_data.get('scope', ''),
        token_data.get('refresh_token', ''),
        token_data.get('expires_in', 0),
        token_data.get('refresh_token_expires_in', 0),
    )
    register_app_uninstalled_webhook(shop, access_token)
    session['shop'] = shop

    # Redirect back into the embedded app with shop param so it auto-scans
    app_home = shopify_app_home_url(request.args.get('host', ''))
    if app_home:
        return redirect(app_home)
    return redirect(f'/app?shop={shop}')


@app.route('/webhooks/app/uninstalled', methods=['POST'])
def webhook_app_uninstalled():
    raw_body = request.get_data()
    if not verify_shopify_webhook(raw_body):
        return 'Invalid webhook signature', 401
    shop = request.headers.get('X-Shopify-Shop-Domain', '')
    if not shop:
        try:
            shop = (json.loads(raw_body.decode('utf-8')) or {}).get('domain', '')
        except Exception:
            shop = ''
    delete_shop_data(shop)
    return '', 200


@app.route('/webhooks/customers/data_request', methods=['POST'])
def webhook_customers_data_request():
    raw_body = request.get_data()
    if not verify_shopify_webhook(raw_body):
        return 'Invalid webhook signature', 401
    return jsonify({
        'message': 'AiReady does not store Shopify customer personal data.'
    }), 200


@app.route('/webhooks/customers/redact', methods=['POST'])
def webhook_customers_redact():
    raw_body = request.get_data()
    if not verify_shopify_webhook(raw_body):
        return 'Invalid webhook signature', 401
    return '', 200


@app.route('/webhooks/shop/redact', methods=['POST'])
def webhook_shop_redact():
    raw_body = request.get_data()
    if not verify_shopify_webhook(raw_body):
        return 'Invalid webhook signature', 401
    shop = request.headers.get('X-Shopify-Shop-Domain', '')
    if not shop:
        try:
            payload = json.loads(raw_body.decode('utf-8')) or {}
            shop = payload.get('shop_domain') or payload.get('domain') or ''
        except Exception:
            shop = ''
    delete_shop_data(shop)
    return '', 200


@app.route('/api/products')
def api_products():
    """Fetch products directly from Shopify GraphQL Admin API using stored token."""
    shop = normalize_shop(request.args.get('shop', session.get('shop', '')))
    if not shop or not get_shop_token(shop):
        return jsonify({'error': 'Not authenticated. Please install the app first.'}), 401
    try:
        products = fetch_shopify_admin_products(shop)
    except Exception as exc:
        app.logger.warning('Failed to fetch products through GraphQL for %s: %s', shop, exc)
        if 'GraphQL 401' in str(exc) or 'GraphQL 403' in str(exc):
            return jsonify({
                'error': 'Please reconnect AiReady to Shopify.',
                'redirect': f'/install?shop={shop}&force=1',
            }), 401
        return jsonify({'error': 'Failed to fetch products from Shopify.'}), 400
    return jsonify({'products': products})

@app.route('/admin/shopify-debug')
def admin_shopify_debug():
    secret = request.headers.get('X-Admin-Secret', '')
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    shop = normalize_shop(request.args.get('shop', ''))
    if not is_valid_shop(shop):
        return jsonify({'error': 'valid shop required'}), 400
    info = get_shop_token_info(shop)
    result = {'shop': shop, **info}
    token = get_shop_token(shop)
    if token:
        query = '{ shop { name myshopifyDomain } products(first: 1) { edges { node { id title } } } }'
        try:
            data = shopify_graphql(shop, token, query)
            result['graphql_ok'] = True
            result['shop_name'] = ((data.get('shop') or {}).get('name')) or ''
            result['product_count_sample'] = len((((data.get('products') or {}).get('edges')) or []))
        except Exception as exc:
            result['graphql_ok'] = False
            result['graphql_error'] = str(exc)[:500]
    return jsonify(result)


@app.route('/api/update_vendor', methods=['POST'])
def update_vendor():
    """Update product vendor/brand via Shopify GraphQL Admin API."""
    data = request.get_json()
    shop = normalize_shop(data.get('shop', session.get('shop', '')))
    product_id = data.get('product_id')
    vendor = data.get('vendor', '')

    token = get_shop_token(shop)
    if not shop or not token:
        return jsonify({'error': 'Not authenticated.'}), 401
    if not product_id:
        return jsonify({'error': 'Missing product_id.'}), 400

    mutation = """
    mutation AiReadyUpdateVendor($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product { id }
        userErrors { field message }
      }
    }
    """
    try:
        data = shopify_graphql(
            shop,
            token,
            mutation,
            {'product': {'id': shopify_product_gid(product_id), 'vendor': vendor}},
        )
    except Exception as exc:
        app.logger.warning('Failed to update vendor through GraphQL for %s: %s', shop, exc)
        return jsonify({'error': 'Failed to update vendor.'}), 400
    errors = ((data.get('productUpdate') or {}).get('userErrors')) or []
    if errors:
        return jsonify({'error': '; '.join(err.get('message', 'Failed to update vendor.') for err in errors)}), 400
    return jsonify({'success': True})


@app.route('/api/update_product', methods=['POST'])
def update_product():
    """Update a product description via Shopify GraphQL Admin API."""
    data = request.get_json()
    shop = normalize_shop(data.get('shop', session.get('shop', '')))
    product_id = data.get('product_id')
    new_description = data.get('description', '')

    token = get_shop_token(shop)
    if not shop or not token:
        return jsonify({'error': 'Not authenticated.'}), 401
    if not product_id:
        return jsonify({'error': 'Missing product_id.'}), 400

    mutation = """
    mutation AiReadyUpdateDescription($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product { id }
        userErrors { field message }
      }
    }
    """
    try:
        data = shopify_graphql(
            shop,
            token,
            mutation,
            {'product': {'id': shopify_product_gid(product_id), 'descriptionHtml': new_description}},
        )
    except Exception as exc:
        app.logger.warning('Failed to update product through GraphQL for %s: %s', shop, exc)
        return jsonify({'error': 'Failed to update product.'}), 400
    errors = ((data.get('productUpdate') or {}).get('userErrors')) or []
    if errors:
        return jsonify({'error': '; '.join(err.get('message', 'Failed to update product.') for err in errors)}), 400
    return jsonify({'success': True})


ADMIN_SECRET = os.environ.get('ADMIN_SECRET', '')

@app.route('/request-unlock', methods=['POST'])
def request_unlock():
    """User submits PayPal payment details for manual verification."""
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    shop = normalize_shop(data.get('shop', ''))
    if not email or not shop:
        return jsonify({'error': 'PayPal email and store URL required.'}), 400
    if not is_valid_shop(shop):
        return jsonify({'error': 'Invalid store URL. Use your myshopify.com domain.'}), 400
    note = f'[paypal] {email}'
    db_execute('INSERT INTO unlock_requests (email, shop) VALUES (?, ?)', (note, shop))
    return jsonify({'success': True})


@app.route('/admin/unlock', methods=['POST'])
def admin_unlock():
    """Admin endpoint to manually unlock a shop after verifying payment."""
    secret = request.headers.get('X-Admin-Secret', '')
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    shop = data.get('shop', '').strip().lower()
    if not shop:
        return jsonify({'error': 'shop required'}), 400
    mark_shop_paid(shop, 'manual')
    return jsonify({'success': True, 'shop': shop})


@app.route('/admin/requests', methods=['GET'])
def admin_requests():
    """List pending unlock requests."""
    secret = request.headers.get('X-Admin-Secret', '')
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    rows = db_execute('SELECT id, email, shop, created_at FROM unlock_requests ORDER BY created_at DESC', fetchall=True)
    return jsonify([{'id': r[0], 'email': r[1], 'shop': r[2], 'created_at': r[3]} for r in rows])


@app.route('/admin/metrics', methods=['GET'])
def admin_metrics():
    secret = request.headers.get('X-Admin-Secret', '')
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    total_scans = db_execute('SELECT COUNT(*) FROM scan_events', fetchone=True)[0]
    unique_scanned_shops = db_execute('SELECT COUNT(DISTINCT shop) FROM scan_events', fetchone=True)[0]
    total_leads = db_execute('SELECT COUNT(*) FROM subscriptions', fetchone=True)[0]
    unique_lead_shops = db_execute('SELECT COUNT(DISTINCT shop) FROM subscriptions', fetchone=True)[0]
    paid_shops = db_execute('SELECT COUNT(*) FROM paid_shops', fetchone=True)[0]
    pending_unlocks = db_execute('SELECT COUNT(*) FROM unlock_requests', fetchone=True)[0]
    suppressed_emails = db_execute('SELECT COUNT(*) FROM email_suppression', fetchone=True)[0]
    total_lead_events = db_execute('SELECT COUNT(*) FROM lead_events', fetchone=True)[0]

    top_sources = db_execute('''
        SELECT COALESCE(NULLIF(source, ''), 'direct') AS source, COUNT(*) AS scans
        FROM scan_events
        GROUP BY COALESCE(NULLIF(source, ''), 'direct')
        ORDER BY scans DESC
        LIMIT 10
    ''', fetchall=True)
    top_lead_sources = db_execute('''
        SELECT COALESCE(NULLIF(source, ''), 'direct') AS source, COUNT(*) AS leads
        FROM lead_events
        GROUP BY COALESCE(NULLIF(source, ''), 'direct')
        ORDER BY leads DESC
        LIMIT 10
    ''', fetchall=True)
    recent_scans = db_execute('''
        SELECT shop, source, avg_score, total_products, created_at
        FROM scan_events
        ORDER BY id DESC
        LIMIT 20
    ''', fetchall=True)
    recent_leads = db_execute('''
        SELECT email, shop, source, avg_score, total_products, created_at
        FROM lead_events
        ORDER BY id DESC
        LIMIT 20
    ''', fetchall=True)

    lead_rate = round((total_leads / total_scans) * 100, 1) if total_scans else 0
    paid_rate = round((paid_shops / unique_scanned_shops) * 100, 1) if unique_scanned_shops else 0

    return jsonify({
        'summary': {
            'total_scans': total_scans,
            'unique_scanned_shops': unique_scanned_shops,
            'total_leads': total_leads,
            'total_lead_events': total_lead_events,
            'unique_lead_shops': unique_lead_shops,
            'paid_shops': paid_shops,
            'pending_unlocks': pending_unlocks,
            'suppressed_emails': suppressed_emails,
            'lead_rate_percent': lead_rate,
            'paid_shop_rate_percent': paid_rate,
        },
        'top_sources': [{'source': r[0], 'scans': r[1]} for r in top_sources],
        'top_lead_sources': [{'source': r[0], 'leads': r[1]} for r in top_lead_sources],
        'recent_scans': [
            {'shop': r[0], 'source': r[1] or 'direct', 'avg_score': r[2], 'total_products': r[3], 'created_at': r[4]}
            for r in recent_scans
        ],
        'recent_leads': [
            {
                'email': r[0],
                'shop': r[1],
                'source': r[2] or 'direct',
                'avg_score': r[3],
                'total_products': r[4],
                'created_at': r[5],
            }
            for r in recent_leads
        ],
    })


@app.route('/admin/leads', methods=['GET'])
def admin_leads():
    secret = request.headers.get('X-Admin-Secret', '')
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    limit_raw = request.args.get('limit', '100')
    try:
        limit = min(max(int(limit_raw), 1), 500)
    except ValueError:
        limit = 100

    rows = db_execute('''
        SELECT le.email, le.shop, le.source, le.avg_score, le.total_products, le.created_at,
               CASE WHEN ps.shop IS NULL THEN 0 ELSE 1 END AS paid
        FROM lead_events le
        LEFT JOIN paid_shops ps ON ps.shop = le.shop
        ORDER BY le.id DESC
        LIMIT ?
    ''', (limit * 3,), fetchall=True)

    deduped = {}
    for row in rows:
        key = (row[0], row[1])
        if key in deduped:
            continue
        avg_score = int(row[3] or 0)
        total_products = int(row[4] or 0)
        paid = bool(row[6])
        deduped[key] = {
            'email': row[0],
            'shop': row[1],
            'source': row[2] or 'direct',
            'avg_score': avg_score,
            'total_products': total_products,
            'paid': paid,
            'priority': lead_priority(avg_score, total_products, paid),
            'created_at': row[5],
        }
        if len(deduped) >= limit:
            break

    leads = sorted(
        deduped.values(),
        key=lambda item: (
            {'high': 0, 'medium': 1, 'low': 2}.get(item['priority'], 3),
            item['paid'],
            item['avg_score'] or 999,
            -(item['total_products'] or 0),
        )
    )

    if request.args.get('format') == 'csv':
        out = io.StringIO()
        fields = ['priority', 'email', 'shop', 'source', 'avg_score', 'total_products', 'paid', 'created_at']
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for lead in leads:
            writer.writerow({field: lead.get(field, '') for field in fields})
        return Response(
            out.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=aiready-leads.csv'},
        )

    return jsonify({'count': len(leads), 'leads': leads})


@app.route('/unsubscribe', methods=['GET'])
def unsubscribe():
    email = request.args.get('email', '').strip().lower()
    token = request.args.get('token', '').strip()
    ok = email and is_valid_email(email) and hmac.compare_digest(token, unsubscribe_token(email))
    if not ok:
        return render_template_string("""
<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Invalid unsubscribe link - AiReady</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#F6F6F7;color:#202223;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px}.box{background:#fff;border:1px solid #E4E5E7;border-radius:8px;max-width:460px;padding:28px;text-align:center}h1{font-size:20px;margin:0 0 8px}p{color:#6D7175;line-height:1.6}</style>
</head><body><div class="box"><h1>Invalid unsubscribe link</h1><p>This link is missing or expired. You can reply to any AiReady email and ask to unsubscribe.</p><a href="/app" style="color:#008060;">Back to AiReady</a></div></body></html>
"""), 400
    suppress_email(email, 'unsubscribe')
    return render_template_string("""
<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Unsubscribed - AiReady</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#F6F6F7;color:#202223;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px}.box{background:#fff;border:1px solid #AEE9D1;border-radius:8px;max-width:460px;padding:28px;text-align:center}h1{font-size:20px;margin:0 0 8px;color:#005E45}p{color:#6D7175;line-height:1.6}</style>
</head><body><div class="box"><h1>You are unsubscribed</h1><p>{{ email }} will no longer receive AiReady scan reports or weekly score emails.</p><a href="/app" style="color:#008060;">Back to AiReady</a></div></body></html>
""", email=email)


@app.route('/subscribe', methods=['POST'])
def subscribe():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    shop = data.get('shop', '').strip().lower()
    summary = data.get('summary') if isinstance(data.get('summary'), dict) else {}
    products = data.get('products') if isinstance(data.get('products'), list) else []
    source = clean_source(data.get('source', '') or request.args.get('source', ''))
    if not email or not shop:
        return jsonify({'error': 'Email and shop required.'}), 400
    if not is_valid_email(email):
        return jsonify({'error': 'Please enter a valid email.'}), 400
    if is_suppressed_email(email):
        return jsonify({'error': 'This email has unsubscribed from AiReady reports.'}), 400
    try:
        db_execute('''INSERT INTO subscriptions (email, shop) VALUES (?, ?)
            ON CONFLICT(email, shop) DO NOTHING
        ''', (email, shop))
        record_lead_event(
            email,
            shop,
            source,
            summary.get('avg_score') or 0,
            summary.get('total_products') or len(products),
        )
        sent = send_scan_report_email(email, shop, summary, products)
        return jsonify({'success': True, 'sent': sent})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/run-weekly-scan', methods=['POST'])
def run_weekly_scan():
    """Called by external cron job weekly. Scans all subscribed shops and emails reports."""
    secret = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET or secret != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    subs = db_execute('SELECT id, email, shop, last_score FROM subscriptions', fetchall=True)

    sent = 0
    for sub_id, email, shop, last_score in subs:
        try:
            # Scan the store
            urls, shopify_products = get_product_urls(shop)
            if not urls:
                continue
            product_by_handle = {p.get('handle', ''): p for p in shopify_products if p.get('handle')}
            results = []
            for url in urls[:10]:
                handle = url.rstrip('/').split('/products/')[-1].split('?')[0]
                schema = schema_from_shopify_product(product_by_handle[handle]) if handle in product_by_handle else None
                if not schema:
                    schema = extract_schema(url)
                if not schema:
                    continue
                score, present, missing = score_product(schema)
                name = schema.get('name', url.split('/')[-1].replace('-', ' ').title())
                results.append({'name': name, 'score': score, 'missing': [f['label'] for f in missing]})
            if not results:
                continue

            avg = round(sum(r['score'] for r in results) / len(results))
            delta = avg - last_score if last_score else 0
            delta_str = f"+{delta}" if delta > 0 else str(delta)

            # Build email HTML
            unsub_url = html.escape(unsubscribe_url(email))
            rows = ''.join(
                f"<tr><td style='padding:8px;border-bottom:1px solid #eee;'>{r['name'][:50]}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #eee;color:{'#008060' if r['score']>=70 else '#B98900' if r['score']>=40 else '#D72C0D'};font-weight:600;'>{r['score']}/100</td>"
                f"<td style='padding:8px;border-bottom:1px solid #eee;font-size:12px;color:#6D7175;'>{', '.join(r['missing'][:3])}</td></tr>"
                for r in results
            )
            html_body = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#1A1A1A;padding:20px 24px;border-radius:8px 8px 0 0;">
    <span style="color:#fff;font-size:18px;font-weight:700;">Ai<span style="color:#95BF47;">Ready</span></span>
    <span style="color:#999;font-size:13px;margin-left:12px;">Weekly AI Readiness Report</span>
  </div>
  <div style="background:#fff;border:1px solid #E4E5E7;border-top:none;padding:24px;border-radius:0 0 8px 8px;">
    <h2 style="font-size:20px;color:#202223;margin:0 0 4px;">{shop}</h2>
    <p style="color:#6D7175;font-size:14px;margin:0 0 20px;">
      Average score: <strong style="color:{'#008060' if avg>=70 else '#B98900' if avg>=40 else '#D72C0D'};">{avg}/100</strong>
      {f' &nbsp; <span style="color:{"#008060" if delta>0 else "#D72C0D"};">({delta_str} from last week)</span>' if last_score else ''}
    </p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr>
        <th style="text-align:left;padding:8px;background:#F6F6F7;color:#6D7175;font-size:11px;text-transform:uppercase;">Product</th>
        <th style="text-align:left;padding:8px;background:#F6F6F7;color:#6D7175;font-size:11px;text-transform:uppercase;">Score</th>
        <th style="text-align:left;padding:8px;background:#F6F6F7;color:#6D7175;font-size:11px;text-transform:uppercase;">Top Missing Fields</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div style="margin-top:24px;text-align:center;">
      <a href="{APP_BASE_URL}/app?url={shop}&source=weekly_report" style="background:#008060;color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">View Full Report</a>
    </div>
    <p style="margin-top:20px;font-size:12px;color:#8C9196;text-align:center;">
      You're receiving this because you subscribed at {APP_BASE_URL}. <a href="{unsub_url}" style="color:#8C9196;">Unsubscribe</a>
    </p>
  </div>
</div>"""

            # Send via Resend
            if RESEND_API_KEY:
                requests.post(
                    'https://api.resend.com/emails',
                    headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
                    json={
                        'from': REPORT_FROM_EMAIL,
                        'to': [email],
                        'subject': f'Weekly AI Readiness Report: {shop} scored {avg}/100',
                        'html': html_body,
                    },
                    timeout=10
                )

            # Update last_score
            db_execute('UPDATE subscriptions SET last_score=?, last_scanned=CURRENT_TIMESTAMP WHERE id=?', (avg, sub_id))
            sent += 1
        except Exception:
            continue

    return jsonify({'sent': sent, 'total': len(subs)})


if __name__ == '__main__':
    app.run(debug=True, port=5001)
