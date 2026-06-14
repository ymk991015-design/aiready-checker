import base64
import hashlib
import hmac
import importlib
import json
import os
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import patch


def assert_true(value, message):
    if not value:
        raise AssertionError(message)

def make_shopify_session_token(client_id, secret, shop):
    def b64url(data):
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    now = int(time.time())
    header = b64url({"alg": "HS256", "typ": "JWT"})
    payload = b64url({
        "iss": f"https://{shop}/admin",
        "dest": f"https://{shop}",
        "aud": client_id,
        "sub": "1",
        "exp": now + 60,
        "nbf": now - 5,
        "iat": now,
        "jti": "smoke-token",
    })
    signed = f"{header}.{payload}".encode("utf-8")
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()).decode("utf-8").rstrip("=")
    return f"{header}.{payload}.{sig}"


def shopify_hmac(params, secret):
    message = "&".join(f"{key}={params[key]}" for key in sorted(params))
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def fresh_app():
    db_path = os.path.join(tempfile.gettempdir(), "aiready_smoke_test.db")
    try:
        os.remove(db_path)
    except FileNotFoundError:
        pass
    os.environ["DB_PATH"] = db_path
    os.environ.setdefault("ADMIN_SECRET", "smoke-admin")
    os.environ.setdefault("CRON_SECRET", "smoke-cron")
    os.environ.setdefault("SHOPIFY_CLIENT_ID", "smoke-client")
    os.environ.setdefault("SHOPIFY_CLIENT_SECRET", "smoke-secret")
    import app

    return importlib.reload(app)


