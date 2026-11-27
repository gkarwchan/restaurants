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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("AHS_DB", str(HERE.parent / "ahs.db"))
MAPS_KEY = os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "")
MAP_ID = os.environ.get("GOOGLE_MAPS_MAP_ID", "DEMO_MAP_ID")  # DEMO_MAP_ID is fine for development
PLACES_KEY = os.environ.get("GOOGLE_GEOCODING_API_KEY", "")
PLACES_BASE_URL = "https://places.googleapis.com/v1"

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


def place_id_for_facility(con, facility_id):
    if not has_table(con, "google_places"):
        raise HTTPException(404, "No Google Place match is available for this facility")
    row = con.execute(
        "SELECT place_id FROM google_places WHERE facilityId = ?", (facility_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Facility not found")
    if not row["place_id"]:
        raise HTTPException(404, "No Google Place match is available for this facility")
    return row["place_id"]


def places_request(url, field_mask=None):
    if not PLACES_KEY:
        raise HTTPException(503, "Google Places is not configured on this server")
    headers = {"X-Goog-Api-Key": PLACES_KEY}
    if field_mask:
        headers["X-Goog-FieldMask"] = field_mask
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return response.read(), response.headers.get_content_type()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise HTTPException(502, "Google Places request failed") from exc


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/config.js")
def config():
    cfg = {"mapsKey": MAPS_KEY, "mapId": MAP_ID}
    return Response(f"window.APP_CONFIG = {json.dumps(cfg)};", media_type="application/javascript")


# Fields for /api/facilities, sent as compact arrays (25k facilities).
FACILITY_FIELDS = ["id", "name", "type", "lat", "lng", "city", "lastInspection",
                   "latestViolations", "latestCritical", "totalViolations", "totalCritical", "inspections",
                   "hasPlaceInfo"]


@app.get("/api/facilities")
def facilities():
    con = connect()
    try:
        total = con.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
        if not has_table(con, "geocodes"):
            return {"fields": FACILITY_FIELDS, "rows": [], "total": total, "unmapped": total}
        place_info = (
            "EXISTS (SELECT 1 FROM google_places gp "
            "WHERE gp.facilityId = f.id AND gp.place_id IS NOT NULL)"
            if has_table(con, "google_places") else "0"
        )
        rows = con.execute(f"""
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
                   COALESCE(t.n, 0), COALESCE(t.crit, 0), COALESCE(t.inspections, 0),
                   {place_info} AS hasPlaceInfo
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
        place_id = None
        if has_table(con, "google_places"):
            place = con.execute(
                "SELECT place_id FROM google_places WHERE facilityId = ?", (facility_id,)
            ).fetchone()
            place_id = place["place_id"] if place else None
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
        return {**dict(f), "place_id": place_id, "inspections": inspections}
    finally:
        con.close()


@app.get("/api/facilities/{facility_id}/place")
def facility_place(facility_id: str):
    con = connect()
    try:
        place_id = place_id_for_facility(con, facility_id)
    finally:
        con.close()

    encoded_id = urllib.parse.quote(place_id, safe="")
    url = f"{PLACES_BASE_URL}/places/{encoded_id}"
    field_mask = (
        "displayName,formattedAddress,rating,userRatingCount,websiteUri,"
        "nationalPhoneNumber,googleMapsUri,types,photos"
    )
    body, _ = places_request(url, field_mask)
    try:
        place = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "Google Places returned an invalid response") from exc
    if not isinstance(place, dict):
        raise HTTPException(502, "Google Places returned an invalid response")

    photos = place.get("photos") or []
    photo = photos[0] if photos else None
    place.pop("photos", None)
    place["photo_url"] = (
        f"/api/facilities/{urllib.parse.quote(facility_id, safe='')}/place/photo"
        f"?photo_name={urllib.parse.quote(photo['name'], safe='')}"
        if photo and photo.get("name") else None
    )
    place["photo_attributions"] = photo.get("authorAttributions", []) if photo else []
    return place


@app.get("/api/facilities/{facility_id}/place/photo")
def facility_place_photo(facility_id: str, photo_name: str):
    con = connect()
    try:
        place_id = place_id_for_facility(con, facility_id)
    finally:
        con.close()

    photo_parts = photo_name.split("/")
    if (len(photo_parts) != 4 or photo_parts[:3] != ["places", place_id, "photos"]
            or not photo_parts[3]
            or not all(char.isalnum() or char in "_-" for char in photo_parts[3])):
        raise HTTPException(400, "Invalid Google Place photo")
    url = (
        f"{PLACES_BASE_URL}/{urllib.parse.quote(photo_name, safe='/')}/media"
        f"?maxWidthPx=1000&key={urllib.parse.quote(PLACES_KEY, safe='')}"
    )
    body, media_type = places_request(url)
    return Response(
        content=body,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
