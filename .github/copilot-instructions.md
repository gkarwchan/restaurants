# Copilot Instructions

## Repository overview

This repository downloads Alberta Health Services inspection data and displays it in a web map.

- `ahs_scraper.py` is the standalone downloader. It uses only the Python standard library and writes `facilities`, `inspections`, and `violations` tables to SQLite.
- `geocode.py` enriches facilities with coordinates in `geocodes` and maintains geocoding run/request history in the SQLite database.
- `cloud_run.py` adapts the downloader for a Google Cloud Run Job: restore/upload SQLite snapshots to Cloud Storage, run the scraper, optionally geocode, and upload a final snapshot.
- `web/app.py` is a read-only FastAPI app that serves compact facility data and facility detail records from SQLite.
- `web/static/` contains the browser-only Google Maps UI. Facility filtering and marker rendering are in `app.js`.

The SQLite database is the integration boundary. The downloader, geocoder, web app, and Cloud Run entrypoint may all operate on the same `ahs.db`; normal `open_db()`/geocoder usage enables WAL mode, while the web app opens it read-only.

## Map app details

- `web/app.py` serves `/` and a runtime-generated `/config.js`; the latter exposes the browser Maps key and Map ID, not the geocoding key.
- The browser fetches `/api/facilities` once, builds map markers from the compact response, and fetches `/api/facilities/{facility_id}` when a marker is selected. Keep this contract aligned with `web/static/app.js`.
- The map uses the Maps JavaScript API's `maps` and `marker` libraries, advanced markers, and the marker-clusterer script loaded from `unpkg.com` in `web/static/index.html`.
- A map can run without coordinates or a browser key, but it will have no mapped facilities or show the key setup notice. Geocoding and map display use distinct Google API keys and credentials.

## Setup and commands

The downloader needs Python 3.9+ and has no Python dependencies:

```sh
python3 ahs_scraper.py --limit 50
python3 ahs_scraper.py --phase facilities
python3 ahs_scraper.py --phase inspections --limit 50
python3 ahs_scraper.py --phase violations --limit 50
```

Use `--limit N` for a small end-to-end check. `--limit` applies per selected phase. `--refresh` re-fetches data already marked complete.

Full download and CSV export:

```sh
python3 ahs_scraper.py 2>&1 | tee run.log
python3 ahs_scraper.py --export-csv out/
```

The web app dependencies are separate:

```sh
python3 -m venv .venv
.venv/bin/pip install -r web/requirements.txt
```

Run it with the repository-local convenience script:

```sh
GOOGLE_MAPS_BROWSER_KEY=... ./run-web.sh
```

Or run directly:

```sh
cd web
GOOGLE_MAPS_BROWSER_KEY=... ../.venv/bin/uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

The web app defaults to `../ahs.db` from `web/`; `AHS_DB` can override the path. `run-web.sh` resolves relative `AHS_DB` values from the repository root and validates that the file is a real SQLite database.

Geocoding requires `GOOGLE_GEOCODING_API_KEY`:

```sh
python3 geocode.py --dry-run
python3 geocode.py --postal-prefix T2G --dry-run
python3 geocode.py --postal-prefix T2G --yes
python3 geocode.py --history
```

The geocoder intentionally tracks and limits locally observed requests. Its default 31-day ledger cap is 9,900 attempts and cache entries are refreshed after 30 days.

Cloud Run:

```sh
docker build -t ahs-scraper .
pip install -r requirements.txt
GCS_BUCKET=... DB_PATH=/tmp/ahs.db python3 cloud_run.py --limit 50
```

Cloud deployment commands and required GCS service-account setup are documented in `README.md`. For the web image, build from `web/` only after copying a usable `ahs.db` into that directory:

```sh
cp ahs.db web/ahs.db
docker build -t ahs-web web/
```

There is currently no test suite, lint configuration, or package script. There is no `pytest` dependency or single-test selector. For downloader changes, use `python3 ahs_scraper.py --limit N` or the narrowest `--phase`; for geocoder selection changes, use `python3 geocode.py --dry-run` (never use a live run as a harmless test). For web changes, start the app with `./run-web.sh` and check `http://127.0.0.1:8000/`, `/config.js`, `/api/facilities`, and a facility detail route in a browser or with `curl`. API routes read the actual SQLite schema and data, so use a representative local database.

