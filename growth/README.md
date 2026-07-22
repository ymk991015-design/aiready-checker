# AiReady 14-Day Customer Acquisition Workspace

Goal: prove demand before spending more time on UI polish.

Targets by day 14:

- 200 visitors
- 30 scans
- 5 installs
- 1 paid shop or clear paid intent
- Clear evidence that merchants generate repair drafts or save fixes

## Daily routine

Use about one hour per day:

1. Research 20 Shopify stores in `lead_research.csv`.
2. Send 10-20 personalized emails or DMs with `../outreach/send_outreach.py`.
3. Publish one short community post from `content_calendar.csv`.
4. Save metrics with `fetch_metrics.py`.

## Tracking links

- Cold email: `https://aiready-checker.onrender.com/app?source=cold_email`
- Reddit: `https://aiready-checker.onrender.com/app?source=reddit`
- Community: `https://aiready-checker.onrender.com/app?source=community`
- Agency/partner: `https://aiready-checker.onrender.com/app?source=agency`
- Google Ads: `https://aiready-checker.onrender.com/?utm_source=google_ads`

## Commands

Preview the first 5 outreach emails:

```powershell
cd "C:\Users\WwwILL\Documents\Cowork OS\AiReady\checker"
python outreach\send_outreach.py --leads growth\lead_research.csv --limit 5
```

Send a small batch:

```powershell
cd "C:\Users\WwwILL\Documents\Cowork OS\AiReady\checker"
$env:RESEND_API_KEY="your_resend_key"
$env:OUTREACH_FROM="AiReady <reports@yourdomain.com>"
$env:OUTREACH_REPLY_TO="your@email.com"
python outreach\send_outreach.py --leads growth\lead_research.csv --send --confirm SEND --limit 20 --delay 8
```

Fetch daily metrics:

```powershell
cd "C:\Users\WwwILL\Documents\Cowork OS\AiReady\checker"
$env:ADMIN_SECRET="your_render_admin_secret"
python growth\fetch_metrics.py
```

The metrics export now tracks the full funnel:

- visitors
- scans, completed scans, failed scans
- installs and uninstalls
- repair drafts generated
- descriptions saved to Shopify
- upgrade clicks
- Shopify Billing checkout starts and completions
- subscription cancellations

## Stop rules

- If Google Ads spends $30 with 0 scans, pause ads and improve the landing page first.
- If outreach gets replies but no scans, make the email more direct and add a personalized first line.
- If scans happen but installs do not, improve the result page and Shopify install CTA.
- If installs happen but upgrades do not, interview users before changing pricing.
