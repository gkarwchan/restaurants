#!/usr/bin/env python3
"""Match pending facilities to Google Places API (New) Place IDs."""
import argparse
import http.client
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id"
MAX_RETRIES = 5


class PlacesRequestError(Exception):
    """A request failed permanently or exhausted its retries."""


def database_path(database_url):
    if database_url.startswith("sqlite:////"):
        path = urllib.parse.unquote(database_url[len("sqlite:///"):])
    elif database_url.startswith("sqlite:///"):
        path = urllib.parse.unquote(database_url[len("sqlite:///"):])
    elif "://" in database_url:
        raise ValueError("This script supports SQLite DATABASE_URL values only")
    else:
        path = database_url
    if not path or path == ":memory:":
        raise ValueError("DATABASE_URL must identify a persistent SQLite database file")
    return path


def ensure_schema(db):
    """Create the relation and move any old facility-level results into it."""
    db.execute("PRAGMA foreign_keys = ON")
    facility_columns = {
        row[1] for row in db.execute('PRAGMA table_info("facilities")')
    }
    if not facility_columns:
        raise ValueError("SQLite table 'facilities' does not exist or has no columns")

    with db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS google_places (
                facilityId TEXT PRIMARY KEY NOT NULL
                    REFERENCES facilities(id) ON DELETE CASCADE,
                place_id TEXT,
                match_status TEXT,
                candidate_ids TEXT
            )
        """)

        old_columns = ("place_id", "match_status", "candidate_ids")
        present = [column for column in old_columns if column in facility_columns]
        if present:
            select_columns = [
                f'"{column}"' if column in facility_columns else "NULL"
                for column in old_columns
            ]
            db.execute(f"""
                INSERT OR IGNORE INTO google_places
                    (facilityId, place_id, match_status, candidate_ids)
                SELECT id, {", ".join(select_columns)}
                FROM facilities
                WHERE {" OR ".join(f'"{c}" IS NOT NULL' for c in present)}
            """)
            for column in present:
                db.execute(f'ALTER TABLE facilities DROP COLUMN "{column}"')


class PlacesClient:
    def __init__(self, api_key, requests_per_minute, jitter_seconds):
        self.api_key = api_key
        self.interval = 60.0 / requests_per_minute
        self.jitter_seconds = jitter_seconds
        self.next_request_at = 0.0

    def _pace(self):
        wait = self.next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.next_request_at = time.monotonic() + self.interval + random.uniform(
            0, self.jitter_seconds
        )

    @staticmethod
    def _retry_after(headers):
        value = headers.get("Retry-After") if headers else None
        if not value:
            return 0
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_time = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT")
                return max(0.0, (retry_time.replace(tzinfo=timezone.utc) -
                                 datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                return 0

    def search(self, text_query):
        body = json.dumps({"textQuery": text_query, "pageSize": 5}).encode("utf-8")
        request = urllib.request.Request(
            URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            method="POST",
        )

        for attempt in range(MAX_RETRIES + 1):
            self._pace()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or "error" in payload:
                    raise PlacesRequestError("Google Places returned an API error")
                places = payload.get("places") or []
                if not isinstance(places, list):
                    raise PlacesRequestError("Google Places returned an invalid places list")
                ids = []
                for place in places:
                    if (not isinstance(place, dict) or not isinstance(place.get("id"), str)
                            or not place["id"]):
                        raise PlacesRequestError("Google Places returned a result without an ID")
                    ids.append(place["id"])
                return ids
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code <= 599
                retry_after = self._retry_after(error.headers) if error.code == 429 else 0
                error.close()
                if not retryable or attempt == MAX_RETRIES:
                    raise PlacesRequestError(f"Google Places HTTP {error.code}") from None
                time.sleep(max(retry_after, 2 ** attempt))
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
                if attempt == MAX_RETRIES:
                    raise PlacesRequestError(
                        f"Google Places request failed: {error.reason if isinstance(error, urllib.error.URLError) else error}"
                    ) from None
                time.sleep(2 ** attempt)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PlacesRequestError(f"Invalid Google Places response: {error}") from None
        raise PlacesRequestError("Google Places request retries exhausted")


def read_pending(db, limit):
    return db.execute(
        """
        SELECT f.id, f.name, f.address_line1, f.city, f.province
        FROM facilities AS f
        LEFT JOIN google_places AS g ON g.facilityId = f.id
        WHERE g.match_status IS NULL
        ORDER BY f.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def pending_count(db):
    return db.execute(
        """
        SELECT COUNT(*)
        FROM facilities AS f
        LEFT JOIN google_places AS g ON g.facilityId = f.id
        WHERE g.match_status IS NULL
        """
    ).fetchone()[0]


