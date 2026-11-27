#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${GOOGLE_MAPS_BROWSER_KEY:-}" && -f "${HOME}/.zshrc" ]]; then
  if ! command -v zsh >/dev/null 2>&1; then
    echo "zsh is required to load GOOGLE_MAPS_BROWSER_KEY from ~/.zshrc." >&2
    exit 1
  fi

  exec zsh -c '
    source "$HOME/.zshrc"
    if [[ -z "${GOOGLE_MAPS_BROWSER_KEY:-}" ]]; then
      print -u2 "GOOGLE_MAPS_BROWSER_KEY was not set by ~/.zshrc."
      exit 1
    fi
    export GOOGLE_MAPS_BROWSER_KEY
    exec bash "$0"
  ' "$ROOT_DIR/deploy-web.sh"
fi

SERVICE="${SERVICE:-ahs-map}"
REGION="${REGION:-northamerica-northeast2}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ "$PROJECT" == "(unset)" ]]; then
  PROJECT=""
fi

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud CLI is required to deploy the web app." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to prepare a consistent database snapshot." >&2
  exit 1
fi

if [[ -z "${GOOGLE_MAPS_BROWSER_KEY:-}" ]]; then
  echo "Set GOOGLE_MAPS_BROWSER_KEY before deploying." >&2
  exit 1
fi

if [[ -z "$PROJECT" ]]; then
  echo "Set PROJECT or configure a default project with 'gcloud config set project'." >&2
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

if ! python3 -c 'import sys; f = open(sys.argv[1], "rb"); sys.exit(f.read(16) != b"SQLite format 3\0")' "$DB_PATH"; then
  echo "Not a SQLite database: $DB_PATH" >&2
  echo "If this is a Git LFS pointer, install Git LFS and run 'git lfs pull'." >&2
  exit 1
fi

WEB_DB="$ROOT_DIR/web/ahs.db"
if [[ -e "$WEB_DB" && "$DB_PATH" -ef "$WEB_DB" ]]; then
  echo "AHS_DB must not point to the web image snapshot at $WEB_DB." >&2
  exit 1
fi

python3 - "$DB_PATH" "$WEB_DB" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.execute("PRAGMA query_only = ON")
    source.backup(target)
PY

ENV_VARS="GOOGLE_MAPS_BROWSER_KEY=${GOOGLE_MAPS_BROWSER_KEY}"
if [[ -n "${GOOGLE_MAPS_MAP_ID:-}" ]]; then
  ENV_VARS+=",GOOGLE_MAPS_MAP_ID=${GOOGLE_MAPS_MAP_ID}"
fi
if [[ -n "${GOOGLE_GEOCODING_API_KEY:-}" ]]; then
  ENV_VARS+=",GOOGLE_GEOCODING_API_KEY=${GOOGLE_GEOCODING_API_KEY}"
fi

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT"

gcloud run deploy "$SERVICE" \
  --source "$ROOT_DIR/web" \
  --region "$REGION" \
  --project "$PROJECT" \
  --allow-unauthenticated \
  --update-env-vars "$ENV_VARS"
