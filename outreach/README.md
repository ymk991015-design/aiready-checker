# AiReady Outreach Workflow

This is a local, low-volume outreach workflow for Shopify store owners.

## 1. Prepare leads

Use the 14-day acquisition lead file:

```powershell
notepad growth\lead_research.csv
```

Required columns:

- `email`
- `store`

Useful columns:

- `contact_name`
- `segment`
- `visible_issue`
- `score`
- `top_issues`
- `source`
- `status`
- `notes`

Use business/contact emails only. Do not send to people who asked not to be contacted.

## 2. Set email environment variables

```powershell
$env:RESEND_API_KEY="your_resend_key"
$env:OUTREACH_FROM="AiReady <reports@yourdomain.com>"
$env:OUTREACH_REPLY_TO="your_email@example.com"
```

`OUTREACH_FROM` must use a domain verified in Resend.

## 3. Preview first

```powershell
python outreach\send_outreach.py --leads growth\lead_research.csv --limit 5
```

This does not send email. It prints previews only.

## 4. Send

```powershell
python outreach\send_outreach.py --leads growth\lead_research.csv --send --confirm SEND --limit 20 --delay 8 --source cold_email
```

Start with 10-20 emails per day until you see replies and bounce rates.

## 5. Suppression list

Add emails that should never be contacted again to `suppression.csv`.

```csv
email,reason
owner@example.com,unsubscribed
```

The script also skips emails already present in `sent_log.csv`.

## 6. Daily metrics

Save app metrics after outreach:

```powershell
$env:ADMIN_SECRET="your_render_admin_secret"
python growth\fetch_metrics.py
```
