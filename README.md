# AHS inspections downloader

Downloads all facilities, inspections (last ~3 years) and violations from
https://ephisahs.albertahealthservices.ca into a SQLite database (`ahs.db`).

- `ahs_scraper.py`: the downloader (Python standard library only). Resumable: re-run it to continue.
- `cloud_run.py`: Cloud Run Job entrypoint. Restores `ahs.db` from Cloud Storage, runs the
  downloader, uploads a snapshot every 15 minutes and at the end.

## Run the scraper

Needs Python 3.9 or newer. Nothing to install. Works on macOS and Linux.

### 1. Quick test (a few minutes)

```sh
python3 ahs_scraper.py --limit 50
```

Downloads inspections and violations for 50 facilities, so you can check that it works and
see what the data looks like.

### 2. Full download (about 6–8 hours)

```sh
python3 ahs_scraper.py 2>&1 | tee run.log
```

It runs three steps, one after the other, and prints progress with an ETA:

1. **Facilities**: the full list (a few minutes).
2. **Inspections**: one request per facility (about 1.5–2 hours).
3. **Violations**: one request per inspection (about 4–5 hours).

It sends at most 4 requests per second, to go easy on the AHS website.

**Stopping and resuming:** press `Ctrl+C` at any time. Progress is saved, and running the
same command again continues where it stopped.

### 3. Keep it running for hours

The run stops if the machine sleeps or if you close the terminal.

**On a Mac**, prevent sleep while it runs:

```sh
caffeinate -i python3 ahs_scraper.py 2>&1 | tee run.log
```

**On Linux**, run it inside `tmux` so it keeps going if you disconnect:

```sh
tmux new -s ahs
python3 ahs_scraper.py 2>&1 | tee run.log
# leave tmux running: press Ctrl+B, then D
# come back later:   tmux attach -t ahs
```

To stop a Linux desktop or laptop from sleeping, run this in a second terminal and leave
it open (press `Ctrl+C` there when the download is done):

```sh
systemd-inhibit sleep infinity
```

Servers and cloud VMs normally don't sleep, so they don't need this.

### 4. Check progress

From another terminal, in the same folder:

```sh
python3 -c "import sqlite3; db=sqlite3.connect('ahs.db'); print([db.execute(f'select count(*) from {t}').fetchone()[0] for t in ('facilities','inspections','violations')])"
```

This prints the number of facilities, inspections and violations downloaded so far.

### 5. The result

Everything is saved in one SQLite file, `ahs.db`, with three tables:

| Table | One row per | Main columns |
|---|---|---|
| `facilities` | facility | name, type (Food, Personal Services, Aquatics, Social Care), address, city, postal code, community, area, last inspection date, link to its AHS page |
| `inspections` | inspection (last ~3 years) | facility, date, type, action taken |
| `violations` | violation | inspection, category, critical (Yes/No), status, regulation, inspector's comment |

To get CSV files you can open in Excel:

```sh
python3 ahs_scraper.py --export-csv out/
```

This writes `out/facilities.csv`, `out/inspections.csv` and `out/violations.csv`.

### Options

| Option | What it does |
|---|---|
| `--limit N` | process only N items per step (for testing) |
| `--phase facilities\|inspections\|violations` | run only one step |
| `--refresh` | download inspections and violations again, including ones already saved |
| `--db FILE` | use a different database file (default `ahs.db`) |
| `--workers N` / `--delay S` | parallel requests and minimum seconds between requests (defaults 4 and 0.25) |
| `--export-csv DIR` | export the tables to CSV and exit |

### Moving to another machine

Copy `ahs_scraper.py` and, to continue an earlier run, `ahs.db`. Stop the scraper first.
If `ahs.db-wal` exists next to `ahs.db`, copy it too, otherwise the newest data may be missing.
Don't run the scraper on two machines at once.

## Map web app

Needs `ahs.db` with coordinates, plus two Google Maps Platform API keys (one key with both APIs
also works for testing):

