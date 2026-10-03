"""Dublin bus data collector.

Every POLL_SECONDS, for each feed (TripUpdates and Vehicles):
  1. fetch it from NTA GTFS-Realtime (protobuf)
  2. write the raw bytes, gzipped, to data/raw/<feed>/YYYY-MM-DD/  (source of truth, re-parse any time)
  3. store the parsed rows in Postgres, for the operators loaded into `routes` only
     (the raw archive keeps the whole national feed):
     - trip_updates: latest known delay per (service day, trip, stop), plus cancellations
     - vehicles: every new GPS fix (ground truth for when buses really passed stops)
Every STATIC_EVERY seconds: archive the static GTFS zip if it changed.
"""
import gzip
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import requests
from google.transit import gtfs_realtime_pb2 as rt

FEEDS = {
    "trip_updates": os.getenv("GTFSR_URL", "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"),
    "vehicles": os.getenv("GTFSR_VEHICLES_URL", "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"),
}
STATIC_URL = os.getenv("GTFS_STATIC_URL", "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip")
DATA = Path(os.getenv("DATA_DIR", "data"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
STATIC_EVERY = 6 * 3600
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL")  # optional, e.g. a free healthchecks.io ping URL
ROUTES_REFRESH = 1800  # re-read the allowed route list every 30 min (picks up new timetable versions)

TRIP_REL = rt.TripDescriptor.ScheduleRelationship
STOP_REL = rt.TripUpdate.StopTimeUpdate.ScheduleRelationship

log = logging.getLogger("collector")

UPSERT_TRIP = """
INSERT INTO trip_obs (start_date, trip_id, route_id, direction_id, start_time, schedule_relationship, vehicle_id,
                      first_seen, last_seen)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (start_date, trip_id) DO UPDATE SET
  schedule_relationship = EXCLUDED.schedule_relationship,
  vehicle_id = COALESCE(EXCLUDED.vehicle_id, trip_obs.vehicle_id),
  last_seen = EXCLUDED.last_seen
"""

UPSERT_STOP = """
INSERT INTO stop_obs (start_date, trip_id, stop_sequence, route_id, stop_id, arrival_delay, departure_delay,
                      arrival_time, schedule_relationship, first_seen, last_seen)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (start_date, trip_id, stop_sequence) DO UPDATE SET
  arrival_delay = EXCLUDED.arrival_delay,
  departure_delay = EXCLUDED.departure_delay,
  arrival_time = EXCLUDED.arrival_time,
  schedule_relationship = EXCLUDED.schedule_relationship,
  last_seen = EXCLUDED.last_seen,
  n_updates = stop_obs.n_updates + 1
"""


INSERT_VEHICLE = """
INSERT INTO vehicle_obs (observed_at, trip_id, start_date, route_id, direction_id, vehicle_id, lat, lon, bearing)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""


_routes = {"ids": set(), "at": 0.0}


def allowed_routes(cur):
    """route_ids of the operators loaded by load_static.py. Empty until the timetable is loaded."""
    if not _routes["ids"] or time.time() - _routes["at"] > ROUTES_REFRESH:
        _routes["ids"] = {r[0] for r in cur.execute("SELECT DISTINCT route_id FROM routes")}
        _routes["at"] = time.time()
        if not _routes["ids"]:
            log.warning("routes table is empty: run load_static.py. Until then only the raw archive is written.")
    return _routes["ids"]


def _opt(msg, field):
    return getattr(msg, field) if msg.HasField(field) else None


def _ts(event):
    return datetime.fromtimestamp(event.time, timezone.utc) if event.HasField("time") else None


def parse(feed):
    """FeedMessage -> (trip_rows, stop_rows). Pure, so it is unit-testable."""
    trips, stops = [], []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        t = tu.trip
        sd, tid, route = t.start_date or None, t.trip_id, t.route_id or None
        trips.append((sd, tid, route, _opt(t, "direction_id"), t.start_time or None,
                      TRIP_REL.Name(t.schedule_relationship), tu.vehicle.id or None))
        for s in tu.stop_time_update:
            if not s.HasField("stop_sequence"):
                continue  # ponytail: skipped; raw archive keeps them if NTA ever omits stop_sequence
            stops.append((sd, tid, s.stop_sequence, route, s.stop_id or None,
                          s.arrival.delay if s.arrival.HasField("delay") else None,
                          s.departure.delay if s.departure.HasField("delay") else None,
                          _ts(s.arrival), STOP_REL.Name(s.schedule_relationship)))
    return trips, stops


def parse_vehicles(feed):
    """FeedMessage -> vehicle_obs rows. Uses the GPS fix time, falling back to the feed time."""
    rows = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not (v.trip.trip_id and v.HasField("position")):
            continue
        fix_time = datetime.fromtimestamp(v.timestamp or feed.header.timestamp, timezone.utc)
        rows.append((fix_time, v.trip.trip_id, v.trip.start_date or None, v.trip.route_id or None,
                     _opt(v.trip, "direction_id"), v.vehicle.id or None,
                     v.position.latitude, v.position.longitude, _opt(v.position, "bearing")))
    return rows


def poll_once(session, db_url, kind, last_feed_ts):
    r = session.get(FEEDS[kind], timeout=30)
    r.raise_for_status()
    feed = rt.FeedMessage()
    feed.ParseFromString(r.content)
    feed_ts = feed.header.timestamp
    if feed_ts == last_feed_ts:
        log.info("%s not refreshed since last poll, skipping", kind)
        return feed_ts

    observed = datetime.fromtimestamp(feed_ts, timezone.utc)
    # Raw first: if the DB write below fails, nothing is lost.
    path = DATA / "raw" / kind / f"{observed:%Y-%m-%d}" / f"{observed:%H%M%S}.pb.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(r.content))

    with psycopg.connect(db_url) as conn, conn.cursor() as cur:  # commits on clean exit
        keep = allowed_routes(cur)
        if kind == "vehicles":
            rows = parse_vehicles(feed)
            kept = [r for r in rows if r[3] in keep]
            cur.executemany(INSERT_VEHICLE, kept)
            counts = f"vehicles={len(kept)}/{len(rows)}"
        else:
            trips, stops = parse(feed)
            trips = [(*t, observed, observed) for t in trips if t[2] in keep]
            stops = [(*s, observed, observed) for s in stops if s[3] in keep]
            cur.executemany(UPSERT_TRIP, trips)
            cur.executemany(UPSERT_STOP, stops)
            counts = f"trips={len(trips)}/{len(feed.entity)} stop_updates={len(stops)}"
    log.info("%s %s %s raw=%.0fKB", kind, observed.isoformat(), counts, len(r.content) / 1024)
    return feed_ts


def archive_static():
    r = requests.get(STATIC_URL, timeout=300)
    r.raise_for_status()
    digest = hashlib.sha256(r.content).hexdigest()[:12]
    folder = DATA / "static"
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.glob(f"*_{digest}.zip")):
        log.info("static GTFS unchanged (%s)", digest)
        return
    (folder / f"{datetime.now(timezone.utc):%Y%m%d}_{digest}.zip").write_bytes(r.content)
    log.info("new static GTFS version archived: %s (%.1f MB)", digest, len(r.content) / 1e6)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.environ["DATABASE_URL"]
    session = requests.Session()
    session.headers["x-api-key"] = os.environ["NTA_API_KEY"]
    last_feed_ts, last_static = dict.fromkeys(FEEDS, 0), 0.0

    while True:
        started = time.monotonic()
        if time.time() - last_static > STATIC_EVERY:
            last_static = time.time()
            try:
                archive_static()
            except Exception:
                log.exception("static fetch failed, will retry in 6h")
        ok = True
        for kind in FEEDS:  # 2 requests/min = 2,880/day; check your quota on the NTA portal
            try:
                last_feed_ts[kind] = poll_once(session, db_url, kind, last_feed_ts[kind])
            except Exception:
                ok = False
                log.exception("%s poll failed", kind)  # a one-minute gap beats a dead collector
        if ok and HEALTHCHECK_URL:
            try:
                requests.get(HEALTHCHECK_URL, timeout=10)  # silence for N minutes = you get an email
            except Exception:
                log.warning("healthcheck ping failed")
        time.sleep(max(0, POLL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
