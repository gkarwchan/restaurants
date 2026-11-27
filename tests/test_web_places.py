import json
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from web import app as web_app


class FakeResponse:
    def __init__(self, body, content_type="application/json"):
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


class WebPlacesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "places.db"
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE facilities (
                    id TEXT PRIMARY KEY, name TEXT, facility_group TEXT, area TEXT,
                    community TEXT, address TEXT, address_line1 TEXT, city TEXT,
                    postal_code TEXT, last_inspection_date TEXT, detail_url TEXT,
                    inspections_fetched_at TEXT
                );
                INSERT INTO facilities (id, name) VALUES ('f1', 'Cafe');
                CREATE TABLE google_places (facilityId TEXT PRIMARY KEY, place_id TEXT);
                INSERT INTO google_places VALUES ('f1', 'place-123');
                CREATE TABLE inspections (
                    id TEXT PRIMARY KEY, facility_id TEXT, inspection_number TEXT,
                    inspection_date TEXT, inspection_type TEXT, action_taken TEXT
                );
                CREATE TABLE violations (
                    id TEXT, inspection_id TEXT, category TEXT, question TEXT,
                    critical TEXT, status TEXT, comments TEXT, inspector_comment TEXT
                );
                CREATE TABLE geocodes (
                    address TEXT PRIMARY KEY, lat REAL, lng REAL, status TEXT
                );
                INSERT INTO geocodes VALUES ('1 Main St', 51.0, -114.0, 'OK');
            """)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_details_use_stored_place_id_and_include_photo_metadata(self):
        payload = {
            "displayName": {"text": "Cafe"},
            "photos": [{
                "name": "places/place-123/photos/photo-abc",
                "authorAttributions": [{"displayName": "Photographer"}],
            }],
        }
        with (
            patch.object(web_app, "DB_PATH", str(self.db_path)),
            patch.object(web_app, "PLACES_KEY", "test-key"),
            patch("web.app.urllib.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode())) as request,
        ):
            result = web_app.facility_place("f1")

        self.assertEqual(result["photo_url"], "/api/facilities/f1/place/photo?photo_name=places%2Fplace-123%2Fphotos%2Fphoto-abc")
        self.assertEqual(result["photo_attributions"][0]["displayName"], "Photographer")
        self.assertEqual(request.call_args.args[0].full_url, "https://places.googleapis.com/v1/places/place-123")
        self.assertEqual(request.call_args.args[0].get_header("X-goog-api-key"), "test-key")

    def test_photo_proxy_rejects_names_for_another_place(self):
        with patch.object(web_app, "DB_PATH", str(self.db_path)):
            with self.assertRaises(web_app.HTTPException) as raised:
                web_app.facility_place_photo("f1", "places/other-place/photos/photo-abc")
        self.assertEqual(raised.exception.status_code, 400)

    def test_photo_proxy_returns_google_image(self):
        image = b"fake image data"
        with (
            patch.object(web_app, "DB_PATH", str(self.db_path)),
            patch.object(web_app, "PLACES_KEY", "test-key"),
            patch("web.app.urllib.request.urlopen", return_value=FakeResponse(image, "image/jpeg")) as request,
        ):
            result = web_app.facility_place_photo(
                "f1", "places/place-123/photos/photo-abc"
            )
        self.assertEqual(result.body, image)
        self.assertEqual(result.media_type, "image/jpeg")
        self.assertIn("maxWidthPx=1000", request.call_args.args[0].full_url)

    def test_facility_response_includes_saved_place_id(self):
        with patch.object(web_app, "DB_PATH", str(self.db_path)):
            result = web_app.facility("f1")
        self.assertEqual(result["place_id"], "place-123")

    def test_facility_details_work_without_places_table(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("DROP TABLE google_places")
        with patch.object(web_app, "DB_PATH", str(self.db_path)):
            result = web_app.facility("f1")
        self.assertIsNone(result["place_id"])

    def test_map_data_flags_only_facilities_with_a_place_id(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("""
                INSERT INTO facilities (id, name, address, facility_group, city)
                VALUES ('f2', 'Bakery', '2 Main St', 'Food', 'Calgary')
            """)
            db.execute("INSERT INTO geocodes VALUES ('2 Main St', 51.1, -114.1, 'OK')")
            db.execute("INSERT INTO google_places VALUES ('f2', NULL)")
            db.execute("""
                UPDATE facilities SET address = '1 Main St', facility_group = 'Food', city = 'Calgary'
                WHERE id = 'f1'
            """)
        with patch.object(web_app, "DB_PATH", str(self.db_path)):
            result = web_app.facilities()
        has_place_info = result["fields"].index("hasPlaceInfo")
        flagged = {row[0]: row[has_place_info] for row in result["rows"]}
        self.assertEqual(flagged, {"f1": 1, "f2": 0})


if __name__ == "__main__":
    unittest.main()