def test_pages(mod):
    client = mod.app.test_client()
    for path in ["/", "/app", "/privacy", "/terms"]:
        res = client.get(path)
        assert_true(res.status_code == 200, f"{path} returned {res.status_code}")
        assert_true("Content-Security-Policy" in res.headers, f"{path} missing CSP")
    app_html = client.get("/app").get_data(as_text=True)
    landing_html = client.get("/").get_data(as_text=True)
    assert_true('href="/privacy"' in landing_html and 'href="/terms"' in landing_html, "landing legal links missing")
    embedded_landing = client.get("/?shop=embedded-paid.myshopify.com&host=embedded-host")
    assert_true(embedded_landing.status_code == 302, "embedded landing did not redirect to app")
    assert_true(embedded_landing.headers.get("Location", "").startswith("/app?"), "embedded landing redirect target wrong")
    assert_true('href="/privacy"' in app_html and 'href="/terms"' in app_html, "app legal links missing")
    assert_true("Approve monthly plan in Shopify" in app_html, "Shopify billing button missing")
    assert_true('id="planStatusCard"' in app_html, "plan status card missing")
    assert_true("Current plan: Free" in app_html, "free plan status copy missing")
    assert_true("Upgrade plan" in app_html, "plan upgrade action missing")
    assert_true("Manage subscription" in app_html, "paid plan management modal copy missing")
    assert_true("Downgrade to Free" in app_html, "Shopify plan downgrade button missing")
    assert_true("/shopify/billing/cancel" in app_html, "Shopify billing cancel endpoint missing from app UI")
    assert_true("You've used your 5 free actions" not in app_html, "old action-limit paywall text still visible")
    assert_true("free actions remaining" not in app_html, "old action remaining badge still visible")
    assert_true("$9.99" in app_html, "monthly price missing from app")
    assert_true("PayPal" in app_html, "standalone PayPal option missing")
    assert_true("Free AI repair preview" in app_html, "AI repair preview missing")
    assert_true('meta name="shopify-api-key"' in app_html, "Shopify App Bridge meta missing")
    assert_true("https://cdn.shopify.com/shopifycloud/app-bridge.js" in app_html, "Shopify App Bridge script missing")
    assert_true("shopify.idToken" in app_html, "Shopify session token call missing")
    assert_true("/api/session-token-check" in app_html, "Shopify session token check missing")
    embedded = client.get("/app?shop=embedded-smoke.myshopify.com")
    assert_true(embedded.status_code == 302, "embedded app did not start Shopify install")
    assert_true("/install?shop=embedded-smoke.myshopify.com" in embedded.headers.get("Location", ""), "embedded app install redirect missing shop")
    mod.save_shop_token("embedded-paid.myshopify.com", "token", "read_products,write_products")
    forced_install = client.get("/install?shop=embedded-paid.myshopify.com&force=1")
    assert_true(forced_install.status_code == 302, "forced install did not redirect")
    assert_true("admin/oauth/authorize" in forced_install.headers.get("Location", ""), "forced install did not start OAuth")
    embedded_paid = client.get("/app?shop=embedded-paid.myshopify.com&upgrade=1").get_data(as_text=True)
    assert_true("Approve monthly plan in Shopify" in embedded_paid, "Shopify billing missing for installed app")
    assert_true('href="/shopify/billing/approve?shop=embedded-paid.myshopify.com"' in embedded_paid, "Shopify billing approval link missing")
    assert_true('target="_top"' in embedded_paid, "Shopify billing link does not use the current Shopify tab")
    assert_true("secure billing approval page in this tab" in embedded_paid, "Shopify billing current-tab hint missing")
    assert_true("startShopifyBilling" not in embedded_paid, "old async billing click handler rendered in Shopify app")
    assert_true("PayPal" not in embedded_paid, "PayPal was rendered inside installed Shopify app")
    assert_true('value="embedded-paid.myshopify.com"' in embedded_paid, "embedded Shopify app did not prefill shop")
    upgrade_paid = client.get("/upgrade?shop=embedded-paid.myshopify.com").get_data(as_text=True)
    assert_true("PayPal" not in upgrade_paid, "PayPal was rendered inside Shopify upgrade page")
    with client.session_transaction() as sess:
        sess["shop"] = "embedded-paid.myshopify.com"
    host_only = client.get("/app?host=embedded-host&upgrade=1").get_data(as_text=True)
    assert_true("PayPal" not in host_only, "PayPal was rendered for Shopify host context")
    assert_true("embedded-paid.myshopify.com" in host_only, "host-only Shopify app did not use session shop")
    token = make_shopify_session_token("smoke-client", "smoke-secret", "embedded-paid.myshopify.com")
    session_check = client.post("/api/session-token-check", headers={"Authorization": f"Bearer {token}"})
    assert_true(session_check.status_code == 200, "valid Shopify session token was rejected")
    missing_token = client.post("/api/session-token-check")
    assert_true(missing_token.status_code == 401, "missing Shopify session token was accepted")
    reconnect_token = make_shopify_session_token("smoke-client", "smoke-secret", "needs-reconnect.myshopify.com")
    reconnect = client.post(
        "/scan",
        json={"url": "needs-reconnect.myshopify.com"},
        headers={"Authorization": f"Bearer {reconnect_token}"},
    )
    assert_true(reconnect.status_code == 401, "scan without offline token did not request reconnect")
    assert_true(reconnect.get_json().get("redirect") == "/install?shop=needs-reconnect.myshopify.com&force=1", "reconnect redirect missing")
    broken_token = make_shopify_session_token("smoke-client", "smoke-secret", "broken-token.myshopify.com")
    with patch("app.get_shop_token", side_effect=RuntimeError("token refresh failed")):
        broken = client.post(
            "/scan",
            json={"url": "broken-token.myshopify.com"},
            headers={"Authorization": f"Bearer {broken_token}"},
        )
    assert_true(broken.status_code == 401, "scan returned server error instead of reconnect for broken Shopify token")
    assert_true(broken.get_json().get("redirect") == "/install?shop=broken-token.myshopify.com&force=1", "broken token reconnect redirect missing")


def test_admin_requires_configured_secret(mod):
    original = mod.ADMIN_SECRET
    mod.ADMIN_SECRET = ""
    client = mod.app.test_client()
    res = client.get("/admin/metrics", headers={"X-Admin-Secret": "anything"})
    assert_true(res.status_code == 401, "admin endpoint allowed access without ADMIN_SECRET")
    mod.ADMIN_SECRET = original


