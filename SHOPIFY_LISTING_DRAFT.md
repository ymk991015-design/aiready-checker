# AiReady Shopify Listing Draft

Use this as the first draft for the Shopify Partner Dashboard listing and review notes.

## App Name

AiReady: AI Visibility Scanner

## Short Description

Find missing product data that prevents AI engines from understanding and recommending your Shopify products.

## Full Description

AiReady scans your Shopify products and scores how ready they are for AI-driven discovery in tools like ChatGPT, Perplexity, and Gemini.

The app checks structured product signals such as brand, description, image, price, availability, GTIN/barcode, SKU, material, color, size, MPN, and aggregate rating. Merchants can see which products need work first, generate AI-ready product descriptions, and save approved updates back to Shopify.

AiReady is built for merchants who want their product catalog to be easier for AI search and recommendation engines to understand.

## Key Features

- Scan up to 20 products and get an AI Readiness Score.
- See missing fields by product and by store.
- Generate AI-ready product descriptions.
- Save approved descriptions back to Shopify.
- Preview one AI repair before upgrading.
- Download or email a scan report.
- Unlock unlimited fixes with Shopify monthly billing.

## Pricing Copy

Free plan:

- Scan up to 20 products.
- View product-level AI readiness scores.
- Use 5 free AI actions.
- Download reports.

Unlimited:

- $9.99 USD monthly subscription through Shopify Billing.
- Unlimited AI description generation for one store.
- Save approved descriptions to Shopify.
- Bulk fix products.
- Weekly email reports.

## Review Testing Instructions

1. Install the app on a Shopify test store.
2. Open the app from Shopify Admin.
3. Click "Scan Store".
4. Review the AI Readiness Score and product breakdown.
5. Open a product row and click "Generate AI Description".
6. If the store is connected through Shopify OAuth, click "Save to Shopify" to confirm the description update flow.
7. Use 5 free AI actions to trigger the upgrade modal.
8. Click "Approve monthly plan in Shopify" to test the Shopify billing approval flow.
9. Return to the app and confirm the store shows as unlocked.
10. Optional: enter an email in the report box to test report subscription capture.

## Data And Privacy Notes

AiReady reads product data to calculate AI readiness and generate approved content suggestions. The app writes product descriptions only when the merchant explicitly clicks save.

AiReady does not store Shopify customer personal data. Store-level records, subscriptions, usage counts, and access tokens are deleted after app uninstall or Shopify shop redaction webhook.

## Scope Justification

`read_products` is required to scan product data and calculate AI readiness.

`write_products` is required only so merchants can save approved AI descriptions back to Shopify products.

## Support Contact

Use the support email connected to the Shopify Partner account.
