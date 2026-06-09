import base64
import hashlib
import hmac
import importlib
import json
import os
import tempfile
from unittest.mock import patch


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


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
    assert_true("Approve charge in Shopify" in app_html, "Shopify billing button missing")
    assert_true("PayPal" in app_html, "standalone PayPal option missing")
    assert_true("Free AI repair preview" in app_html, "AI repair preview missing")
    assert_true('meta name="shopify-api-key"' in app_html, "Shopify App Bridge meta missing")
    assert_true("https://cdn.shopify.com/shopifycloud/app-bridge.js" in app_html, "Shopify App Bridge script missing")
    embedded = client.get("/app?shop=embedded-smoke.myshopify.com")
    assert_true(embedded.status_code == 302, "embedded app did not start Shopify install")
    assert_true("/install?shop=embedded-smoke.myshopify.com" in embedded.headers.get("Location", ""), "embedded app install redirect missing shop")
    mod.save_shop_token("embedded-paid.myshopify.com", "token", "read_products,write_products")
    embedded_paid = client.get("/app?shop=embedded-paid.myshopify.com&upgrade=1").get_data(as_text=True)
    assert_true("Approve charge in Shopify" in embedded_paid, "Shopify billing missing for installed app")
    assert_true("PayPal" not in embedded_paid, "PayPal was rendered inside installed Shopify app")


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
                    "appPurchaseOneTimeCreate": {
                        "userErrors": [],
                        "appPurchaseOneTime": {"id": "gid://shopify/AppPurchaseOneTime/1"},
                        "confirmationUrl": "https://demo.myshopify.com/admin/charges/confirm",
                    }
                }
            }

    with patch("app.requests.post", return_value=Resp()):
        res = client.post("/shopify/billing/start", json={"shop": "demo.myshopify.com"})
    assert_true(res.status_code == 200, f"billing start failed {res.status_code}")
    assert_true(res.get_json().get("confirmationUrl"), "billing confirmationUrl missing")


def test_shopify_graphql_admin_api(mod):
    client = mod.app.test_client()
    mod.save_shop_token("graphql-demo.myshopify.com", "token", "read_products,write_products")
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
        vendor = client.post("/api/update_vendor", json={"shop": "graphql-demo.myshopify.com", "product_id": "123", "vendor": "New Brand"})
        description = client.post("/api/update_product", json={"shop": "graphql-demo.myshopify.com", "product_id": "123", "description": "<p>New copy</p>"})
        webhook_ok = mod.register_app_uninstalled_webhook("graphql-demo.myshopify.com", "token")

    assert_true(products.status_code == 200, f"GraphQL products failed {products.status_code}")
    assert_true(products.get_json()["products"][0]["admin_graphql_api_id"] == "gid://shopify/Product/123", "GraphQL product id missing")
    assert_true(vendor.status_code == 200, f"GraphQL vendor update failed {vendor.status_code}")
    assert_true(description.status_code == 200, f"GraphQL description update failed {description.status_code}")
    assert_true(webhook_ok, "GraphQL webhook registration failed")
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
