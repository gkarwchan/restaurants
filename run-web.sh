#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
UVICORN="$ROOT_DIR/.venv/bin/uvicorn"

if [[ ! -x "$UVICORN" || ! -x "$PYTHON" ]]; then
  echo "Project virtual environment not found. Create it and install web/requirements.txt first." >&2
  exit 1
fi

if [[ -n "${AHS_DB:-}" ]]; then
  case "$AHS_DB" in
    /*) DB_PATH="$AHS_DB" ;;
    *) DB_PATH="$PWD/$AHS_DB" ;;
  esac
else
  DB_PATH="$ROOT_DIR/ahs.db"
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

if ! "$PYTHON" -c 'import sys; f = open(sys.argv[1], "rb"); sys.exit(f.read(16) != b"SQLite format 3\0")' "$DB_PATH"; then
  echo "Not a SQLite database: $DB_PATH" >&2
  echo "If this is the Git LFS pointer, install Git LFS and run 'git lfs pull' from the repository root." >&2
  exit 1
fi

if [[ -z "${GOOGLE_MAPS_BROWSER_KEY:-}" ]]; then
  echo "Warning: GOOGLE_MAPS_BROWSER_KEY is unset; the map will show a setup notice." >&2
fi

export AHS_DB="$DB_PATH"
cd "$ROOT_DIR/web"
exec "$UVICORN" app:app --reload --host 127.0.0.1 --port "${PORT:-8000}"
