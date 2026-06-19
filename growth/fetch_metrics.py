import csv
import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_URL = os.environ.get("AIREADY_APP_URL", "https://aiready-checker.onrender.com").rstrip("/")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
OUTPUT = Path(os.environ.get("AIREADY_METRICS_CSV", "growth/daily_metrics.csv"))


FIELDS = [
    "date",
    "visitors",
    "scans",
    "unique_scanned_shops",
    "email_leads",
    "installs",
    "upgrade_clicks",
    "paid_shops",
    "top_source",
    "notes",
]


def read_existing(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    if not ADMIN_SECRET:
        sys.exit("Set ADMIN_SECRET before fetching metrics.")

    request = Request(f"{APP_URL}/admin/metrics", headers={"X-Admin-Secret": ADMIN_SECRET})
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        sys.exit(f"Metrics request failed: {exc.code} {exc.read().decode('utf-8', errors='replace')}")
    except URLError as exc:
        sys.exit(f"Metrics request failed: {exc}")
    summary = data.get("summary") or {}
    top_sources = data.get("top_sources") or data.get("top_visit_sources") or []
    top_source = top_sources[0].get("source", "") if top_sources else ""
    today = date.today().isoformat()

    row = {
        "date": today,
        "visitors": summary.get("total_visits", 0),
        "scans": summary.get("total_scans", 0),
        "unique_scanned_shops": summary.get("unique_scanned_shops", 0),
        "email_leads": summary.get("total_leads", 0),
        "installs": "",
        "upgrade_clicks": summary.get("upgrade_clicks", 0),
        "paid_shops": summary.get("paid_shops", 0),
        "top_source": top_source,
        "notes": "Auto-fetched from /admin/metrics",
    }

    rows = [r for r in read_existing(OUTPUT) if r.get("date") != today]
    rows.append(row)
    write_rows(OUTPUT, rows)
    print(f"Saved metrics for {today} to {OUTPUT}")


if __name__ == "__main__":
    main()
