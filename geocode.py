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
  python3 geocode.py --postal-prefix T2G --dry-run
  python3 geocode.py                         # process pending addresses within local request cap
  python3 geocode.py --history               # show recent run history and local usage

The local request ledger limits this database to 9,900 attempts in any rolling 31-day
window. It cannot account for usage by other projects or API clients on the same billing account.
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
REQUEST_WINDOW_LIMIT = 9900
REQUEST_WINDOW_DAYS = 31

SCHEMA = """
CREATE TABLE IF NOT EXISTS geocodes (
    address TEXT PRIMARY KEY,      -- matches facilities.address
    lat REAL,
    lng REAL,
    place_id TEXT,
    location_type TEXT,            -- ROOFTOP, RANGE_INTERPOLATED, GEOMETRIC_CENTER, APPROXIMATE
    formatted_address TEXT,
    status TEXT,                   -- OK or ZERO_RESULTS
    geocoded_at TEXT,
    source TEXT NOT NULL DEFAULT 'google'
);
CREATE TABLE IF NOT EXISTS geocode_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    postal_prefix TEXT,
    addresses_selected INTEGER NOT NULL DEFAULT 0,
    api_requests INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0,
    zero_results_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_request_attempts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES geocode_runs(id),
    address TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('baseline', 'request'))
);
CREATE INDEX IF NOT EXISTS ix_geocode_attempts_time ON geocode_request_attempts(attempted_at);
CREATE INDEX IF NOT EXISTS ix_geocode_attempts_address ON geocode_request_attempts(address, kind, attempted_at);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(geocodes)")}
    if "source" not in columns:
        db.execute(
            "ALTER TABLE geocodes ADD COLUMN source TEXT NOT NULL DEFAULT 'google'"
        )
    db.commit()


class Fatal(Exception):
    """Errors that won't fix themselves (bad key, API not enabled, billing off)."""


class RequestWindowLimit(Exception):
    """The local rolling request limit has been reached."""


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_window_start():
    return (datetime.now(timezone.utc) - timedelta(days=REQUEST_WINDOW_DAYS)).isoformat(
        timespec="seconds"
    )


def requests_in_window(db):
    cutoff = request_window_start()
    return db.execute(
        "SELECT COUNT(*) FROM geocode_request_attempts WHERE attempted_at >= ?", (cutoff,)
    ).fetchone()[0]


