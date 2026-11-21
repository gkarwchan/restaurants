#!/usr/bin/env python3
"""
Download all Alberta Health Services environmental health inspection data
(https://ephisahs.albertahealthservices.ca) into a SQLite database.

Phases (each is resumable — re-run the script and it continues where it stopped):
  1. facilities   – the full facility list (Power Pages entity-grid JSON endpoint)
  2. inspections  – last ~3 years of inspections for every facility
  3. violations   – violations/comments for every inspection

Usage:
  python3 ahs_scraper.py                     # everything, into ahs.db
  python3 ahs_scraper.py --phase facilities  # just one phase
  python3 ahs_scraper.py --limit 50          # only process 50 items per phase (testing)
  python3 ahs_scraper.py --workers 4 --delay 0.25
  python3 ahs_scraper.py --refresh           # re-fetch inspections/violations already downloaded
  python3 ahs_scraper.py --export-csv out/   # dump tables to CSV

Only the Python standard library is used.
"""
import argparse
import base64
import csv
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE = "https://ephisahs.albertahealthservices.ca"
LIST_PAGE = BASE + "/inspections/all/"
TOKEN_URL = BASE + "/_layout/tokenhtml"
GRID_URL = BASE + "/_services/entity-grid-data.json/7b138792-1090-45b6-9241-8f8d96d8c372"
INSPECTIONS_URL = BASE + "/getlast3yearsinspectionsbyfacilityid/?facilityid={id}&facilitygroup={group}"
VIOLATIONS_URL = BASE + "/getviolationsandcommentsbyinspectionid/?inspectionid={id}"

UA = "Mozilla/5.0 (compatible; ahs-inspections-downloader/1.0)"
INSPECTION_DATE_ATTR = "a_46f410ddc4cf4f7fb8ad97e0266cebc8.fs_inspectiondate"

SCHEMA = """
CREATE TABLE IF NOT EXISTS facilities (
    id TEXT PRIMARY KEY,
    name TEXT,
    facility_group TEXT,
    area TEXT,
    community TEXT,
    address_line1 TEXT,
    city TEXT,
    province TEXT,
    postal_code TEXT,
    country TEXT,
    address TEXT,
    last_inspection_date TEXT,
    status TEXT,
    detail_url TEXT,
    raw_json TEXT,
    listed_at TEXT,
    inspections_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS inspections (
    id TEXT PRIMARY KEY,
    facility_id TEXT REFERENCES facilities(id),
    inspection_number TEXT,
    inspection_date TEXT,       -- ISO yyyy-mm-dd
    inspection_date_text TEXT,  -- as shown on the site
    inspection_type TEXT,
    action_taken TEXT,
    raw_json TEXT,
    violations_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS violations (
    id TEXT PRIMARY KEY,
    inspection_id TEXT REFERENCES inspections(id),
    inspection_date_text TEXT,
    category TEXT,
    question TEXT,
    critical TEXT,
    status TEXT,
    comments TEXT,
    inspector_comment TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_insp_facility ON inspections(facility_id);
CREATE INDEX IF NOT EXISTS ix_viol_inspection ON violations(inspection_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def clean(s):
    """Turn the site's HTML-ish text into plain text."""
    if s is None:
        return None
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def loads_lenient(text):
    """The custom endpoints emit hand-templated JSON; tolerate trailing commas / control chars."""
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([\]}])", r"\1", text)
        return json.loads(fixed, strict=False)


class Client:
    def __init__(self, delay=0.25, retries=5, timeout=60):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self._lock = threading.Lock()
        self._last = 0.0
        self.token = None
        self.secure_config = None

    def _throttle(self):
        # Global rate limit across threads: at most one request every `delay` seconds.
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def request(self, url, data=None, headers=None):
        hdrs = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
        hdrs.update(headers or {})
        last_err = None
        attempt = -1
        while attempt + 1 < self.retries:
            attempt += 1
            self._throttle()
            try:
                req = urllib.request.Request(url, data=data, headers=hdrs)
                with self.opener.open(req, timeout=self.timeout) as r:
                    body = r.read().decode("utf-8", "replace")
                self._outage_start = None
                return body
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (400, 401, 403, 404) and attempt >= 1:
                    raise
            except urllib.error.URLError as e:  # DNS failure / no network (e.g. laptop asleep)
                last_err = e
                outage_start = getattr(self, "_outage_start", None) or time.monotonic()
                self._outage_start = outage_start
                if time.monotonic() - outage_start < 3600:  # wait up to 1h for the network to return
                    log(f"  network error, waiting 30s: {e}")
                    time.sleep(30)
                    attempt -= 1  # network outages don't use up retries
                    continue
                raise
            except Exception as e:  # timeouts, connection resets
                last_err = e
            backoff = min(60, 2 ** attempt * 2)
            log(f"  retry {attempt + 1}/{self.retries} in {backoff}s: {last_err}")
            time.sleep(backoff)
        raise last_err

    # ---- facility list (Power Pages entity grid) ----
    def init_grid_session(self):
        page = self.request(LIST_PAGE)
        m = re.search(r"data-view-layouts='([^']+)'", page)
        if not m:
            raise RuntimeError("Could not find grid configuration on the list page (site changed?)")
        layouts = json.loads(base64.b64decode(m.group(1)))
        self.secure_config = layouts[0]["Base64SecureConfiguration"]
        tok_html = self.request(TOKEN_URL)
        m = re.search(r'value="([^"]+)"', tok_html)
        if not m:
            raise RuntimeError("Could not get anti-forgery token")
        self.token = m.group(1)

    def grid_page(self, page, page_size):
        body = {
            "base64SecureConfiguration": self.secure_config,
            "sortExpression": "name ASC, accountid ASC",
            "search": None, "page": page, "pageSize": page_size, "pagingCookie": "",
            "filter": None, "metaFilter": None, "timezoneOffset": 420,
            "customParameters": [], "entityName": "account", "entityId": None,
        }
        txt = self.request(GRID_URL, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json; charset=utf-8",
            "__RequestVerificationToken": self.token,
            "X-Requested-With": "XMLHttpRequest",
        })
        return json.loads(txt)

    def inspections(self, facility_id, group):
        url = INSPECTIONS_URL.format(id=facility_id, group=urllib.parse.quote(group or ""))
        return loads_lenient(self.request(url)).get("results", [])

    def violations(self, inspection_id):
        return loads_lenient(self.request(VIOLATIONS_URL.format(id=inspection_id))).get("results", [])


