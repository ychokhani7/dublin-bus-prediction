"""One-off look at what NTA actually sends, so the schema is based on real data.

Run on the VM:  docker compose run --rm --no-deps collector python check_feeds.py
Makes 3 API calls (TripUpdates once, Vehicles twice 60 s apart), downloads the static
zip into data/static/ if none is there yet, and prints a summary. Writes nothing to the database.
"""
import collections
import csv
import io
import logging
import os
import time
import zipfile

import requests
from google.protobuf import text_format
from google.transit import gtfs_realtime_pb2 as rt

from collector import DATA, FEEDS, STOP_REL, TRIP_REL, archive_static


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "n/a"


def quantiles(xs):
    if not xs:
        return "n/a"
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    return f"min {xs[0]}, p10 {q(.1)}, median {q(.5)}, p90 {q(.9)}, max {xs[-1]}"


def has(msg, path):
    for part in path.split("."):
        try:
            if not msg.HasField(part):
                return False
        except ValueError:  # field not in this version of the bindings
            return None
        msg = getattr(msg, part)
    return True


def presence(msgs, paths):
    lines = []
    for p in paths:
        hits = [has(m, p) for m in msgs]
        lines.append(f"  {p:28} {'not in bindings' if None in hits else pct(sum(hits), len(msgs))}")
    return "\n".join(lines)


def sample(msg, max_lines=30):
    lines = text_format.MessageToString(msg).splitlines()
    return "\n".join(lines[:max_lines] + (["  ..."] if len(lines) > max_lines else []))


def fetch(session, kind):
    r = session.get(FEEDS[kind], timeout=60)
    r.raise_for_status()
    feed = rt.FeedMessage()
    feed.ParseFromString(r.content)
    h = feed.header
    types = collections.Counter(k for e in feed.entity for k in ("trip_update", "vehicle", "alert") if e.HasField(k))
    print(f"\n===== {kind} =====")
    print(f"size {len(r.content) / 1024:.0f} KB, gtfs-rt version {h.gtfs_realtime_version}, "
          f"incrementality {rt.FeedHeader.Incrementality.Name(h.incrementality)}, "
          f"feed age {int(time.time()) - h.timestamp} s, entities {len(feed.entity)}, types {dict(types)}")
    return feed


def check_trip_updates(feed):
    tus = [e.trip_update for e in feed.entity if e.HasField("trip_update")]
    stus = [s for tu in tus for s in tu.stop_time_update]
    print(f"trips {len(tus)}, schedule_relationship {dict(collections.Counter(TRIP_REL.Name(t.trip.schedule_relationship) for t in tus))}")
    print("trip-level fields present:")
    print(presence(tus, ["trip.trip_id", "trip.route_id", "trip.direction_id", "trip.start_date",
                         "trip.start_time", "vehicle.id", "timestamp", "delay"]))
    counts = [len(t.stop_time_update) for t in tus]
    print(f"stop_time_updates per trip: {quantiles(counts)}; trips with exactly 1: {pct(counts.count(1), len(counts))}")
    print(f"first stop_sequence in each trip: {quantiles([t.stop_time_update[0].stop_sequence for t in tus if t.stop_time_update])}")
    print("stop-level fields present:")
    print(presence(stus, ["stop_sequence", "stop_id", "arrival.delay", "arrival.time", "arrival.uncertainty",
                          "departure.delay", "departure.time"]))
    print(f"stop schedule_relationship {dict(collections.Counter(STOP_REL.Name(s.schedule_relationship) for s in stus))}")
    print(f"arrival delay (s): {quantiles([s.arrival.delay for s in stus if s.arrival.HasField('delay')])}")
    example = next((t for t in tus if len(t.stop_time_update) >= 2), tus[0] if tus else None)
    if example:
        print("example trip_update:\n" + sample(example))
    return {t.trip.trip_id for t in tus}, {t.trip.route_id for t in tus if t.trip.route_id}