def test_subscribe_unsubscribe_metrics(mod):
    client = mod.app.test_client()
    payload = {
        "email": "lead@example.com",
        "shop": "leadstore.myshopify.com",
        "source": "cold_email",
        "summary": {"avg_score": 44, "total_products": 20},
        "products": [{"name": "Sample", "score": 44, "missing": ["Brand"]}],
    }
    res = client.post("/subscribe", json=payload)
    assert_true(res.status_code == 200, f"subscribe failed {res.status_code}")

    metrics = client.get("/admin/metrics", headers={"X-Admin-Secret": mod.ADMIN_SECRET})
    assert_true(metrics.status_code == 200, "metrics unauthorized with valid secret")
    data = metrics.get_json()
    assert_true(data["top_lead_sources"][0]["source"] == "cold_email", "lead source not recorded")
    assert_true(data["recent_leads"][0]["avg_score"] == 44, "lead score not recorded")
    leads = client.get("/admin/leads", headers={"X-Admin-Secret": mod.ADMIN_SECRET})
    assert_true(leads.status_code == 200, "admin leads failed")
    lead_data = leads.get_json()
    assert_true(lead_data["leads"][0]["priority"] in ("high", "medium"), "lead priority missing")
    csv_res = client.get("/admin/leads?format=csv", headers={"X-Admin-Secret": mod.ADMIN_SECRET})
    assert_true(csv_res.status_code == 200, "admin leads csv failed")
    assert_true("priority,email,shop" in csv_res.get_data(as_text=True), "leads csv header missing")

    bad = client.get("/unsubscribe?email=lead@example.com&token=bad")
    assert_true(bad.status_code == 400, "bad unsubscribe token accepted")
    token = mod.unsubscribe_token("lead@example.com")
    ok = client.get(f"/unsubscribe?email=lead@example.com&token={token}")
    assert_true(ok.status_code == 200, "valid unsubscribe failed")
    again = client.post("/subscribe", json=payload)
    assert_true(again.status_code == 400, "suppressed email resubscribed")


def test_webhooks(mod):
    client = mod.app.test_client()
    mod.SHOPIFY_CLIENT_SECRET = "smoke-secret"
    body = json.dumps({"shop_domain": "demo.myshopify.com"}).encode("utf-8")
    sig = base64.b64encode(hmac.new(b"smoke-secret", body, hashlib.sha256).digest()).decode("utf-8")
    headers = {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Shop-Domain": "demo.myshopify.com"}
    for path in [
        "/webhooks/app/uninstalled",
        "/webhooks/customers/data_request",
        "/webhooks/customers/redact",
        "/webhooks/shop/redact",
    ]:
        res = client.post(path, data=body, headers=headers, content_type="application/json")
        assert_true(res.status_code == 200, f"{path} returned {res.status_code}")
    bad = client.post(
        "/webhooks/app/uninstalled",
        data=body,
        headers={"X-Shopify-Hmac-Sha256": "bad"},
        content_type="application/json",
    )
    assert_true(bad.status_code == 401, "bad webhook signature accepted")


def test_shopify_billing(mod):
    client = mod.app.test_client()
    mod.save_shop_token("demo.myshopify.com", "token", "read_products,write_products")

    class Resp:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "appSubscriptionCreate": {
                        "userErrors": [],
                        "appSubscription": {"id": "gid://shopify/AppSubscription/1", "status": "PENDING"},
                        "confirmationUrl": "https://demo.myshopify.com/admin/charges/confirm_recurring_application_charge",
                    }
                }
            }

    with patch("app.requests.post", return_value=Resp()):
        res = client.post("/shopify/billing/start", json={"shop": "demo.myshopify.com"})
    assert_true(res.status_code == 200, f"billing start failed {res.status_code}")
    assert_true(res.get_json().get("confirmationUrl"), "billing confirmationUrl missing")
    with patch("app.requests.post", return_value=Resp()):
        approve = client.get("/shopify/billing/approve?shop=demo.myshopify.com")
    assert_true(approve.status_code == 302, f"billing approve did not redirect {approve.status_code}")
    assert_true(
        approve.headers.get("Location") == "https://demo.myshopify.com/admin/charges/confirm_recurring_application_charge",
        "billing approve did not redirect to Shopify confirmation URL",
    )

    class RejectResp:
        status_code = 403
        text = '{"errors":"[API] Non-expiring access tokens are no longer accepted for the Admin API."}'

        def json(self):
            return {"errors": "[API] Non-expiring access tokens are no longer accepted for the Admin API."}

    with patch("app.requests.post", return_value=RejectResp()):
        rejected = client.post("/shopify/billing/start", json={"shop": "demo.myshopify.com"})
    assert_true(rejected.status_code == 401, "billing did not request reconnect for rejected token")
    assert_true(rejected.get_json().get("redirect") == "/install?shop=demo.myshopify.com&force=1", "billing reconnect redirect missing")

    mod.mark_shop_paid("demo.myshopify.com", "shopify_subscription:gid://shopify/AppSubscription/1")
    paid_upgrade = client.get("/app?shop=demo.myshopify.com&upgrade=1").get_data(as_text=True)
    assert_true('<body class="paywall-open">' not in paid_upgrade, "paid Shopify app opened the upgrade modal on load")
    assert_true('<div class="modal-overlay visible" id="upgradeModal">' not in paid_upgrade, "paid Shopify app rendered a visible upgrade modal")
    assert_true('<div class="page-title">AI Readiness Scanner</div>' in paid_upgrade, "paid Shopify app still rendered upgrade page title")

    class GraphQLResp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def billing_post(*args, **kwargs):
        query = (kwargs.get("json") or {}).get("query", "")
        if "appSubscriptionCancel" in query:
            return GraphQLResp({
                "data": {
                    "appSubscriptionCancel": {
                        "userErrors": [],
                        "appSubscription": {"id": "gid://shopify/AppSubscription/1", "status": "CANCELLED"},
                    }
                }
            })
        return GraphQLResp({
            "data": {
                "currentAppInstallation": {
                    "activeSubscriptions": [{
                        "id": "gid://shopify/AppSubscription/1",
                        "name": mod.SHOPIFY_BILLING_NAME,
                        "status": "ACTIVE",
                        "test": False,
                        "lineItems": [{
                            "plan": {
                                "pricingDetails": {
                                    "__typename": "AppRecurringPricing",
                                    "interval": "EVERY_30_DAYS",
                                    "price": {"amount": str(mod.SHOPIFY_MONTHLY_PRICE), "currencyCode": "USD"},
                                }
                            }
                        }],
                    }]
                }
            }
        })

    with patch("app.requests.post", side_effect=billing_post):
        cancelled = client.post("/shopify/billing/cancel", json={"shop": "demo.myshopify.com"})
    assert_true(cancelled.status_code == 200, f"billing cancel failed {cancelled.status_code}")
    assert_true(cancelled.get_json().get("success"), "billing cancel did not return success")
    assert_true(not mod.is_paid("demo.myshopify.com"), "billing cancel did not downgrade local plan")


