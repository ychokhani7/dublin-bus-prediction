"""Load one static GTFS zip into PostGIS as a versioned snapshot, keeping only the chosen operators.

Usage: python load_static.py [path/to/gtfs.zip]     (default: newest zip in data/static/)
Re-running for the same zip replaces that version, so it is safe to repeat.
Streams every file through COPY, so memory stays small even for the nationwide stop_times.txt.
"""
import csv
import io
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import psycopg

# agency_id values from agency.txt: 1 = Dublin Bus, 03C and 3 = Go-Ahead Ireland, 10000 = LUAS
AGENCY_IDS = {a.strip() for a in os.getenv("GTFS_AGENCY_IDS", "1,03C,3,10000").split(",")}
DATA = Path(os.getenv("DATA_DIR", "data"))
TO_ITM = "ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 2157)"


def rows(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def secs(hms):
    """'25:10:00' -> 90600. GTFS times can pass 24:00 for trips that run after midnight."""
    if not hms:
        return None
    h, m, s = map(int, hms.split(":"))
    return h * 3600 + m * 60 + s


def blank(v):
    return v if v not in ("", None) else None


def gtfs_date(v):
    return datetime.strptime(v, "%Y%m%d").date() if v else None


def load(conn, zip_path):
    zf = zipfile.ZipFile(zip_path)
    files = set(zf.namelist())
    cur = conn.cursor()

    agencies = {r["agency_id"]: r["agency_name"] for r in rows(zf, "agency.txt")}
    keep = AGENCY_IDS & agencies.keys()
    if not keep:
        sys.exit(f"No agency_id matched {sorted(AGENCY_IDS)}. Available: {agencies}")
    print("operators:", sorted(f"{a} = {agencies[a]}" for a in keep))

    info = next(rows(zf, "feed_info.txt"), {}) if "feed_info.txt" in files else {}
    name = Path(zip_path).stem
    cur.execute("DELETE FROM static_versions WHERE name = %s", (name,))  # cascades to every static table
    vid = cur.execute(
        "INSERT INTO static_versions (name, feed_version, feed_start_date, feed_end_date) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (name, blank(info.get("feed_version")), gtfs_date(info.get("feed_start_date")),
         gtfs_date(info.get("feed_end_date")))).fetchone()[0]

    route_ids = set()
    with cur.copy("COPY routes FROM STDIN") as cp:
        for r in rows(zf, "routes.txt"):
            if r.get("agency_id") in keep:
                route_ids.add(r["route_id"])
                cp.write_row((vid, r["route_id"], r["agency_id"], blank(r.get("route_short_name")),
                              blank(r.get("route_long_name")), blank(r.get("route_type"))))

    trip_ids, shape_ids, service_ids = set(), set(), set()
    with cur.copy("COPY trips FROM STDIN") as cp:
        for r in rows(zf, "trips.txt"):
            if r["route_id"] in route_ids:
                trip_ids.add(r["trip_id"])
                shape_ids.add(r.get("shape_id"))
                service_ids.add(r["service_id"])
                cp.write_row((vid, r["trip_id"], r["route_id"], r["service_id"], blank(r.get("direction_id")),
                              blank(r.get("shape_id")), blank(r.get("trip_headsign")), blank(r.get("block_id"))))

    stop_ids = set()
    with cur.copy("COPY stop_times FROM STDIN") as cp:
        for r in rows(zf, "stop_times.txt"):
            if r["trip_id"] in trip_ids:
                stop_ids.add(r["stop_id"])
                cp.write_row((vid, r["trip_id"], r["stop_sequence"], r["stop_id"], secs(r.get("arrival_time")),
                              secs(r.get("departure_time")), blank(r.get("timepoint"))))

    cur.execute("DROP TABLE IF EXISTS stg_stops, stg_shapes")
    cur.execute("CREATE TEMP TABLE stg_stops (stop_id text, stop_code text, stop_name text, lat float8, lon float8) ON COMMIT DROP")
    with cur.copy("COPY stg_stops FROM STDIN") as cp:
        for r in rows(zf, "stops.txt"):
            if r["stop_id"] in stop_ids:
                cp.write_row((r["stop_id"], blank(r.get("stop_code")), r.get("stop_name"), r["stop_lat"], r["stop_lon"]))
    cur.execute(f"INSERT INTO stops SELECT %s, stop_id, stop_code, stop_name, {TO_ITM} FROM stg_stops", (vid,))

    if "shapes.txt" in files:
        cur.execute("CREATE TEMP TABLE stg_shapes (shape_id text, seq int, lat float8, lon float8) ON COMMIT DROP")
        with cur.copy("COPY stg_shapes FROM STDIN") as cp:
            for r in rows(zf, "shapes.txt"):
                if r["shape_id"] in shape_ids:
                    cp.write_row((r["shape_id"], r["shape_pt_sequence"], r["shape_pt_lat"], r["shape_pt_lon"]))
        cur.execute(f"""INSERT INTO shapes
                        SELECT %s, shape_id, ST_MakeLine({TO_ITM} ORDER BY seq) FROM stg_shapes GROUP BY shape_id""", (vid,))

    if "calendar.txt" in files:
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        with cur.copy("COPY calendar FROM STDIN") as cp:
            for r in rows(zf, "calendar.txt"):
                if r["service_id"] in service_ids:
                    cp.write_row((vid, r["service_id"], *(r[d] for d in days), r["start_date"], r["end_date"]))

    if "calendar_dates.txt" in files:
        with cur.copy("COPY calendar_dates FROM STDIN") as cp:
            for r in rows(zf, "calendar_dates.txt"):
                if r["service_id"] in service_ids:
                    cp.write_row((vid, r["service_id"], r["date"], r["exception_type"]))

    for table in ["routes", "trips", "stop_times", "stops", "shapes", "calendar", "calendar_dates"]:
        n = cur.execute(f"SELECT count(*) FROM {table} WHERE version_id = %s", (vid,)).fetchone()[0]
        print(f"{table:15} {n:>10,}")
    size = cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    print(f"version {name} loaded as id {vid}; database size now {size}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else max((DATA / "static").glob("*.zip"), default=None)
    if not path:
        sys.exit("No zip given and none in data/static/ yet (the collector downloads one on start).")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:  # one transaction: all or nothing
        load(conn, path)
