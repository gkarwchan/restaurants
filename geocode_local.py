#!/usr/bin/env python3
"""Import local municipal and ODA address points without calling Google."""
import argparse
import codecs
import csv
import math
import re
import sqlite3
import struct
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from geocode import ensure_schema, now


CALGARY_FILE = Path("geocoding_data/calgary/Parcel_Address_and_lat_long_20261123.csv")
EDMONTON_FILE = Path("geocoding_data/edmonton/Parcel_Addresses_20261123.csv")
FULL_MUNICIPALITY_DIR = Path("geocoding_data/fullmunicipality")
ODA_FILE = Path("geocoding_data/oda_data/ODA_AB_v1.csv")

MUNICIPALITY_PREFIXES = {
    "city", "county", "ham", "ir", "md", "mun", "rm", "sv", "town", "twn",
    "village", "vlg",
}
STREET_TYPE_ALIASES = {
    "AV": "AVE", "AVENUE": "AVE",
    "BLVD": "BOULEVARD",
    "CIR": "CIRCLE",
    "CL": "CLOSE",
    "CR": "CRESCENT", "CRES": "CRESCENT",
    "CT": "COURT",
    "DR": "DRIVE",
    "GT": "GATE",
    "HTS": "HEIGHTS",
    "LN": "LANE",
    "PL": "PLACE",
    "PK": "PARK",
    "RD": "ROAD",
    "ST": "STREET",
    "TER": "TERRACE", "TERR": "TERRACE",
    "TR": "TRAIL", "TRL": "TRAIL",
    "WY": "WAY",
}
DIRECTION_ALIASES = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW",
    "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
SOURCES = ("calgary", "edmonton", "fullmunicipality", "oda")


@dataclass
class SourceIndex:
    coordinates: dict
    labels: dict
    rows_read: int
    rows_matched: int
    invalid_coordinates: int


@dataclass(frozen=True)
class LocalGeocode:
    address: str
    lat: float
    lng: float
    formatted_address: str
    source: str


