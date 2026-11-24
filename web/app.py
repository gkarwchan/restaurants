"""
Map web app for the AHS inspection data.

Run:
  cd web
  export GOOGLE_MAPS_BROWSER_KEY=...     # key with the Maps JavaScript API enabled
  export AHS_DB=../ahs.db                # optional, this is the default
  uvicorn app:app --reload --port 8000
"""
import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("AHS_DB", str(HERE.parent / "ahs.db"))
MAPS_KEY = os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "")
MAP_ID = os.environ.get("GOOGLE_MAPS_MAP_ID", "DEMO_MAP_ID")  # DEMO_MAP_ID is fine for development

app = FastAPI(title="AHS Inspections Map")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def connect():
    if not os.path.exists(DB_PATH):
        raise HTTPException(500, f"Database not found: {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/config.js")
def config():
    cfg = {"mapsKey": MAPS_KEY, "mapId": MAP_ID}
    return Response(f"window.APP_CONFIG = {json.dumps(cfg)};", media_type="application/javascript")


# Fields for /api/facilities, sent as compact arrays (25k facilities).
FACILITY_FIELDS = ["id", "name", "type", "lat", "lng", "city", "lastInspection",
                   "latestViolations", "latestCritical", "totalViolations", "totalCritical", "inspections"]


@app.get("/api/facilities")
def facilities():
    con = connect()
    try:
        total = con.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
        if not has_table(con, "geocodes"):
            return {"fields": FACILITY_FIELDS, "rows": [], "total": total, "unmapped": total}
        rows = con.execute("""
            WITH latest AS (
                SELECT facility_id, id
                FROM (
                    SELECT facility_id, id,
                           ROW_NUMBER() OVER (
                               PARTITION BY facility_id
                               ORDER BY inspection_date DESC, id DESC
                           ) AS rn
                    FROM inspections
                )
                WHERE rn = 1
            ),
            vcount AS (
                SELECT inspection_id, COUNT(*) AS n, SUM(critical = 'Yes') AS crit
                FROM violations GROUP BY inspection_id
            ),
            totals AS (
                SELECT i.facility_id, COUNT(DISTINCT i.id) AS inspections,
                       COALESCE(SUM(vc.n), 0) AS n, COALESCE(SUM(vc.crit), 0) AS crit
                FROM inspections i LEFT JOIN vcount vc ON vc.inspection_id = i.id
                GROUP BY i.facility_id
            )
            SELECT f.id, f.name, f.facility_group AS type, g.lat, g.lng, f.city, f.last_inspection_date,
                   COALESCE(lv.n, 0), COALESCE(lv.crit, 0),
                   COALESCE(t.n, 0), COALESCE(t.crit, 0), COALESCE(t.inspections, 0)
            FROM facilities f
            JOIN geocodes g ON g.address = f.address
                           AND g.status = 'OK'
                           AND g.lat IS NOT NULL
                           AND g.lng IS NOT NULL
            LEFT JOIN latest l ON l.facility_id = f.id
            LEFT JOIN vcount lv ON lv.inspection_id = l.id
            LEFT JOIN totals t ON t.facility_id = f.id
        """).fetchall()
        return {"fields": FACILITY_FIELDS, "rows": [list(r) for r in rows],
                "total": total, "unmapped": total - len(rows)}
    finally:
        con.close()


@app.get("/api/facilities/{facility_id}")
def facility(facility_id: str):
    con = connect()
    try:
        f = con.execute("""
            SELECT id, name, facility_group, area, community, address, address_line1, city,
                   postal_code, last_inspection_date, detail_url, inspections_fetched_at
            FROM facilities WHERE id = ?""", (facility_id,)).fetchone()
        if not f:
            raise HTTPException(404, "Facility not found")
        inspections = [dict(r) for r in con.execute("""
            SELECT id, inspection_number, inspection_date, inspection_type, action_taken
            FROM inspections WHERE facility_id = ? ORDER BY inspection_date DESC""", (facility_id,))]
        by_id = {i["id"]: i for i in inspections}
        for i in inspections:
            i["violations"] = []
        if inspections:
            marks = ",".join("?" * len(inspections))
            for v in con.execute(f"""
                SELECT id, inspection_id, category, question, critical, status, comments, inspector_comment
                FROM violations WHERE inspection_id IN ({marks})
                ORDER BY critical = 'Yes' DESC, category""", list(by_id)):
                by_id[v["inspection_id"]]["violations"].append(dict(v))
        return {**dict(f), "inspections": inspections}
    finally:
        con.close()
