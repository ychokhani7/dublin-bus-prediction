"""Load one static GTFS zip into PostGIS as a versioned snapshot, keeping only the chosen operators.

Usage: python load_static.py [path/to/gtfs.zip]
  no argument (the nightly job): check each new zip in data/static/ and load it only if it changes something
    for our operators. NTA republishes the timetable daily, mostly unchanged; duplicates are recorded in
    static_skipped and deleted (the Mobility Database keeps every NTA version if they are ever needed).
  explicit path: (re)load that zip, replacing the version if it exists
  --prune: keep only the newest KEEP_VERSIONS versions (run after labels are built)
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
        # Refresh planner statistics now: a bulk load leaves them stale, and Postgres then picks very slow
        # plans for the next queries on this version (e.g. stop_positions.py) until autovacuum catches up.
        cur.execute(f"ANALYZE {table}")
        n = cur.execute(f"SELECT count(*) FROM {table} WHERE version_id = %s", (vid,)).fetchone()[0]
        print(f"{table:15} {n:>10,}")
    size = cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    print(f"version {name} loaded as id {vid}; database size now {size}")


SKIPPED_SCHEMA = """
CREATE TABLE IF NOT EXISTS static_skipped (
  name       text PRIMARY KEY,                                         -- zip stem that was checked and not loaded
  same_as    smallint REFERENCES static_versions ON DELETE SET NULL,   -- version it duplicated
  checked_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE static_skipped ADD COLUMN IF NOT EXISTS reason text"""


# Newest timetable, not newest load: an old zip reloaded by hand gets the highest id.
NEWEST_FIRST = "feed_start_date DESC NULLS LAST, id DESC"


def load_new(conn):
    """Check every zip in data/static/ not seen before, oldest first. Load it only if it changes something
    for our operators compared with the newest timetable loaded; otherwise record it and delete the file."""
    from static_diff import compare, fingerprint, is_minor   # imported here: static_diff imports this module
    conn.execute(SKIPPED_SCHEMA)
    conn.commit()
    seen = {n for (n,) in conn.execute("SELECT name FROM static_versions UNION SELECT name FROM static_skipped")}
    pending = [p for p in sorted((DATA / "static").glob("*.zip")) if p.stem not in seen]
    if not pending:
        print("no new timetable to check")
    base_name, base_fp = None, None
    for path in pending:
        latest = conn.execute(f"SELECT id, name FROM static_versions ORDER BY {NEWEST_FIRST} LIMIT 1").fetchone()
        base_path = DATA / "static" / f"{latest[1]}.zip" if latest else None
        if not latest or not base_path.exists():
            print(f"{path.stem}: nothing to compare with, loading")
            load(conn, path)
            conn.commit()
            continue
        if base_name != latest[1]:
            with zipfile.ZipFile(base_path) as z:
                base_fp, base_name = fingerprint(z), latest[1]
        with zipfile.ZipFile(path) as z:
            new_fp = fingerprint(z)
        changes = compare(base_fp, new_fp)
        summary = "; ".join(f"{len(v)} {label}" for label, v in changes.items() if v)
        if summary and not is_minor(changes, len(new_fp["trips"])):
            print(f"{path.stem}: changed vs {latest[1]} ({summary}), loading")
            load(conn, path)
            conn.commit()
        else:
            reason = f"minor: {summary}" if summary else "duplicate"
            conn.execute("INSERT INTO static_skipped (name, same_as, reason) VALUES (%s, %s, %s)",
                         (path.stem, latest[0], reason))
            conn.commit()
            path.unlink()
            print(f"{path.stem}: {reason} vs {latest[1]}, recorded as skipped and deleted")


KEEP_VERSIONS = 2


def prune(conn):
    """Delete all but the newest KEEP_VERSIONS timetable versions (~1.8 GB each). Labels already carry the trip
    details they need, and changed zips stay in data/static/, so an old day can be rebuilt by reloading its zip."""
    old = [v for (v,) in conn.execute(f"SELECT id FROM static_versions ORDER BY {NEWEST_FIRST} OFFSET %s",
                                      (KEEP_VERSIONS,))]
    if old:
        # remember the names, or load_new would see their zips as new and reload them the next night
        conn.execute("""INSERT INTO static_skipped (name, reason) SELECT name, 'pruned' FROM static_versions
                        WHERE id = ANY(%s) ON CONFLICT DO NOTHING""", (old,))
        conn.execute("DELETE FROM static_versions WHERE id = ANY(%s)", (old,))   # cascades to all static tables
        conn.commit()
    print(f"pruned timetable versions {old}" if old else "no timetable versions to prune")


if __name__ == "__main__":
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if sys.argv[1:] == ["--prune"]:
            prune(conn)                        # nightly job, after yesterday's labels are built
        elif len(sys.argv) > 1:
            load(conn, Path(sys.argv[1]))      # explicit path: (re)load it regardless
        else:
            load_new(conn)                     # nightly job: only what really changed