def open_db(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    return db


# ---------------------------------------------------------------- phase 1
def phase_facilities(client, db, page_size, limit):
    client.init_grid_session()
    start = int((db.execute("SELECT value FROM meta WHERE key='facilities_next_page'").fetchone() or [1])[0])
    if start > 1:
        log(f"Resuming facility list at page {start}")
    page, total = start, 0
    while True:
        try:
            d = client.grid_page(page, page_size)
        except urllib.error.HTTPError as e:
            if e.code in (400, 403, 500):  # token/session expired -> refresh once
                log("  grid session refresh")
                client.init_grid_session()
                d = client.grid_page(page, page_size)
            else:
                raise
        recs = d.get("Records") or []
        ts = now()
        rows = []
        for r in recs:
            a = {x["Name"]: x.get("DisplayValue") for x in r["Attributes"]}
            rows.append((
                r["Id"], a.get("name"), a.get("fs_facilitygroup"), a.get("fs_area"),
                a.get("fs_communityneighbourhood"), a.get("address1_line1"), a.get("address1_city"),
                a.get("address1_stateorprovince"), a.get("address1_postalcode"), a.get("address1_country"),
                (a.get("address1_composite") or "").replace("\r\n", ", "), a.get(INSPECTION_DATE_ATTR),
                a.get("statuscode"), f"{BASE}/facilitydetails/?id={r['Id']}", json.dumps(a), ts,
            ))
        with db:
            db.executemany("""
                INSERT INTO facilities (id,name,facility_group,area,community,address_line1,city,province,
                    postal_code,country,address,last_inspection_date,status,detail_url,raw_json,listed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, facility_group=excluded.facility_group,
                    area=excluded.area, community=excluded.community, address_line1=excluded.address_line1,
                    city=excluded.city, province=excluded.province, postal_code=excluded.postal_code,
                    country=excluded.country, address=excluded.address,
                    last_inspection_date=excluded.last_inspection_date, status=excluded.status,
                    raw_json=excluded.raw_json, listed_at=excluded.listed_at
            """, rows)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('facilities_next_page', ?)", (str(page + 1),))
        total += len(rows)
        count = db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
        log(f"facilities page {page}: +{len(rows)} (db total {count})")
        if not recs or not d.get("MoreRecords") or (limit and total >= limit):
            break
        page += 1
    if not limit:
        with db:
            db.execute("DELETE FROM meta WHERE key='facilities_next_page'")  # completed; next run starts fresh
            db.execute("INSERT OR REPLACE INTO meta VALUES ('facilities_completed_at', ?)", (now(),))


# ---------------------------------------------------------------- phase 2
def iso_date(order, text):
    if order and len(order) == 8 and order.isdigit():
        return f"{order[:4]}-{order[4:6]}-{order[6:]}"
    try:
        return datetime.strptime(text, "%b %d, %Y").date().isoformat()
    except Exception:
        return None


def run_parallel(items, fn, workers, label):
    done = errors = 0
    total = len(items)
    t0 = time.time()
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(fn, it): it for it in items}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                errors += 1
                log(f"  {label} error for {futs[f][0]}: {e}")
            done += 1
            if done % 100 == 0 or done == total:
                rate = done / max(1e-9, time.time() - t0)
                eta = (total - done) / rate if rate else 0
                log(f"{label}: {done}/{total} ({errors} errors) {rate:.1f}/s, ETA {eta/60:.0f} min")
    finally:
        # On Ctrl+C / SIGTERM, drop queued work instead of waiting for all of it.
        ex.shutdown(wait=False, cancel_futures=True)


