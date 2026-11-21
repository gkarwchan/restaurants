#!/usr/bin/env python3
"""
Add map coordinates to the facilities in ahs.db using the Google Geocoding API.

Each distinct address is geocoded once (many facilities share an address, e.g. food courts)
and stored in a `geocodes` table keyed by address. Re-running only geocodes new addresses
and ones older than --max-age-days.

Google's Maps Platform terms allow caching latitude/longitude for up to 30 days, hence the
default --max-age-days 30: re-run this monthly to stay within them.

Usage:
  export GOOGLE_GEOCODING_API_KEY=...        # a key with the Geocoding API enabled
  python3 geocode.py --dry-run               # show how many addresses would be geocoded
  python3 geocode.py --limit 20              # small test
  python3 geocode.py                         # everything (asks for confirmation)

Only the Python standard library is used.
"""
import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

URL = "https://maps.googleapis.com/maps/api/geocode/json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS geocodes (
    address TEXT PRIMARY KEY,      -- matches facilities.address
    lat REAL,
    lng REAL,
    place_id TEXT,
    location_type TEXT,            -- ROOFTOP, RANGE_INTERPOLATED, GEOMETRIC_CENTER, APPROXIMATE
    formatted_address TEXT,
    status TEXT,                   -- OK or ZERO_RESULTS
    geocoded_at TEXT
);
"""


class Fatal(Exception):
    """Errors that won't fix themselves (bad key, API not enabled, billing off)."""


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Geocoder:
    def __init__(self, key, delay):
        self.key = key
        self.delay = delay
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self):
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def geocode(self, address):
        params = urllib.parse.urlencode({
            "address": address,
            "key": self.key,
            "region": "ca",
            "components": "country:CA",
        })
        for attempt in range(6):
            self._throttle()
            try:
                with urllib.request.urlopen(f"{URL}?{params}", timeout=30) as r:
                    data = json.loads(r.read())
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"  network error ({e}), retrying")
                time.sleep(min(60, 2 ** attempt * 2))
                continue
            status = data.get("status")
            if status == "OK":
                res = data["results"][0]
                loc = res["geometry"]["location"]
                return ("OK", loc["lat"], loc["lng"], res.get("place_id"),
                        res["geometry"].get("location_type"), res.get("formatted_address"))
            if status == "ZERO_RESULTS":
                return ("ZERO_RESULTS", None, None, None, None, None)
            if status in ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR"):
                time.sleep(min(60, 2 ** attempt * 2))
                continue
            # REQUEST_DENIED, INVALID_REQUEST, OVER_DAILY_LIMIT ...
            raise Fatal(f"{status}: {data.get('error_message', '')}")
        raise RuntimeError(f"gave up on address after retries: {address}")


def pending_addresses(db, max_age_days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    return [r[0] for r in db.execute("""
        SELECT DISTINCT f.address FROM facilities f
        LEFT JOIN geocodes g ON g.address = f.address
        WHERE f.address IS NOT NULL AND f.address != ''
          AND (g.address IS NULL OR g.geocoded_at < ?)
        ORDER BY f.address
    """, (cutoff,))]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="ahs.db")
    ap.add_argument("--key", default=os.environ.get("GOOGLE_GEOCODING_API_KEY"),
                    help="API key (default: $GOOGLE_GEOCODING_API_KEY)")
    ap.add_argument("--max-age-days", type=int, default=30, help="re-geocode entries older than this")
    ap.add_argument("--limit", type=int, default=0, help="only geocode this many addresses")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.05, help="min seconds between requests (0.05 = 20/s)")
    ap.add_argument("--dry-run", action="store_true", help="only show how many addresses need geocoding")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, check_same_thread=False)
    db.executescript(SCHEMA)
    todo = pending_addresses(db, args.max_age_days)
    if args.limit:
        todo = todo[:args.limit]
    total_fac = db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
    log(f"{total_fac} facilities; {len(todo)} addresses to geocode (1 Google request each)")
    if args.dry_run or not todo:
        return
    if not args.key:
        sys.exit("No API key: set GOOGLE_GEOCODING_API_KEY or pass --key")
    if not args.yes and not args.limit:
        if input(f"Send {len(todo)} requests to the Google Geocoding API (billed)? [y/N] ").strip().lower() != "y":
            return

    geo = Geocoder(args.key, args.delay)
    wlock = threading.Lock()
    stop = threading.Event()
    counts = {"OK": 0, "ZERO_RESULTS": 0, "error": 0}

    def work(address):
        if stop.is_set():
            return
        res = geo.geocode(address)
        with wlock, db:
            db.execute("INSERT OR REPLACE INTO geocodes VALUES (?,?,?,?,?,?,?,?)",
                       (address, res[1], res[2], res[3], res[4], res[5], res[0], now()))
            counts[res[0]] += 1

    ex = ThreadPoolExecutor(max_workers=args.workers)
    done = 0
    try:
        futs = {ex.submit(work, a): a for a in todo}
        for f in as_completed(futs):
            try:
                f.result()
            except Fatal as e:
                stop.set()
                log(f"Google refused the request, stopping: {e}")
                break
            except Exception as e:
                counts["error"] += 1
                log(f"  error for {futs[f]!r}: {e}")
            done += 1
            if done % 500 == 0 or done == len(todo):
                log(f"{done}/{len(todo)}  ok={counts['OK']} not_found={counts['ZERO_RESULTS']} errors={counts['error']}")
    except KeyboardInterrupt:
        log("Interrupted — results so far are saved; re-run to continue.")
    finally:
        stop.set()
        ex.shutdown(wait=False, cancel_futures=True)

    mapped = db.execute("""SELECT COUNT(*) FROM facilities f JOIN geocodes g
                           ON g.address = f.address AND g.status = 'OK'""").fetchone()[0]
    log(f"facilities with coordinates: {mapped}/{total_fac}")


if __name__ == "__main__":
    main()
