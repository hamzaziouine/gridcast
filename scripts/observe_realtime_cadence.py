"""Observe the posting cadence of IESO RealtimeTotals (P0.5 evidence for REALTIME_LAG_H).

Polls once per hour (or once with --once) and appends one CSV row per checked hour file:

    polled_at_utc, file, http_status, last_modified_utc, created_at_est, he, n_intervals,
    target_time_utc, lag_vs_target_min

`target_time_utc` is the start of the hour-ending interval (HE h of EST date D starts at
D 00:00 + (h-1) h + 5 h UTC; see BUILD_PLAN §3.3). `lag_vs_target_min` is
Last-Modified - target_time in minutes, so a complete hour posted before the hour ends shows
a value below 60.

Each poll checks the current hour's file and the previous `--lookback` hours, so a file that
is completed or back-filled late is visible as a changed Last-Modified / n_intervals.

Stdlib only. Run it in a terminal you watch and stop it with Ctrl+C; nothing else starts it.

    python scripts/observe_realtime_cadence.py --out realtime_cadence.csv            # hourly
    python scripts/observe_realtime_cadence.py --once --out realtime_cadence.csv     # one poll
"""

from __future__ import annotations

import argparse
import csv
import email.utils
import os
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta, timezone

BASE = "https://reports-public.ieso.ca/public/RealtimeTotals/"
EST = timezone(timedelta(hours=-5))  # IESO market time, no DST (BUILD_PLAN §3.3)
UA = "gridcast-cadence-probe/0.1"
FIELDS = [
    "polled_at_utc",
    "file",
    "http_status",
    "last_modified_utc",
    "created_at_est",
    "he",
    "n_intervals",
    "target_time_utc",
    "lag_vs_target_min",
]
CREATED_RE = re.compile(r"CREATED AT (\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")


def hour_files(now_utc: datetime, lookback: int) -> list[tuple[str, datetime]]:
    """Return (file name, target_time_utc) for the current EST hour and `lookback` before it."""
    now_est = now_utc.astimezone(EST)
    current_start = now_est.replace(minute=0, second=0, microsecond=0)
    out = []
    for k in range(lookback + 1):
        start = current_start - timedelta(hours=k)
        he = start.hour + 1  # hour-ending 1..24
        name = f"PUB_RealtimeTotals_{start:%Y%m%d}{he:02d}.csv"
        out.append((name, start.astimezone(UTC)))
    return out


def fetch(name: str) -> tuple[int, str | None, str]:
    # BASE is a fixed https:// URL, so the scheme audit (S310) does not apply.
    req = urllib.request.Request(BASE + name, headers={"User-Agent": UA})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", "replace")
            return resp.status, resp.headers.get("Last-Modified"), body
    except urllib.error.HTTPError as exc:
        return exc.code, None, ""


def poll(writer: csv.DictWriter, lookback: int) -> None:
    polled = datetime.now(UTC)
    for name, target in hour_files(polled, lookback):
        status, last_mod, body = fetch(name)
        lm = email.utils.parsedate_to_datetime(last_mod) if last_mod else None
        created = CREATED_RE.search(body)
        rows = [line for line in body.splitlines() if line[:1].isdigit()]
        writer.writerow(
            {
                "polled_at_utc": polled.isoformat(timespec="seconds"),
                "file": name,
                "http_status": status,
                "last_modified_utc": lm.isoformat() if lm else "",
                "created_at_est": created.group(1) if created else "",
                "he": rows[0].split(",")[0] if rows else "",
                "n_intervals": len(rows),
                "target_time_utc": target.isoformat(),
                "lag_vs_target_min": (round((lm - target).total_seconds() / 60, 1) if lm else ""),
            }
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default="realtime_cadence.csv", help="CSV file to append to")
    ap.add_argument("--interval-min", type=float, default=60.0, help="minutes between polls")
    ap.add_argument(
        "--lookback", type=int, default=2, help="previous hour files to re-check each poll"
    )
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args()

    new_file = not os.path.exists(args.out)
    while True:
        with open(args.out, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
                new_file = False
            poll(writer, args.lookback)
        if args.once:
            return
        time.sleep(args.interval_min * 60)


if __name__ == "__main__":
    main()