def phase_inspections(client, db, workers, limit, refresh):
    q = "SELECT id, facility_group FROM facilities"
    if not refresh:
        q += " WHERE inspections_fetched_at IS NULL"
    items = db.execute(q + " ORDER BY name").fetchall()
    if limit:
        items = items[:limit]
    log(f"Fetching inspections for {len(items)} facilities")
    wlock = threading.Lock()

    def work(item):
        fid, group = item
        res = client.inspections(fid, group)
        rows = [(
            r.get("inspectionid"), fid, r.get("inspectionname"),
            iso_date(r.get("inspectiondateorder"), r.get("inspectiondate")), r.get("inspectiondate"),
            clean(r.get("inspectiontype")), clean(r.get("actiontaken")), json.dumps(r),
        ) for r in res if r.get("inspectionid")]
        with wlock, db:
            db.executemany("""
                INSERT INTO inspections (id,facility_id,inspection_number,inspection_date,
                    inspection_date_text,inspection_type,action_taken,raw_json)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET facility_id=excluded.facility_id,
                    inspection_number=excluded.inspection_number, inspection_date=excluded.inspection_date,
                    inspection_date_text=excluded.inspection_date_text,
                    inspection_type=excluded.inspection_type, action_taken=excluded.action_taken,
                    raw_json=excluded.raw_json
            """, rows)
            db.execute("UPDATE facilities SET inspections_fetched_at=? WHERE id=?", (now(), fid))

    run_parallel(items, work, workers, "inspections")


# ---------------------------------------------------------------- phase 3
def phase_violations(client, db, workers, limit, refresh):
    q = "SELECT id FROM inspections"
    if not refresh:
        q += " WHERE violations_fetched_at IS NULL"
    items = db.execute(q + " ORDER BY inspection_date DESC").fetchall()
    if limit:
        items = items[:limit]
    log(f"Fetching violations for {len(items)} inspections")
    wlock = threading.Lock()

    def work(item):
        (iid,) = item
        res = client.violations(iid)
        rows = [(
            r.get("complianceid"), iid, r.get("inspectiondate"), clean(r.get("category")),
            clean(r.get("question")), r.get("critical"), clean(r.get("status")),
            clean(r.get("comments")), clean(r.get("responsecomment")), json.dumps(r),
        ) for r in res if r.get("complianceid")]
        with wlock, db:
            db.executemany("""
                INSERT OR REPLACE INTO violations (id,inspection_id,inspection_date_text,category,question,
                    critical,status,comments,inspector_comment,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, rows)
            db.execute("UPDATE inspections SET violations_fetched_at=? WHERE id=?", (now(), iid))

    run_parallel(items, work, workers, "violations")


def export_csv(db, outdir):
    os.makedirs(outdir, exist_ok=True)
    for t in ("facilities", "inspections", "violations"):
        cur = db.execute(f"SELECT * FROM {t}")
        cols = [c[0] for c in cur.description if c[0] != "raw_json"]
        idx = [i for i, c in enumerate(cur.description) if c[0] != "raw_json"]
        path = os.path.join(outdir, f"{t}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            n = 0
            for row in cur:
                w.writerow([row[i] for i in idx])
                n += 1
        log(f"wrote {path} ({n} rows)")


def summary(db):
    f = db.execute("SELECT COUNT(*), SUM(inspections_fetched_at IS NOT NULL) FROM facilities").fetchone()
    i = db.execute("SELECT COUNT(*), SUM(violations_fetched_at IS NOT NULL) FROM inspections").fetchone()
    v = db.execute("SELECT COUNT(*), SUM(critical='Yes') FROM violations").fetchone()
    log(f"facilities: {f[0]} ({f[1] or 0} with inspections fetched)")
    log(f"inspections: {i[0]} ({i[1] or 0} with violations fetched)")
    log(f"violations: {v[0]} ({v[1] or 0} critical)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="ahs.db")
    ap.add_argument("--phase", choices=["all", "facilities", "inspections", "violations"], default="all")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests (default 4)")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="minimum seconds between requests across all workers (default 0.25 = max 4 req/s)")
    ap.add_argument("--page-size", type=int, default=500)
    ap.add_argument("--limit", type=int, default=0, help="max items per phase (for testing)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch already downloaded inspections/violations")
    ap.add_argument("--export-csv", metavar="DIR", help="export tables to CSV and exit")
    args = ap.parse_args()

    db = open_db(args.db)
    if args.export_csv:
        export_csv(db, args.export_csv)
        return
    client = Client(delay=args.delay)
    try:
        if args.phase in ("all", "facilities"):
            phase_facilities(client, db, args.page_size, args.limit)
        if args.phase in ("all", "inspections"):
            phase_inspections(client, db, args.workers, args.limit, args.refresh)
        if args.phase in ("all", "violations"):
            phase_violations(client, db, args.workers, args.limit, args.refresh)
    except KeyboardInterrupt:
        log("Interrupted — progress is saved; re-run to resume.")
    summary(db)


if __name__ == "__main__":
    main()