def normalize_address(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").upper()
    tokens = re.findall(r"[A-Z0-9]+", value)
    normalized = [STREET_TYPE_ALIASES.get(token, token) for token in tokens]
    return " ".join(DIRECTION_ALIASES.get(token, token) for token in normalized)


def normalize_municipality(value):
    value = (value or "").strip().lower().replace("_", " ")
    if value.startswith("summer village "):
        value = value[len("summer village "):]
    else:
        prefix, separator, remainder = value.partition(" ")
        if separator and prefix in MUNICIPALITY_PREFIXES:
            value = remainder
    return normalize_address(value)


def validate_csv(path, required_headers):
    if not path.is_file():
        raise FileNotFoundError(f"Address data file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        missing = set(required_headers) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{path} is missing required columns: {', '.join(sorted(missing))}"
            )


def load_source_index(path, wanted_keys, required_headers, key_for_row,
                      label_for_row, row_filter=None,
                      coordinate_fields=("latitude", "longitude")):
    validate_csv(path, required_headers)
    coordinates = defaultdict(set)
    labels = {}
    rows_read = rows_matched = invalid_coordinates = 0

    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        for row in reader:
            rows_read += 1
            if row_filter and not row_filter(row):
                continue
            key = key_for_row(row)
            if not key or key not in wanted_keys:
                continue
            try:
                lat = float(row[coordinate_fields[0]])
                lng = float(row[coordinate_fields[1]])
            except (TypeError, ValueError):
                invalid_coordinates += 1
                continue
            if not math.isfinite(lat) or not math.isfinite(lng) or not (
                -90 <= lat <= 90 and -180 <= lng <= 180
            ):
                invalid_coordinates += 1
                continue
            coordinates[key].add((lat, lng))
            labels.setdefault(key, label_for_row(row))
            rows_matched += 1

    return SourceIndex(
        dict(coordinates), labels, rows_read, rows_matched, invalid_coordinates
    )


def load_calgary_index(path, wanted_keys):
    return load_source_index(
        path,
        wanted_keys,
        ("ADDRESS", "ADDRESS_TYPE", "latitude", "longitude"),
        lambda row: normalize_address(row["ADDRESS"]),
        lambda row: row["ADDRESS"].strip(),
        lambda row: row["ADDRESS_TYPE"].strip().upper() == "PARCEL",
    )


def load_edmonton_index(path, wanted_keys):
    def key_for_row(row):
        house_and_street = f"{row['House Number']} {row['Street Name']}"
        object_type = row["Object Type"].strip().upper()
        if object_type == "PARCEL":
            return normalize_address(house_and_street)
        if object_type == "SUITE" and row["Sub Address"].strip():
            return normalize_address(f"{row['Sub Address']} {house_and_street}")
        return None

    def label_for_row(row):
        return " ".join(
            part.strip()
            for part in (row["Sub Address"], row["House Number"], row["Street Name"])
            if part.strip()
        )

    return load_source_index(
        path,
        wanted_keys,
        (
            "Sub Address", "House Number", "Street Name", "Status",
            "Latitude", "Longitude", "Object Type",
        ),
        key_for_row,
        label_for_row,
        lambda row: row["Status"].strip().upper() == "OFFICIAL",
        ("Latitude", "Longitude"),
    )


def load_oda_index(path, wanted_keys):
    return load_source_index(
        path,
        wanted_keys,
        ("full_addr", "city_pcs", "pruid", "latitude", "longitude"),
        lambda row: (
            normalize_address(row["full_addr"]),
            normalize_address(row["city_pcs"]),
        ),
        lambda row: ", ".join(
            part.strip() for part in (row["full_addr"], row["city_pcs"], "AB") if part.strip()
        ),
        lambda row: row["pruid"].strip() == "48",
    )


def iter_dbf_records(path, required_fields):
    with path.open("rb") as file:
        header = file.read(32)
        if len(header) != 32:
            raise ValueError(f"{path} has an incomplete DBF header")
        record_count = struct.unpack("<I", header[4:8])[0]
        header_length = struct.unpack("<H", header[8:10])[0]
        record_length = struct.unpack("<H", header[10:12])[0]
        if header_length < 33 or record_length < 1:
            raise ValueError(f"{path} has invalid DBF dimensions")

        fields = {}
        position = 32
        offset = 1
        while position < header_length:
            file.seek(position)
            descriptor = file.read(32)
            if len(descriptor) != 32 or descriptor[0] == 0x0D:
                break
            name = descriptor[:11].split(b"\0", 1)[0].decode("ascii").lower()
            length = descriptor[16]
            fields[name] = (offset, length)
            offset += length
            position += 32

        missing = set(required_fields) - set(fields)
        if missing:
            raise ValueError(
                f"{path} is missing DBF fields: {', '.join(sorted(missing))}"
            )
        if offset != record_length:
            raise ValueError(f"{path} has inconsistent DBF record length")

        code_page_file = path.with_suffix(".cpg")
        encoding = (
            code_page_file.read_text(encoding="ascii").strip()
            if code_page_file.is_file()
            else "utf-8"
        )
        codecs.lookup(encoding)

        file.seek(header_length)
        for record_number in range(record_count):
            record = file.read(record_length)
            if len(record) != record_length:
                raise ValueError(
                    f"{path} ended before DBF record {record_number + 1}"
                )
            if record[0:1] == b"*":
                continue
            values = {}
            for name in required_fields:
                field_offset, field_length = fields[name]
                values[name] = record[
                    field_offset:field_offset + field_length
                ].decode(encoding).strip()
            yield values


FULL_MUNICIPALITY_FIELDS = (
    "inc_muni", "a_full_add", "addnum_pre", "add_number", "addnum_suf",
    "st_premod", "st_predir", "st_pretyp", "st_presep", "st_name",
    "st_postyp", "st_posdir", "st_posmod", "a_st_name",
    "longitude", "latitude",
)


def address_from_municipality_record(row):
    if row["a_full_add"]:
        return row["a_full_add"]

    number = " ".join(
        part for part in (row["addnum_pre"], row["add_number"], row["addnum_suf"])
        if part
    )
    street = " ".join(
        row[field] for field in (
            "st_premod", "st_predir", "st_pretyp", "st_presep", "st_name",
            "st_postyp", "st_posdir", "st_posmod",
        )
        if row[field]
    ) or row["a_st_name"]
    return " ".join(part for part in (number, street) if part)


def load_full_municipality_index(directory, wanted_keys):
    paths = (directory / "address.dbf", directory / "address_1.dbf")
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Municipal address DBF not found: {path}")

    coordinates = defaultdict(set)
    labels = {}
    rows_read = rows_matched = invalid_coordinates = 0
    for path in paths:
        for row in iter_dbf_records(path, FULL_MUNICIPALITY_FIELDS):
            rows_read += 1
            address = address_from_municipality_record(row)
            key = (
                normalize_address(address),
                normalize_municipality(row["inc_muni"]),
            )
            if not key[0] or not key[1] or key not in wanted_keys:
                continue
            try:
                lng = float(row["longitude"])
                lat = float(row["latitude"])
            except ValueError:
                invalid_coordinates += 1
                continue
            if not math.isfinite(lat) or not math.isfinite(lng) or not (
                48 <= lat <= 61 and -121 <= lng <= -109
            ):
                invalid_coordinates += 1
                continue
            coordinates[key].add((lat, lng))
            labels.setdefault(key, address)
            rows_matched += 1

    return SourceIndex(
        dict(coordinates), labels, rows_read, rows_matched, invalid_coordinates
    )


def resolve_local_geocodes(facilities, existing_addresses, indexes):
    candidates = {
        source: defaultdict(set)
        for source in SOURCES
    }
    labels = {source: {} for source in SOURCES}

    for address, address_line1, city in facilities:
        line_key = normalize_address(address_line1)
        city_key = normalize_address(city)
        source_keys = {
            "calgary": line_key if city_key == "CALGARY" else None,
            "edmonton": line_key if city_key == "EDMONTON" else None,
            "fullmunicipality": (line_key, normalize_municipality(city)),
            "oda": (line_key, city_key),
        }
        for source, key in source_keys.items():
            if key is None:
                continue
            index = indexes[source]
            points = index.coordinates.get(key)
            if points:
                candidates[source][address].update(points)
                labels[source].setdefault(address, index.labels[key])

    resolved = []
    ambiguous = {source: 0 for source in SOURCES}
    for address in dict.fromkeys(row[0] for row in facilities):
        if address in existing_addresses:
            continue
        for source in SOURCES:
            points = candidates[source].get(address, set())
            if len(points) > 1:
                ambiguous[source] += 1
                continue
            if len(points) == 1:
                lat, lng = next(iter(points))
                resolved.append(
                    LocalGeocode(address, lat, lng, labels[source][address], source)
                )
                break

    return resolved, ambiguous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="ahs.db", help="SQLite database (default: ahs.db)")
    parser.add_argument("--calgary-file", type=Path, default=CALGARY_FILE)
    parser.add_argument("--edmonton-file", type=Path, default=EDMONTON_FILE)
    parser.add_argument("--full-municipality-dir", type=Path, default=FULL_MUNICIPALITY_DIR)
    parser.add_argument("--oda-file", type=Path, default=ODA_FILE)
    parser.add_argument("--dry-run", action="store_true", help="report matches without importing")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        parser.error(f"database file not found: {db_path}")

    db = sqlite3.connect(args.db)
    try:
        ensure_schema(db)
        facilities = db.execute("""
            SELECT DISTINCT address, address_line1, city
            FROM facilities
            WHERE address IS NOT NULL AND trim(address) != ''
              AND address_line1 IS NOT NULL AND trim(address_line1) != ''
        """).fetchall()
        existing_addresses = {
            row[0] for row in db.execute("SELECT address FROM geocodes")
        }

        wanted_calgary = {
            normalize_address(line)
            for _, line, city in facilities
            if normalize_address(city) == "CALGARY"
        }
        wanted_edmonton = {
            normalize_address(line)
            for _, line, city in facilities
            if normalize_address(city) == "EDMONTON"
        }
        wanted_oda = {
            (normalize_address(line), normalize_address(city))
            for _, line, city in facilities
            if normalize_address(line) and normalize_address(city)
        }
        wanted_fullmunicipality = {
            (normalize_address(line), normalize_municipality(city))
            for _, line, city in facilities
            if normalize_address(line) and normalize_municipality(city)
        }

        indexes = {
            "calgary": load_calgary_index(args.calgary_file, wanted_calgary),
            "edmonton": load_edmonton_index(args.edmonton_file, wanted_edmonton),
            "fullmunicipality": load_full_municipality_index(
                args.full_municipality_dir, wanted_fullmunicipality
            ),
            "oda": load_oda_index(args.oda_file, wanted_oda),
        }

        for source in SOURCES:
            index = indexes[source]
            if index.invalid_coordinates:
                print(
                    f"Warning: {source} skipped {index.invalid_coordinates} rows "
                    "with invalid coordinates."
                )

        resolved, ambiguous = resolve_local_geocodes(
            facilities, existing_addresses, indexes
        )
        by_source = {
            source: sum(result.source == source for result in resolved)
            for source in SOURCES
        }
        address_count = len({address for address, _, _ in facilities})
        existing_count = len({
            address for address, _, _ in facilities if address in existing_addresses
        })
        print(
            f"{address_count:,} distinct AHS address keys; "
            f"{existing_count:,} already have geocode rows."
        )
        for source in SOURCES:
            index = indexes[source]
            print(
                f"{source}: read {index.rows_read:,} rows, matched "
                f"{index.rows_matched:,} source rows; selected "
                f"{by_source[source]:,}; ambiguous address keys "
                f"{ambiguous[source]:,}."
            )
        print(f"Local coordinates available: {len(resolved):,}")

        if args.dry_run:
            return

        imported_at = now()
        with db:
            for result in resolved:
                db.execute("""
                    INSERT OR IGNORE INTO geocodes
                        (address, lat, lng, place_id, location_type, formatted_address,
                         status, geocoded_at, source)
                    VALUES (?, ?, ?, NULL, NULL, ?, 'OK', ?, ?)
                """, (
                    result.address, result.lat, result.lng, result.formatted_address,
                    imported_at, result.source,
                ))
        print(f"Imported {len(resolved):,} local coordinates; existing rows were not replaced.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
