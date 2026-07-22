import argparse
import csv
import html
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

APP_URL = os.environ.get("AIREADY_APP_URL", "https://aiready-checker.onrender.com").rstrip("/")
RESEND_API_URL = "https://api.resend.com/emails"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def read_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_email_set(path):
    return {
        (row.get("email") or "").strip().lower()
        for row in read_csv(path)
        if (row.get("email") or "").strip()
    }


def append_log(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    fields = ["email", "store", "source", "status", "resend_id", "error", "sent_at"]
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def clean_store(store):
    store = (store or "").strip()
    store = re.sub(r"^https?://", "", store, flags=re.I).split("/")[0]
    return store


def issue_list(raw):
    parts = [p.strip() for p in re.split(r"[;,|]", raw or "") if p.strip()]
    return parts[:4]


def build_message(lead, default_source="cold_email"):
    store = clean_store(lead.get("store", ""))
    name = (lead.get("contact_name") or "").strip()
    score = (lead.get("score") or "").strip()
    issues = issue_list(lead.get("top_issues", ""))
    source = (lead.get("source") or default_source or "cold_email").strip()
    greeting = f"Hi {name}," if name else "Hi,"

    if score:
        opener = f"I ran a quick product data scan on {store}. It scored {score}/100."
    else:
        opener = f"I checked {store} and noticed some products may be missing facts that make listings easier to understand and compare."

    issue_text = ""
    if issues:
        issue_text = "\nMain missing fields:\n" + "\n".join(f"- {i}" for i in issues)
    else:
        issue_text = "\nExamples include material, barcode, brand, size, color, review data, and other product facts."

    scan_url = f"{APP_URL}/app?{urlencode({'url': store, 'source': source})}"
    subject = f"Quick product data scan for {store}"

    text = f"""{greeting}

{opener}{issue_text}

I built AiReady, a Shopify app that scans products, shows missing product data, and drafts cleaner AI-ready descriptions for review.

You can run a free scan here:
{scan_url}

No signup is needed for the first scan.

Best,
AiReady

If this is not relevant, reply "unsubscribe" and I will not contact you again.
"""

    safe_text = html.escape(text).replace("\n", "<br>")
    html_body = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:15px;line-height:1.6;color:#202223;max-width:620px">
{safe_text}
</div>"""

    return subject, text, html_body


def send_email(api_key, from_email, reply_to, lead, subject, text, html_body):
    import requests

    payload = {
        "from": from_email,
        "to": [lead["email"]],
        "subject": subject,
        "text": text,
        "html": html_body,
    }
    if reply_to:
        payload["reply_to"] = [reply_to]

    response = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code}: {response.text}")
    return response.json().get("id", "")


def main():
    parser = argparse.ArgumentParser(description="Preview or send AiReady outreach emails.")
    parser.add_argument("--leads", default="outreach/leads.csv")
    parser.add_argument("--suppression", default="outreach/suppression.csv")
    parser.add_argument("--log", default="outreach/sent_log.csv")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--delay", type=float, default=5)
    parser.add_argument("--source", default="cold_email")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    leads_path = Path(args.leads)
    suppression_path = Path(args.suppression)
    log_path = Path(args.log)

    leads = read_csv(leads_path)
    suppressed = read_email_set(suppression_path)
    already_sent = read_email_set(log_path)

    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("OUTREACH_FROM", "")
    reply_to = os.environ.get("OUTREACH_REPLY_TO", "")

    if args.send:
        if args.confirm != "SEND":
            sys.exit('Refusing to send without --confirm SEND')
        if not api_key:
            sys.exit("RESEND_API_KEY is required to send.")
        if not from_email:
            sys.exit("OUTREACH_FROM is required to send.")

    sent_or_previewed = 0
    for lead in leads:
        email = (lead.get("email") or "").strip().lower()
        store = clean_store(lead.get("store", ""))
        if not email or not EMAIL_RE.match(email):
            continue
        if email in suppressed or email in already_sent:
            continue
        if not store:
            continue

        lead["email"] = email
        lead["store"] = store
        subject, text, html_body = build_message(lead, args.source)

        if not args.send:
            print("=" * 72)
            print(f"TO: {email}")
            print(f"SUBJECT: {subject}")
            print(text)
        else:
            try:
                resend_id = send_email(api_key, from_email, reply_to, lead, subject, text, html_body)
                append_log(log_path, {
                    "email": email,
                    "store": store,
                    "source": lead.get("source") or args.source,
                    "status": "sent",
                    "resend_id": resend_id,
                    "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                print(f"sent {email} {resend_id}")
            except Exception as exc:
                append_log(log_path, {
                    "email": email,
                    "store": store,
                    "source": lead.get("source") or args.source,
                    "status": "error",
                    "error": str(exc),
                    "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                print(f"error {email}: {exc}")
            time.sleep(max(0, args.delay))

        sent_or_previewed += 1
        if sent_or_previewed >= args.limit:
            break

    if sent_or_previewed == 0:
        print("No eligible leads found.")


if __name__ == "__main__":
    main()
