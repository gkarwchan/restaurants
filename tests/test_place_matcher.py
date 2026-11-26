import json
import sqlite3
import unittest
from unittest.mock import patch

import place_matcher


class PlaceMatcherTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("""
            CREATE TABLE facilities (
                id TEXT PRIMARY KEY,
                name TEXT,
                address_line1 TEXT,
                city TEXT,
                province TEXT,
                place_id TEXT,
                match_status TEXT,
                candidate_ids TEXT
            )
        """)
        self.db.executemany(
            "INSERT INTO facilities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("f1", "Cafe", "1 Main St", "Calgary", "AB",
                 "old-place", "MATCHED", None),
                ("f2", "Bakery", "2 Main St", "Calgary", "AB",
                 None, None, None),
                ("f3", "Deli", "3 Main St", "Calgary", "AB",
                 None, None, None),
                ("f4", "Market", "4 Main St", "Calgary", "AB",
                 None, None, None),
            ],
        )

    def tearDown(self):
        self.db.close()

    def test_migrates_results_to_google_places_and_drops_facility_columns(self):
        place_matcher.ensure_schema(self.db)
        rows = self.db.execute(
            "SELECT facilityId, place_id, match_status, candidate_ids "
            "FROM google_places ORDER BY facilityId"
        ).fetchall()
        self.assertEqual(rows, [("f1", "old-place", "MATCHED", None)])
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(facilities)")}
        self.assertTrue({"place_id", "match_status", "candidate_ids"}.isdisjoint(columns))
        self.assertEqual(place_matcher.read_pending(self.db, 10), [
            ("f2", "Bakery", "2 Main St", "Calgary", "AB"),
            ("f3", "Deli", "3 Main St", "Calgary", "AB"),
            ("f4", "Market", "4 Main St", "Calgary", "AB"),
        ])

    def test_saves_single_multiple_and_not_found_results(self):
        place_matcher.ensure_schema(self.db)
        self.assertEqual(place_matcher.save_record(self.db, "f2", ["p1"]), "MATCHED")
        self.assertEqual(place_matcher.save_record(self.db, "f3", []), "NOT_FOUND")
        self.assertEqual(
            place_matcher.save_record(
                self.db, "f4", ["p2", "p3"], multiple_place_id="null"
            ),
            "MULTIPLE_MATCHES",
        )
        row = self.db.execute(
            "SELECT place_id, match_status, candidate_ids "
            "FROM google_places WHERE facilityId='f4'"
        ).fetchone()
        self.assertEqual(row, (None, "MULTIPLE_MATCHES", '["p2", "p3"]'))
        self.assertEqual(place_matcher.pending_count(self.db), 0)

    @patch("place_matcher.urllib.request.urlopen")
    def test_search_uses_ids_only_field_mask_and_five_results(self, urlopen):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"places":[{"id":"p1"}]}'
        urlopen.return_value = response
        client = place_matcher.PlacesClient("test-key", 90, 0)

        self.assertEqual(client.search("Cafe, Calgary, Canada"), ["p1"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, place_matcher.URL)
        self.assertEqual(request.get_header("X-goog-fieldmask"), "places.id")
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        self.assertEqual(
            json.loads(request.data),
            {"textQuery": "Cafe, Calgary, Canada", "pageSize": 5},
        )


if __name__ == "__main__":
    unittest.main()