def test_oauth_signed_state_without_cookie(mod):
    client = mod.app.test_client()
    install = client.get("/install?shop=oauth-demo.myshopify.com")
    assert_true(install.status_code == 302, "install did not redirect to Shopify OAuth")
    state = parse_qs(urlparse(install.headers["Location"]).query)["state"][0]

    params = {
        "shop": "oauth-demo.myshopify.com",
        "code": "oauth-code",
        "state": state,
        "timestamp": "123",
    }
    params["hmac"] = shopify_hmac(params, "smoke-secret")

    class Resp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        if url.endswith("/admin/oauth/access_token"):
            assert_true((kwargs.get("json") or {}).get("expiring") == 1, "OAuth did not request expiring offline token")
            return Resp({
                "access_token": "oauth-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "refresh_token_expires_in": 7776000,
                "scope": "read_products,write_products",
            })
        return Resp({
            "data": {
                "webhookSubscriptionCreate": {
                    "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/1"},
                    "userErrors": [],
                }
            }
        })

    fresh_browser = mod.app.test_client()
    with patch("app.requests.post", side_effect=fake_post):
        callback = fresh_browser.get("/auth/callback?" + urlencode(params))
    assert_true(callback.status_code == 302, f"OAuth callback failed without session cookie: {callback.status_code}")
    assert_true("Invalid state parameter" not in callback.get_data(as_text=True), "signed OAuth state was rejected")
    token_info = mod.get_shop_token_info("oauth-demo.myshopify.com")
    assert_true(token_info["has_refresh_token"], "expiring offline refresh token was not saved")
    assert_true(token_info["expires_at"], "expiring offline access token expiry was not saved")