def check_vehicles(first, second):
    vs = [e.vehicle for e in first.entity if e.HasField("vehicle")]
    print(f"vehicles {len(vs)}, with a trip_id {pct(sum(bool(v.trip.trip_id) for v in vs), len(vs))}")
    print("fields present:")
    print(presence(vs, ["trip.trip_id", "trip.route_id", "trip.direction_id", "trip.start_date", "trip.start_time",
                        "trip.schedule_relationship", "vehicle.id", "vehicle.label", "position.latitude",
                        "position.bearing", "position.speed", "timestamp", "current_stop_sequence", "stop_id",
                        "current_status", "congestion_level", "occupancy_status"]))
    print(f"GPS fix age vs feed time (s): {quantiles([first.header.timestamp - v.timestamp for v in vs if v.timestamp])}")
    before = {v.vehicle.id: (v.timestamp, v.position.latitude, v.position.longitude) for v in vs if v.vehicle.id}
    after = {e.vehicle.vehicle.id: (e.vehicle.timestamp, e.vehicle.position.latitude, e.vehicle.position.longitude)
             for e in second.entity if e.HasField("vehicle") and e.vehicle.vehicle.id}
    both = before.keys() & after.keys()
    print(f"second poll 60 s later: {len(after)} vehicles, {len(both)} in both polls; of those, "
          f"same GPS timestamp {pct(sum(before[k][0] == after[k][0] for k in both), len(both))}, "
          f"moved {pct(sum(before[k][1:] != after[k][1:] for k in both), len(both))}")
    if vs:
        print("example vehicle:\n" + sample(vs[0]))
    return {v.trip.trip_id for v in vs if v.trip.trip_id}, {v.trip.route_id for v in vs if v.trip.route_id}


def rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def check_static(path, rt_trips, rt_routes):
    print(f"\n===== static timetable: {path.name} ({path.stat().st_size / 1e6:.0f} MB zipped) =====")
    z = zipfile.ZipFile(path)
    for info in sorted(z.infolist(), key=lambda i: i.filename):
        with z.open(info) as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            header = next(reader, [])
            n = sum(1 for _ in reader)
        print(f"{info.filename}: {n:,} rows, {info.file_size / 1e6:.0f} MB; columns: {', '.join(header)}")

    names = set(z.namelist())
    agencies = {r["agency_id"]: r["agency_name"] for r in rows(z, "agency.txt")}
    print("agencies:", agencies)
    if "feed_info.txt" in names:
        print("feed_info:", list(rows(z, "feed_info.txt")))
    route_agency = {r["route_id"]: r.get("agency_id") for r in rows(z, "routes.txt")}
    trip_route = {r["trip_id"]: r["route_id"] for r in rows(z, "trips.txt")}

    print(f"realtime trip_ids found in trips.txt: {pct(len(rt_trips & trip_route.keys()), len(rt_trips))} "
          f"({len(rt_trips)} distinct)")
    print(f"realtime route_ids found in routes.txt: {pct(len(rt_routes & route_agency.keys()), len(rt_routes))}")
    print("unmatched realtime trip_id examples:", sorted(rt_trips - trip_route.keys())[:5])
    print("static trip_id examples:", list(trip_route)[:3])
    per_agency = collections.Counter(agencies.get(route_agency.get(trip_route[t]), "?") for t in rt_trips if t in trip_route)
    print("realtime trips per operator:", dict(per_agency))

    filled = total = 0
    for i, r in enumerate(rows(z, "stop_times.txt")):
        if i >= 200_000:
            break
        total += 1
        filled += bool(r.get("shape_dist_traveled"))
    print(f"stop_times.shape_dist_traveled filled (first {total:,} rows): {pct(filled, total)}")
    if "calendar.txt" in names:
        cal = list(rows(z, "calendar.txt"))
        print(f"calendar: {len(cal)} services, {min(r['start_date'] for r in cal)} to {max(r['end_date'] for r in cal)}")
    if "calendar_dates.txt" in names:
        print("calendar_dates exception_type:", dict(collections.Counter(r["exception_type"] for r in rows(z, "calendar_dates.txt"))))


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    session = requests.Session()
    session.headers["x-api-key"] = os.environ["NTA_API_KEY"]

    trips, routes = check_trip_updates(fetch(session, "trip_updates"))
    first = fetch(session, "vehicles")
    print("(waiting 60 s for a second Vehicles poll)")
    time.sleep(60)
    second = fetch(session, "vehicles")
    v_trips, v_routes = check_vehicles(first, second)

    static_dir = DATA / "static"
    if not sorted(static_dir.glob("*.zip")):
        print("\ndownloading static timetable ...")
        archive_static()
    check_static(sorted(static_dir.glob("*.zip"))[-1], trips | v_trips, routes | v_routes)


if __name__ == "__main__":
    main()
