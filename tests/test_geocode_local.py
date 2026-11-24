import csv
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from geocode import ensure_schema, pending_addresses, seed_usage_baseline
from geocode_local import (
    FULL_MUNICIPALITY_FIELDS,
    SourceIndex,
    load_edmonton_index,
    load_full_municipality_index,
    normalize_address,
    resolve_local_geocodes,
)


def write_dbf(path, rows):
    widths = {
        "inc_muni": 100, "a_full_add": 254, "addnum_pre": 15, "add_number": 11,
        "addnum_suf": 15, "st_premod": 15, "st_predir": 10, "st_pretyp": 50,
        "st_presep": 20, "st_name": 254, "st_postyp": 50, "st_posdir": 10,
        "st_posmod": 25, "a_st_name": 254, "longitude": 31, "latitude": 31,
    }
    header_length = 32 + 32 * len(FULL_MUNICIPALITY_FIELDS) + 1
    record_length = 1 + sum(widths[field] for field in FULL_MUNICIPALITY_FIELDS)
    header = bytearray(32)
    header[0] = 0x03
    header[4:8] = struct.pack("<I", len(rows))
    header[8:10] = struct.pack("<H", header_length)
    header[10:12] = struct.pack("<H", record_length)

    with path.open("wb") as file:
        file.write(header)
        for field in FULL_MUNICIPALITY_FIELDS:
            descriptor = bytearray(32)
            descriptor[:len(field)] = field.encode("ascii")
            descriptor[11] = ord("C")
            descriptor[16] = widths[field]
            file.write(descriptor)
        file.write(b"\r")
        for row in rows:
            record = bytearray(b" " * record_length)
            record[0] = ord(" ")
            offset = 1
            for field in FULL_MUNICIPALITY_FIELDS:
                encoded = row.get(field, "").encode("utf-8")
                record[offset:offset + widths[field]] = encoded.ljust(widths[field], b" ")
                offset += widths[field]
            file.write(record)
        file.write(b"\x1a")
    path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")