def save_record(db, facility_id, ids=None, status=None, multiple_place_id="first"):
    if status == "ERROR":
        place_id, candidates = None, None
    elif not ids:
        status, place_id, candidates = "NOT_FOUND", None, None
    elif len(ids) == 1:
        status, place_id, candidates = "MATCHED", ids[0], None
    else:
        status = "MULTIPLE_MATCHES"
        place_id = ids[0] if multiple_place_id == "first" else None
        candidates = json.dumps(ids, ensure_ascii=False)

    with db:
        db.execute(
            """
            INSERT INTO google_places
                (facilityId, place_id, match_status, candidate_ids)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(facilityId) DO UPDATE SET
                place_id = excluded.place_id,
                match_status = excluded.match_status,
                candidate_ids = excluded.candidate_ids
            WHERE google_places.match_status IS NULL
            """,
            (facility_id, place_id, status, candidates),
        )
    return status


def progress(counts, started, final=False):
    elapsed = max(time.monotonic() - started, 0.001)
    rate = counts["processed"] / elapsed
    remaining = max(0, counts["total"] - counts["processed"])
    eta = remaining / rate if rate else 0
    print(
        f"{'Final' if final else 'Progress'}: {counts['processed']}/{counts['total']} "
        f"processed; matched={counts['MATCHED']}, multiple={counts['MULTIPLE_MATCHES']}, "
        f"not_found={counts['NOT_FOUND']}, errors={counts['ERROR']}, "
        f"ETA={int(eta)}s",
        file=sys.stderr,
        flush=True,
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="process at most this many facilities")
    parser.add_argument(
        "--rate-limit", type=float, default=90,
        help="maximum target request rate in requests per minute (default: 90)",
    )
    parser.add_argument(
        "--jitter-seconds", type=float, default=0.1,
        help="random additional pacing delay per request (default: 0.1)",
    )
    parser.add_argument(
        "--multiple-place-id", choices=("first", "null"), default="first",
        help="whether to save the first Place ID when there are multiple matches",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if not 0 < args.rate_limit <= 90:
        parser.error("--rate-limit must be greater than 0 and no more than 90")
    if args.jitter_seconds < 0:
        parser.error("--jitter-seconds cannot be negative")
    return args


def main():
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL", "ahs.db")
    api_key = os.environ.get("GOOGLE_GEOCODING_API_KEY")
    if not api_key:
        sys.exit("Set GOOGLE_GEOCODING_API_KEY to a Google Places API key")

    try:
        db = sqlite3.connect(database_path(database_url), timeout=30)
        db.execute("PRAGMA busy_timeout = 30000")
        ensure_schema(db)
        batch_size = min(args.limit or 1000, 1000)
        client = PlacesClient(api_key, args.rate_limit, args.jitter_seconds)
        total_pending = pending_count(db)
        total = min(total_pending, args.limit) if args.limit is not None else total_pending
        counts = {
            "total": total, "processed": 0, "MATCHED": 0,
            "MULTIPLE_MATCHES": 0, "NOT_FOUND": 0, "ERROR": 0,
        }
        started = time.monotonic()

        while args.limit is None or counts["processed"] < args.limit:
            remaining = batch_size
            if args.limit is not None:
                remaining = min(remaining, args.limit - counts["processed"])
            records = read_pending(db, remaining)
            if not records:
                break
            for facility_id, name, address, city, province in records:
                query_parts = [str(part).strip() for part in (name, address, city, province)
                               if part is not None and str(part).strip()]
                query_parts.append("Canada")
                try:
                    if len(query_parts) == 1:
                        raise PlacesRequestError("Facility has no business name or address")
                    ids = client.search(", ".join(query_parts))
                    status = save_record(
                        db, facility_id, ids=ids,
                        multiple_place_id=args.multiple_place_id,
                    )
                    counts[status] += 1
                except PlacesRequestError as error:
                    save_record(db, facility_id, status="ERROR")
                    counts["ERROR"] += 1
                    print(f"Facility {facility_id!r}: {error}", file=sys.stderr, flush=True)
                counts["processed"] += 1
                if counts["processed"] % 100 == 0:
                    progress(counts, started)
                if args.limit is not None and counts["processed"] >= args.limit:
                    break

        progress(counts, started, final=True)
        db.close()
    except (sqlite3.Error, ValueError) as error:
        sys.exit(f"Database/configuration error: {error}")


if __name__ == "__main__":
    main()
