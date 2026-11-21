#!/usr/bin/env python3
"""
Cloud Run Job entrypoint.

1. Downloads the previous ahs.db from Cloud Storage (if there is one), so runs resume.
2. Runs ahs_scraper.py with any arguments passed to this script.
3. Uploads a snapshot of the database every UPLOAD_EVERY_MIN minutes, and again at the end
   (also when Cloud Run stops the task with SIGTERM).

Environment variables:
  GCS_BUCKET        bucket name (no gs://). If unset, nothing is downloaded/uploaded (local testing).
  GCS_OBJECT        object name in the bucket (default: ahs.db)
  DB_PATH           local database path (default: /data/ahs.db)
  UPLOAD_EVERY_MIN  minutes between snapshot uploads (default: 15)
  GOOGLE_GEOCODING_API_KEY  if set, geocode.py runs after the download to add map coordinates
"""
import os
import signal
import sqlite3
import subprocess
import sys
import threading

import ahs_scraper

BUCKET = os.environ.get("GCS_BUCKET")
OBJECT = os.environ.get("GCS_OBJECT", "ahs.db")
DB = os.environ.get("DB_PATH", "/data/ahs.db")
UPLOAD_EVERY = int(os.environ.get("UPLOAD_EVERY_MIN", "15")) * 60
log = ahs_scraper.log

_upload_lock = threading.Lock()
_terminated = False


def _blob():
    from google.cloud import storage  # only needed when a bucket is configured
    return storage.Client().bucket(BUCKET).blob(OBJECT)


def download():
    if not BUCKET:
        log("GCS_BUCKET not set: running without Cloud Storage sync")
        return
    blob = _blob()
    if blob.exists():
        blob.download_to_filename(DB)
        log(f"restored gs://{BUCKET}/{OBJECT} ({os.path.getsize(DB) / 1e6:.1f} MB)")
    else:
        log(f"gs://{BUCKET}/{OBJECT} not found: starting with an empty database")


def upload(reason):
    if not BUCKET or not os.path.exists(DB):
        return
    with _upload_lock:
        # Copy through SQLite's backup API on a separate connection, so the upload is a
        # consistent snapshot of committed data even while the scraper is writing.
        snap = DB + ".snapshot"
        src, dst = sqlite3.connect(DB), sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        _blob().upload_from_filename(snap, timeout=600)
        os.remove(snap)
        log(f"uploaded snapshot to gs://{BUCKET}/{OBJECT} ({reason})")


def periodic_upload(stop):
    while not stop.wait(UPLOAD_EVERY):
        try:
            upload("periodic")
        except Exception as e:
            log(f"periodic upload failed (will retry): {e}")


def on_sigterm(signum, frame):
    global _terminated
    _terminated = True
    log("SIGTERM received: stopping and saving")
    raise KeyboardInterrupt  # the scraper catches this, saves progress and returns


def main():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    signal.signal(signal.SIGTERM, on_sigterm)
    download()

    stop = threading.Event()
    threading.Thread(target=periodic_upload, args=(stop,), daemon=True).start()

    sys.argv = ["ahs_scraper.py", "--db", DB] + sys.argv[1:]
    exit_code = 0
    try:
        ahs_scraper.main()
        if os.environ.get("GOOGLE_GEOCODING_API_KEY") and not _terminated:
            # Add/refresh map coordinates for the web app (only new or >30-day-old addresses).
            log("running geocode.py")
            subprocess.run([sys.executable, "geocode.py", "--db", DB, "--yes"], check=True)
    except Exception as e:
        log(f"scraper failed: {e!r}")
        exit_code = 1  # non-zero lets Cloud Run retry; the retry resumes from the saved db
    finally:
        stop.set()
        upload("final")
    if _terminated:
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