class LocalGeocodeTests(unittest.TestCase):
    def test_city_source_precedes_oda_and_ambiguous_city_match_falls_back(self):
        calgary = SourceIndex(
            coordinates={
                normalize_address("10 Main St"): {(51.0, -114.0)},
                normalize_address("20 Main St"): {(51.1, -114.1), (51.2, -114.2)},
            },
            labels={},
            rows_read=0,
            rows_matched=0,
            invalid_coordinates=0,
        )
        calgary.labels = {
            normalize_address("10 Main St"): "Calgary point",
            normalize_address("20 Main St"): "Ambiguous Calgary point",
        }
        oda = SourceIndex(
            coordinates={
                (normalize_address("10 Main St"), "CALGARY"): {(52.0, -115.0)},
                (normalize_address("20 Main St"), "CALGARY"): {(53.0, -116.0)},
                (normalize_address("30 Main St"), "CALGARY"): {(54.0, -117.0)},
            },
            labels={
                (normalize_address("10 Main St"), "CALGARY"): "ODA point 1",
                (normalize_address("20 Main St"), "CALGARY"): "ODA point 2",
                (normalize_address("30 Main St"), "CALGARY"): "ODA point 3",
            },
            rows_read=0,
            rows_matched=0,
            invalid_coordinates=0,
        )
        fullmunicipality = SourceIndex(
            coordinates={
                (normalize_address("20 Main St"), "CALGARY"): {(52.5, -115.5)},
            },
            labels={
                (normalize_address("20 Main St"), "CALGARY"): "Municipal point",
            },
            rows_read=0,
            rows_matched=0,
            invalid_coordinates=0,
        )
        facilities = [
            ("10 Main St, Calgary AB", "10 Main St", "Calgary"),
            ("20 Main St, Calgary AB", "20 Main St", "Calgary"),
            ("30 Main St, Calgary AB", "30 Main St", "Calgary"),
            ("40 Main St, Calgary AB", "40 Main St", "Calgary"),
        ]

        resolved, ambiguous = resolve_local_geocodes(
            facilities,
            {"40 Main St, Calgary AB"},
            {
                "calgary": calgary,
                "edmonton": SourceIndex({}, {}, 0, 0, 0),
                "fullmunicipality": fullmunicipality,
                "oda": oda,
            },
        )

        self.assertEqual(
            [(item.address, item.source, item.lat, item.lng) for item in resolved],
            [
                ("10 Main St, Calgary AB", "calgary", 51.0, -114.0),
                ("20 Main St, Calgary AB", "fullmunicipality", 52.5, -115.5),
                ("30 Main St, Calgary AB", "oda", 54.0, -117.0),
            ],
        )
        self.assertEqual(ambiguous["calgary"], 1)

    def test_full_municipality_index_reads_split_dbfs_and_reconstructs_address(self):
        row = {
            "inc_muni": "twn_stony_plain",
            "a_full_add": "",
            "add_number": "19312",
            "st_pretyp": "Township Road",
            "st_name": "492",
            "longitude": "-114.0100",
            "latitude": "53.5470",
        }
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_dbf(folder / "address.dbf", [row])
            write_dbf(folder / "address_1.dbf", [])

            index = load_full_municipality_index(
                folder,
                {(normalize_address("19312 Township Road 492"), "STONY PLAIN")},
            )

        key = (normalize_address("19312 Township Road 492"), "STONY PLAIN")
        self.assertEqual(index.coordinates[key], {(53.547, -114.01)})
        self.assertEqual(index.rows_read, 1)

    def test_edmonton_index_reads_capitalized_coordinate_columns_and_suite_keys(self):
        headers = [
            "Sub Address", "House Number", "Street Name", "Status",
            "Latitude", "Longitude", "Object Type",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edmonton.csv"
            with path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=headers)
                writer.writeheader()
                writer.writerow({
                    "Sub Address": "", "House Number": "100", "Street Name": "MAIN ST",
                    "Status": "OFFICIAL", "Latitude": "53.5", "Longitude": "-113.5",
                    "Object Type": "PARCEL",
                })
                writer.writerow({
                    "Sub Address": "12", "House Number": "100", "Street Name": "MAIN ST",
                    "Status": "OFFICIAL", "Latitude": "53.6", "Longitude": "-113.6",
                    "Object Type": "SUITE",
                })

            index = load_edmonton_index(
                path,
                {normalize_address("100 Main St"), normalize_address("12 100 Main St")},
            )

        self.assertEqual(index.coordinates[normalize_address("100 Main St")], {(53.5, -113.5)})
        self.assertEqual(index.coordinates[normalize_address("12 100 Main St")], {(53.6, -113.6)})

    def test_legacy_geocodes_remain_google_and_local_entries_do_not_refresh(self):
        db = sqlite3.connect(":memory:")
        db.execute("""
            CREATE TABLE geocodes (
                address TEXT PRIMARY KEY, lat REAL, lng REAL, place_id TEXT,
                location_type TEXT, formatted_address TEXT, status TEXT, geocoded_at TEXT
            )
        """)
        db.execute("""
            INSERT INTO geocodes (address, lat, lng, status, geocoded_at)
            VALUES ('legacy', 51, -114, 'OK', '2000-01-01T00:00:00+00:00')
        """)
        ensure_schema(db)
        self.assertEqual(
            db.execute("SELECT source FROM geocodes WHERE address='legacy'").fetchone()[0],
            "google",
        )

        db.execute("""
            CREATE TABLE facilities (
                address TEXT, postal_code TEXT
            )
        """)
        db.executemany(
            "INSERT INTO facilities (address, postal_code) VALUES (?, '')",
            [("legacy",), ("local",), ("missing",)],
        )
        db.execute("""
            INSERT INTO geocodes
                (address, lat, lng, status, geocoded_at, source)
            VALUES ('local', 53, -113, 'OK', '2000-01-01T00:00:00+00:00', 'edmonton')
        """)
        db.execute("""
            INSERT INTO geocodes
                (address, lat, lng, status, geocoded_at, source)
            VALUES ('google_recent', 53, -113, 'OK', '2026-11-23T00:00:00+00:00', 'google')
        """)
        db.commit()

        self.assertEqual(set(pending_addresses(db, 30)), {"missing", "legacy"})

        seed_usage_baseline(db)
        self.assertEqual(
            db.execute(
                "SELECT address FROM geocode_request_attempts WHERE kind='baseline'"
            ).fetchall(),
            [("google_recent",)],
        )
        db.close()


if __name__ == "__main__":
    unittest.main()
