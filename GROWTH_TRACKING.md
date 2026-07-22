# AiReady Growth Tracking

Use source links to track which customer acquisition channel creates visits, scans, installs, repair drafts, saved fixes, checkout starts, and paid shops.

The executable 14-day acquisition workspace is in `growth/`.

- `growth/lead_research.csv`: daily target stores and outreach status.
- `growth/content_calendar.csv`: one low-hype community post per day.
- `growth/google_ads_keywords.csv`: $100 Google Search test keywords and negatives.
- `growth/daily_metrics.csv`: daily KPI log.
- `growth/fetch_metrics.py`: pulls `/admin/metrics` into the KPI log.

## Source Links

Cold email:

```text
https://aiready-checker.onrender.com/app?source=cold_email
```

Reddit:

```text
https://aiready-checker.onrender.com/app?source=reddit
```

Community:

```text
https://aiready-checker.onrender.com/app?source=community
```

Shopify agency outreach:

```text
https://aiready-checker.onrender.com/app?source=agency
```

Partner or manual demo:

```text
https://aiready-checker.onrender.com/app?source=demo
```

Google Ads:

```text
https://aiready-checker.onrender.com/?utm_source=google_ads
```

The app also accepts:

```text
utm_source
ref
```

## Admin Metrics

Use this endpoint with the Render `ADMIN_SECRET` value:

```powershell
$headers = @{"X-Admin-Secret"="your_admin_secret"}
Invoke-RestMethod "https://aiready-checker.onrender.com/admin/metrics" -Headers $headers
```

The response includes:

- total scans
- completed scans
- failed scans
- unique scanned shops
- email leads
- app installs
- app uninstalls
- repair drafts generated
- descriptions saved to Shopify
- upgrade clicks
- checkout starts
- checkout completions
- subscription cancellations
- paid shops
- pending unlock requests
- suppressed or unsubscribed emails
- lead rate
- paid shop rate
- top visit and scan sources
- top email lead sources
- app event counts
- recent scans
- recent leads
- recent app events

## Lead Follow-Up List

Use this endpoint to see which emails are worth contacting first:

```powershell
$headers = @{"X-Admin-Secret"="your_admin_secret"}
Invoke-RestMethod "https://aiready-checker.onrender.com/admin/leads" -Headers $headers
```

Download as CSV:

```powershell
$headers = @{"X-Admin-Secret"="your_admin_secret"}
Invoke-WebRequest "https://aiready-checker.onrender.com/admin/leads?format=csv" -Headers $headers -OutFile aiready-leads.csv
```

Lead priority:

- `high`: low score, meaningful product count, not paid yet.
- `medium`: some clear fix opportunity.
- `low`: already paid, high score, or not enough product data.

## What To Watch First

For the first 100 visitors, track:

- Scan rate: how many visitors actually scan a store.
- Scan failure rate: whether stores are blocked by product access, token, or scraping issues.
- Lead rate: how many scanned stores leave an email.
- Install rate: how many scanners connect the Shopify app.
- Repair draft rate: how many installed stores generate at least one draft.
- Save rate: how many stores save a repair back to Shopify.
- Upgrade rate: how many scanned or installed stores start checkout and pay.
- Best source: which source creates the most scans and emails.
- Best lead source: prioritize channels in `top_lead_sources`, not only `top_sources`.
- Unsubscribe count: if this rises quickly, the outreach audience or message is wrong.

Do not scale outreach until at least one source produces replies or emails.
