# AiReady Shopify App Submission Checklist

Use these URLs in the Shopify Partner dashboard for the AiReady app.

## App URLs

App URL:

```text
https://aiready-checker.onrender.com/app
```

Allowed redirection URL:

```text
https://aiready-checker.onrender.com/auth/callback
```

Embedded app readiness:

```text
/app includes Shopify App Bridge and the shopify-api-key meta tag.
/app?shop=... sends uninstalled stores into Shopify OAuth before showing the UI.
```

Privacy policy:

```text
https://aiready-checker.onrender.com/privacy
```

Terms of service:

```text
https://aiready-checker.onrender.com/terms
```

## Required Webhooks

App uninstalled:

```text
https://aiready-checker.onrender.com/webhooks/app/uninstalled
```

The app also attempts to register this webhook automatically after OAuth install.

Customers data request:

```text
https://aiready-checker.onrender.com/webhooks/customers/data_request
```

Customers redact:

```text
https://aiready-checker.onrender.com/webhooks/customers/redact
```

Shop redact:

```text
https://aiready-checker.onrender.com/webhooks/shop/redact
```

## Scopes

The app currently requests:

```text
read_products,write_products
```

Use these only if the app submission explains that AiReady reads products to score AI readiness and writes product descriptions only when the merchant clicks save.

## Shopify API Compliance

AiReady uses the GraphQL Admin API for installed-store product reads, product updates, Shopify Billing, and app/uninstalled webhook registration.

The standalone public scanner may read a store's public `/products.json` storefront endpoint when a merchant enters a public store URL. This is not an Admin API call and does not use a Shopify access token.

## Render Environment Variables

Required for the Shopify app:

```text
SHOPIFY_CLIENT_ID
SHOPIFY_CLIENT_SECRET
DATABASE_URL
APP_BASE_URL=https://aiready-checker.onrender.com
FLASK_SECRET
ADMIN_SECRET
CRON_SECRET
```

Required for AI generation:

```text
DEEPSEEK_API_KEY
```

Required for PayPal unlock:

```text
PAYPAL_HOSTED_BUTTON_ID
PAYPAL_RECEIVER_EMAIL
USD_PRICE=9
```

PayPal is for the standalone web scanner only. For installed Shopify stores, AiReady starts a Shopify one-time app purchase through the GraphQL Admin API.

Optional Shopify billing settings:

```text
SHOPIFY_BILLING_NAME=AiReady Unlimited
SHOPIFY_BILLING_TEST=false
```

Use `SHOPIFY_BILLING_TEST=true` only when testing billing on a development store.

Required for report email sending:

```text
RESEND_API_KEY
REPORT_FROM_EMAIL=AiReady <reports@yourdomain.com>
```

Do not use `aiready-checker.onrender.com` as the Resend sending domain. Use a domain you own and can verify with DNS.

## Weekly Report Cron

`render.yaml` defines a weekly cron service:

```text
aiready-weekly-report
```

It runs every Monday at 09:00 UTC and calls:

```text
POST https://aiready-checker.onrender.com/run-weekly-scan
```

Set the same `CRON_SECRET` value on both the web service and the cron service.

`ADMIN_SECRET` protects `/admin/metrics`, `/admin/leads`, `/admin/requests`, and `/admin/unlock`. Do not leave it blank.

## Pre-Deploy Smoke Test

Run this before pushing a production deploy:

```powershell
& "C:\Users\WwwILL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" smoke_test.py
```

Expected result:

```text
All smoke tests passed.
```