For interactive browser checks, the repository `.mcp.json` configures the Playwright MCP server for Copilot CLI. It uses `npx` and requires Node.js 20+; after changing MCP config, reload the Copilot CLI session. The CLI loads `.mcp.json` only from a trusted workspace.

## Architecture and data flow

1. `ahs_scraper.py` obtains a Power Pages grid session and downloads facilities from the AHS entity-grid endpoint.
2. Inspection fetches are keyed by `facilities.id`; violation fetches are keyed by `inspections.id`.
3. Raw endpoint payloads are retained in `raw_json` columns while normalized fields are stored in dedicated columns.
4. `geocode.py` caches one coordinate record per distinct `facilities.address` in `geocodes`.
5. `/api/facilities` joins facilities to successful geocodes and performs latest-inspection and violation aggregates in SQL. Its JSON shape is a compact `fields`/`rows` pair consumed by `web/static/app.js`.
6. `/api/facilities/{facility_id}` supplies the detail panel and all retained inspections and violations for one facility.

Use existing SQLite tables and SQL shape as the contract between these components. Do not add a separate ORM or service layer unless the task explicitly requires it.

## Key conventions

- Database work is resumable. Facility pagination uses `meta.facilities_next_page`; inspection and violation completion is tracked by `inspections_fetched_at` and `violations_fetched_at`. Preserve these markers and `ON CONFLICT` behavior when changing fetch logic.
- `--refresh` is the explicit opt-in for re-fetching records whose completion timestamps are already set. Normal reruns process only incomplete records.
- Downloader and geocoder network operations are thread-pooled and use shared, lock-protected throttling. SQLite writes in worker paths use a write lock plus `with db:` transactions.
- `Ctrl+C` and Cloud Run `SIGTERM` must leave committed rows usable and queued work discarded. Cloud snapshot consistency uses SQLite's backup API, not a raw file copy.
- HTML from the AHS API is cleaned by `clean()`/`loads_lenient()` conventions; dates should remain ISO `yyyy-mm-dd` in normalized columns.
- Facility type values are `Food`, `Personal Services`, `Aquatics`, and `Social Care`. Marker status is derived in `statusOf()` in `web/static/app.js`.
- Browser-rendered AHS text is escaped with `esc()` before HTML interpolation. Preserve that when extending detail markup.
- `/api/facilities` deliberately sends column-oriented compact rows for scale. If its result shape or `FACILITY_FIELDS` changes, update the `app.js` row-to-object mapping in the same change.
- Geocodes are keyed by the exact `facilities.address` value, so address normalization changes must be consistent across lookup and persistence. The `--dry-run` and `--history` modes report locally tracked usage; a `--yes` live run may incur Google API charges, and the local quota ledger cannot see usage from other projects or clients.
- Cloud Run restores the configured GCS object before scraping and uploads SQLite backup snapshots periodically and at completion. `GOOGLE_GEOCODING_API_KEY` enables geocoding after scraping; without it, the job only updates inspection data.
- The API key values are supplied at runtime: `GOOGLE_MAPS_BROWSER_KEY` is sent to the browser through `/config.js`, whereas `GOOGLE_GEOCODING_API_KEY` is used server-side by `geocode.py`. Never put either key in source, instruction files, or browser code.
- Environment variables: `GOOGLE_MAPS_BROWSER_KEY`, `GOOGLE_MAPS_MAP_ID`, `AHS_DB`, `GOOGLE_GEOCODING_API_KEY`, `GCS_BUCKET`, `GCS_OBJECT`, `DB_PATH`, and `UPLOAD_EVERY_MIN`. Keep real API keys and credentials out of tracked files.
- `ahs.db` is Git LFS-managed. A missing or pointer-only database should be restored with `git lfs pull`; when copying a live database, include `ahs.db-wal` if it exists and stop the writer first.
- Do not run multiple downloader processes against the same database or Cloud Storage object. Keep Cloud Run jobs at one task because SQLite is single-file and shared state cannot be partitioned safely.
