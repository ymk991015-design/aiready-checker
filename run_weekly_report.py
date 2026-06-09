import os
import sys

import requests


def main():
    app_base_url = os.environ.get("APP_BASE_URL", "https://aiready-checker.onrender.com").rstrip("/")
    cron_secret = os.environ.get("CRON_SECRET", "")
    if not cron_secret:
        print("CRON_SECRET is required.", file=sys.stderr)
        return 1

    response = requests.post(
        f"{app_base_url}/run-weekly-scan",
        headers={"X-Cron-Secret": cron_secret},
        timeout=600,
    )
    print(f"weekly report status={response.status_code} body={response.text[:1000]}")
    return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