def seed_usage_baseline(db):
    cutoff = request_window_start()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            INSERT INTO geocode_request_attempts (address, attempted_at, kind)
            SELECT g.address, g.geocoded_at, 'baseline'
            FROM geocodes g
            WHERE g.geocoded_at >= ?
              AND COALESCE(g.source, 'google') = 'google'
              AND NOT EXISTS (
                  SELECT 1 FROM geocode_request_attempts a
                  WHERE a.address = g.address AND a.kind = 'request'
                    AND a.attempted_at <= g.geocoded_at
              )
              AND NOT EXISTS (
                  SELECT 1 FROM geocode_request_attempts a
                  WHERE a.address = g.address AND a.kind = 'baseline'
                    AND a.attempted_at = g.geocoded_at
              )
        """, (cutoff,))
        db.commit()
    except Exception:
        db.rollback()
        raise


def reserve_request(db, db_lock, run_id, address):
    attempted_at = now()
    cutoff = request_window_start()
    with db_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            used = db.execute(
                "SELECT COUNT(*) FROM geocode_request_attempts WHERE attempted_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            if used >= REQUEST_WINDOW_LIMIT:
                raise RequestWindowLimit
            db.execute(
                """INSERT INTO geocode_request_attempts
                   (run_id, address, attempted_at, kind) VALUES (?, ?, ?, 'request')""",
                (run_id, address, attempted_at),
            )
            db.execute(
                "UPDATE geocode_runs SET api_requests = api_requests + 1 WHERE id = ?", (run_id,)
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def finish_run(db, run_id, status, counts=None):
    counts = counts or {}
    with db:
        db.execute("""
            UPDATE geocode_runs SET completed_at=?, status=?, ok_count=?,
                zero_results_count=?, error_count=? WHERE id=?
        """, (now(), status, counts.get("OK", 0), counts.get("ZERO_RESULTS", 0),
              counts.get("error", 0), run_id))


def show_history(db):
    used = requests_in_window(db)
    print(f"Rolling {REQUEST_WINDOW_DAYS}-day window: {used}/{REQUEST_WINDOW_LIMIT} "
          f"locally tracked requests "
          f"({max(0, REQUEST_WINDOW_LIMIT - used)} remaining)")
    print("Run --dry-run or --history again later to check when capacity is available.")
    runs = db.execute("""
        SELECT id, started_at, completed_at, postal_prefix, addresses_selected,
               api_requests, ok_count, zero_results_count, error_count, status
        FROM geocode_runs ORDER BY id DESC LIMIT 20
    """).fetchall()
    if not runs:
        print("No geocoding runs recorded.")
        return
    print("Recent runs (UTC):")
    for row in runs:
        run_id, started, completed, prefix, addresses, requests, ok, zero, errors, status = row
        scope = f" postal_prefix={prefix}" if prefix else ""
        print(f"  #{run_id} {started}–{completed or 'running'}{scope}: "
              f"selected={addresses}, requests={requests}, ok={ok}, "
              f"zero_results={zero}, errors={errors}, status={status}")


class Geocoder:
    def __init__(self, key, delay, request_guard, stop):
        self.key = key
        self.delay = delay
        self.request_guard = request_guard
        self.stop = stop
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
            if self.stop.is_set():
                return None
            self._throttle()
            if self.stop.is_set():
                return None
            self.request_guard(address)
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


def pending_addresses(db, max_age_days, postal_prefix=None):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    query = """
        SELECT address FROM (
            SELECT DISTINCT f.address AS address, g.geocoded_at AS geocoded_at
            FROM facilities f
            LEFT JOIN geocodes g ON g.address = f.address
            WHERE f.address IS NOT NULL AND f.address != ''
              AND (
                  g.address IS NULL
                  OR (
                      COALESCE(g.source, 'google') = 'google'
                      AND g.geocoded_at < ?
                  )
              )
    """
    params = [cutoff]
    if postal_prefix:
        query += """
              AND substr(upper(trim(f.postal_code)), 1, length(?)) = upper(?)
        """
        params.extend((postal_prefix, postal_prefix))
    query += """
        )
        ORDER BY geocoded_at IS NOT NULL, geocoded_at, address
    """
    return [r[0] for r in db.execute(query, params)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="ahs.db")
    ap.add_argument("--key", default=os.environ.get("GOOGLE_GEOCODING_API_KEY"),
                    help="API key (default: $GOOGLE_GEOCODING_API_KEY)")
    ap.add_argument("--max-age-days", type=int, default=30, help="re-geocode entries older than this")
    ap.add_argument("--postal-prefix", help="only process facilities whose postal code starts with this")
    ap.add_argument("--limit", type=int, default=0, help="process at most this many addresses in this run")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.05, help="min seconds between requests (0.05 = 20/s)")
    ap.add_argument("--dry-run", action="store_true", help="only show how many addresses need geocoding")
    ap.add_argument("--history", action="store_true", help="show recent run history and rolling usage")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    postal_prefix = args.postal_prefix.strip().upper() if args.postal_prefix else None
    if args.postal_prefix is not None and not postal_prefix:
        ap.error("--postal-prefix must not be empty")
    if args.limit < 0:
        ap.error("--limit cannot be negative")

    db = sqlite3.connect(args.db, check_same_thread=False)
    ensure_schema(db)
    seed_usage_baseline(db)
    if args.history:
        show_history(db)
        return

    used = requests_in_window(db)
    remaining = max(0, REQUEST_WINDOW_LIMIT - used)
    pending = pending_addresses(db, args.max_age_days, postal_prefix)
    batch_limit = min(remaining, args.limit) if args.limit else remaining
    todo = pending[:batch_limit]
    facility_query = "SELECT COUNT(*) FROM facilities"
    facility_params = ()
    if postal_prefix:
        facility_query += """
            WHERE substr(upper(trim(postal_code)), 1, length(?)) = upper(?)
        """
        facility_params = (postal_prefix, postal_prefix)
    total_fac = db.execute(facility_query, facility_params).fetchone()[0]
    scope = f" with postal prefix {postal_prefix}" if postal_prefix else ""
    log(f"{total_fac} facilities{scope}; {len(pending)} pending addresses; selecting {len(todo)} "
        f"within rolling {REQUEST_WINDOW_DAYS}-day usage {used}/{REQUEST_WINDOW_LIMIT}")
    with db:
        run_id = db.execute("""
            INSERT INTO geocode_runs (started_at, postal_prefix, addresses_selected, status)
            VALUES (?, ?, ?, 'running')
        """, (now(), postal_prefix, len(todo))).lastrowid
    if args.dry_run:
        finish_run(db, run_id, "dry_run")
        return
    if not todo:
        status = "request_window_cap_reached" if pending and remaining == 0 else "no_work"
        finish_run(db, run_id, status)
        return
    if not args.key:
        finish_run(db, run_id, "missing_api_key")
        sys.exit("No API key: set GOOGLE_GEOCODING_API_KEY or pass --key")
    if not args.yes:
        if input(f"Send geocoding requests for up to {len(todo)} addresses? "
                 f"Retries count against the local {REQUEST_WINDOW_DAYS}-day limit. [y/N] ").strip().lower() != "y":
            finish_run(db, run_id, "cancelled")
            return

    db_lock = threading.Lock()
    stop = threading.Event()
    counts = {"OK": 0, "ZERO_RESULTS": 0, "error": 0}
    geo = Geocoder(args.key, args.delay,
                   lambda address: reserve_request(db, db_lock, run_id, address), stop)
    quota_reached = False
    fatal_error = False
    interrupted = False

    def work(address):
        if stop.is_set():
            return
        res = geo.geocode(address)
        if res is None:
            return
        with db_lock, db:
            db.execute("""
                INSERT OR REPLACE INTO geocodes
                    (address, lat, lng, place_id, location_type, formatted_address,
                     status, geocoded_at, source)
                VALUES (?,?,?,?,?,?,?,?, 'google')
            """, (address, res[1], res[2], res[3], res[4], res[5], res[0], now()))
            counts[res[0]] += 1

    ex = ThreadPoolExecutor(max_workers=args.workers)
    done = 0
    try:
        futs = {ex.submit(work, a): a for a in todo}
        for f in as_completed(futs):
            try:
                f.result()
            except RequestWindowLimit:
                quota_reached = True
                stop.set()
                log(f"Local {REQUEST_WINDOW_DAYS}-day request cap reached "
                    f"({REQUEST_WINDOW_LIMIT}); stopping.")
            except Fatal as e:
                fatal_error = True
                counts["error"] += 1
                stop.set()
                log(f"Google refused the request, stopping: {e}")
            except Exception as e:
                counts["error"] += 1
                log(f"  error for {futs[f]!r}: {e}")
            done += 1
            if done % 500 == 0 or done == len(todo):
                log(f"{done}/{len(todo)}  ok={counts['OK']} not_found={counts['ZERO_RESULTS']} errors={counts['error']}")
    except KeyboardInterrupt:
        interrupted = True
        stop.set()
        log("Interrupted — results so far are saved; re-run to continue.")
    finally:
        stop.set()
        ex.shutdown(wait=True, cancel_futures=True)

    if interrupted:
        run_status = "interrupted"
    elif fatal_error:
        run_status = "stopped_on_api_error"
    elif quota_reached:
        run_status = "request_window_cap_reached"
    elif counts["error"]:
        run_status = "completed_with_errors"
    else:
        run_status = "completed"
    finish_run(db, run_id, run_status, counts)

    mapped_query = """SELECT COUNT(*) FROM facilities f JOIN geocodes g
                      ON g.address = f.address AND g.status = 'OK'"""
    mapped_params = ()
    if postal_prefix:
        mapped_query += """
            WHERE substr(upper(trim(f.postal_code)), 1, length(?)) = upper(?)
        """
        mapped_params = (postal_prefix, postal_prefix)
    mapped = db.execute(mapped_query, mapped_params).fetchone()[0]
    log(f"facilities with coordinates: {mapped}/{total_fac}")


if __name__ == "__main__":
    main()