def test_shopify_graphql_admin_api(mod):
    client = mod.app.test_client()
    mod.save_shop_token("graphql-demo.myshopify.com", "token", "read_products,write_products")
    malformed_schema = mod.schema_from_shopify_product({
        "id": "bad",
        "title": "Malformed Product",
        "options": [{"name": None, "values": None}, None],
        "images": [None],
        "variants": [{"sku": "SKU", "barcode": ""}],
    })
    assert_true(malformed_schema["name"] == "Malformed Product", "malformed Shopify product was not normalized")
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append({"url": url, "json": kwargs.get("json") or {}})
        query = (kwargs.get("json") or {}).get("query", "")
        if "AiReadyProducts" in query:
            return Resp({
                "data": {
                    "products": {
                        "edges": [{
                            "node": {
                                "id": "gid://shopify/Product/123",
                                "legacyResourceId": "123",
                                "title": "GraphQL Shirt",
                                "handle": "graphql-shirt",
                                "vendor": "AiReady",
                                "descriptionHtml": "<p>Good shirt</p>",
                                "onlineStoreUrl": "https://graphql-demo.myshopify.com/products/graphql-shirt",
                                "featuredMedia": {"preview": {"image": {"url": "https://cdn.example/image.jpg"}}},
                                "options": [{"name": "Size", "values": ["M"]}],
                                "variants": {
                                    "edges": [{
                                        "node": {
                                            "id": "gid://shopify/ProductVariant/456",
                                            "legacyResourceId": "456",
                                            "sku": "SKU-1",
                                            "barcode": "0123",
                                            "price": "19.99",
                                        }
                                    }]
                                },
                            }
                        }]
                    }
                }
            })
        if "productUpdate" in query:
            return Resp({"data": {"productUpdate": {"product": {"id": "gid://shopify/Product/123"}, "userErrors": []}}})
        if "RegisterAppUninstalledWebhook" in query:
            return Resp({
                "data": {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": {
                            "id": "gid://shopify/WebhookSubscription/1",
                            "topic": "APP_UNINSTALLED",
                            "uri": "https://aiready-checker.onrender.com/webhooks/app/uninstalled",
                        },
                        "userErrors": [],
                    }
                }
            })
        return Resp({"data": {}})

    with patch("app.requests.post", side_effect=fake_post):
        products = client.get("/api/products?shop=graphql-demo.myshopify.com")
        scan = client.post("/scan", json={"url": "graphql-demo.myshopify.com"})
        vendor = client.post("/api/update_vendor", json={"shop": "graphql-demo.myshopify.com", "product_id": "123", "vendor": "New Brand"})
        description = client.post("/api/update_product", json={"shop": "graphql-demo.myshopify.com", "product_id": "123", "description": "<p>New copy</p>"})
        webhook_ok = mod.register_app_uninstalled_webhook("graphql-demo.myshopify.com", "token")
        blocked_debug = client.get("/admin/shopify-debug?shop=graphql-demo.myshopify.com")
        debug = client.get(
            "/admin/shopify-debug?shop=graphql-demo.myshopify.com",
            headers={"X-Admin-Secret": "smoke-admin"},
        )

    assert_true(products.status_code == 200, f"GraphQL products failed {products.status_code}")
    assert_true(products.get_json()["products"][0]["admin_graphql_api_id"] == "gid://shopify/Product/123", "GraphQL product id missing")
    assert_true(scan.status_code == 200, f"Admin-backed scan failed {scan.status_code}")
    assert_true(scan.get_json()["summary"]["total_products"] == 1, "Admin-backed scan did not read products")
    assert_true(vendor.status_code == 200, f"GraphQL vendor update failed {vendor.status_code}")
    assert_true(description.status_code == 200, f"GraphQL description update failed {description.status_code}")
    assert_true(webhook_ok, "GraphQL webhook registration failed")
    assert_true(blocked_debug.status_code == 401, "Shopify debug endpoint allowed unauthenticated access")
    assert_true(debug.status_code == 200 and debug.get_json()["graphql_ok"], "Shopify debug endpoint failed")
    assert_true(all("/graphql.json" in call["url"] for call in calls), "Admin API call did not use GraphQL endpoint")
    assert_true(not any("products.json" in call["url"] or "webhooks.json" in call["url"] for call in calls), "REST Admin API endpoint used")
    update_vars = [call["json"].get("variables", {}) for call in calls if "productUpdate" in call["json"].get("query", "")]
    assert_true(update_vars[0]["product"]["id"] == "gid://shopify/Product/123", "numeric product id was not converted to GID")
    assert_true(update_vars[1]["product"]["descriptionHtml"] == "<p>New copy</p>", "descriptionHtml variable missing")


def test_weekly_cron_script():
    import run_weekly_report

    class Resp:
        status_code = 200
        text = '{"sent":1,"total":1}'

    os.environ["CRON_SECRET"] = "smoke-cron"
    os.environ["APP_BASE_URL"] = "https://example.com"
    with patch("run_weekly_report.requests.post", return_value=Resp()) as mocked:
        assert_true(run_weekly_report.main() == 0, "weekly cron script failed")
    assert_true(mocked.call_args.kwargs["headers"]["X-Cron-Secret"] == "smoke-cron", "cron secret header missing")


def main():
    mod = fresh_app()
    tests = [
        test_pages,
        test_admin_requires_configured_secret,
        test_subscribe_unsubscribe_metrics,
        test_webhooks,
        test_shopify_billing,
        test_oauth_signed_state_without_cookie,
        test_shopify_graphql_admin_api,
    ]
    for test in tests:
        test(mod)
        print(f"PASS {test.__name__}")
    test_weekly_cron_script()
    print("PASS test_weekly_cron_script")
    print("All smoke tests passed.")


if __name__ == "__main__":
    main()