- **Geocoding API** key, used once by `geocode.py` (restrict it to the Geocoding API).
- **Maps JavaScript API** key, used by the browser (restrict it to your site's HTTP referrers).

```sh
# 1. Add available local coordinates without Google API calls
python3 geocode_local.py --dry-run
python3 geocode_local.py

# 2. Geocode remaining addresses (Google bills per request; local results are retained)
export GOOGLE_GEOCODING_API_KEY=...
python3 geocode.py --dry-run                    # preview this batch
python3 geocode.py --postal-prefix T2G --dry-run # preview only T2G
python3 geocode.py --postal-prefix T2G          # geocode only T2G
python3 geocode.py                              # next pending batch
python3 geocode.py --history                    # run times and counts

# 3. Run the app
python3 -m venv .venv && .venv/bin/pip install -r web/requirements.txt
cd web
GOOGLE_MAPS_BROWSER_KEY=... ../.venv/bin/uvicorn app:app --port 8000
# open http://localhost:8000
```

The Google geocoder selects addresses with no geocode row first, then the oldest stale
Google results, up to 9,900 locally tracked request attempts in any rolling 31-day window.
Retries count toward that limit. Run timestamps, selected addresses, requests, and outcomes
are recorded in `geocode_runs`; `--history` displays recent runs and this database's recent usage.
The local importer prefers Calgary, then Edmonton, then the province-wide municipal address
DBFs (`address.dbf` and `address_1.dbf`), and Alberta ODA points last. It imports only
unambiguous matches, never replaces existing geocode rows, and tags imported coordinates by
source. Google refreshes continue to apply only to Google-sourced rows. Override the municipal
data directory with `--full-municipality-dir`.

This local guard cannot see usage from other projects or clients on the Google billing
account, so it cannot guarantee a free bill. Google Geocoding results are only cached for
up to 30 days; spreading a large dataset across batches does not let all coordinates remain
current and available for free at once. Check Google Cloud billing before each batch.

Optional: `GOOGLE_MAPS_MAP_ID` (a Map ID from the Google Cloud console; defaults to `DEMO_MAP_ID`)
and `AHS_DB` (database path; defaults to `../ahs.db`).

Marker colours show the result of each facility's latest inspection: red = critical violation,
orange = violation, green = none, grey = no inspection data yet.

## Run the downloader on Google Cloud Run (as a Job)

```sh
PROJECT=my-project
REGION=northamerica-northeast2        # Toronto; pick any region
BUCKET=my-ahs-data                    # must be globally unique
SA=ahs-scraper@$PROJECT.iam.gserviceaccount.com

gcloud config set project $PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# Bucket for the database, and a service account that can write to it
gcloud storage buckets create gs://$BUCKET --location=$REGION
gcloud iam service-accounts create ahs-scraper
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$SA --role=roles/storage.objectAdmin

# Optional: start from the database already downloaded on this machine
gcloud storage cp ahs.db gs://$BUCKET/ahs.db

# Build the image with Cloud Build and create the job
gcloud run jobs deploy ahs-scraper --source . --region=$REGION \
  --service-account=$SA \
  --set-env-vars=GCS_BUCKET=$BUCKET \
  --tasks=1 --max-retries=3 --task-timeout=24h \
  --cpu=1 --memory=2Gi

# Quick test first (checks the AHS site accepts requests from Google Cloud)
gcloud run jobs execute ahs-scraper --region=$REGION --args="--limit,50" --wait

# Full run
gcloud run jobs execute ahs-scraper --region=$REGION
```

Notes:

- Keep `--tasks=1`: there is a single SQLite file, so tasks can't run in parallel.
- Cloud Run's disk is held in memory and counts toward `--memory`. The database plus the
  upload snapshot need roughly twice the database size. Raise `--memory` if the task runs out.
- If a task fails or times out, the retry restores the latest snapshot and resumes.
- Logs: Cloud Run console → Jobs → ahs-scraper → Logs.
- Get the result: `gcloud storage cp gs://$BUCKET/ahs.db .`
- To run on a schedule, add a Cloud Scheduler trigger to the job (Cloud Run console → Jobs → Triggers).
