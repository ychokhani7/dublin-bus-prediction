"""Does a new NTA timetable zip change anything for the operators we store?

NTA republishes the whole timetable every day with a new feed_version and a window moved forward one day,
even when nothing changed. Loading every copy would add ~1.8 GB a day to the database, so load_static.py
uses differences() and only loads a zip that really changes something.

A new zip changes nothing if, for our operators (AGENCY_IDS) and from its first service day onwards:
  - every trip in it exists in the old zip with the same route, shape, block, stop times AND the same
    actual dates of operation (compared as dates, not service_id labels: NTA sometimes relabels service_ids
    without changing any day a bus runs),
  - every trip the old zip ran in that window is still in it,
  - the shapes, stops and routes those trips use are identical.
Fingerprints are order-independent sums of 64-bit row hashes, so memory stays small (no rows are kept).
"""
import hashlib
from datetime import date, datetime, timedelta

from load_static import AGENCY_IDS, rows

MASK = (1 << 64) - 1
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def h(*fields):
    return int.from_bytes(hashlib.blake2b("\x1f".join(f or "" for f in fields).encode(), digest_size=8).digest(), "big")


def d(v):
    return datetime.strptime(v, "%Y%m%d").date()


def fingerprint(zf):
    """Order-independent fingerprint of the in-scope part of an open GTFS zip."""
    files = set(zf.namelist())
    routes = {r["route_id"]: h(r.get("agency_id"), r.get("route_short_name"), r.get("route_long_name"), r.get("route_type"))
              for r in rows(zf, "routes.txt") if r.get("agency_id") in AGENCY_IDS}
    trips, service_of = {}, {}
    for r in rows(zf, "trips.txt"):
        if r["route_id"] in routes:
            trips[r["trip_id"]] = [h(r["route_id"], r.get("direction_id"), r.get("shape_id"),   # not service_id:
                                     r.get("trip_headsign"), r.get("block_id")), 0, 0]           # dates compared instead
            service_of[r["trip_id"]] = r["service_id"]
    stop_ids = set()
    for r in rows(zf, "stop_times.txt"):
        t = trips.get(r["trip_id"])
        if t:
            t[1] = (t[1] + h(r["stop_sequence"], r["stop_id"], r.get("arrival_time"), r.get("departure_time"),
                             r.get("timepoint"))) & MASK
            t[2] += 1
            stop_ids.add(r["stop_id"])
    stops = {r["stop_id"]: h(r.get("stop_code"), r.get("stop_name"), r["stop_lat"], r["stop_lon"])
             for r in rows(zf, "stops.txt") if r["stop_id"] in stop_ids}
    shape_ids = set()
    for r in rows(zf, "trips.txt"):
        if r["trip_id"] in trips and r.get("shape_id"):
            shape_ids.add(r["shape_id"])
    shapes = {}
    if "shapes.txt" in files:
        for r in rows(zf, "shapes.txt"):
            if r["shape_id"] in shape_ids:
                s = shapes.setdefault(r["shape_id"], [0, 0])
                s[0] = (s[0] + h(r["shape_pt_sequence"], r["shape_pt_lat"], r["shape_pt_lon"])) & MASK
                s[1] += 1
    services = set(service_of.values())
    calendar = [r for r in rows(zf, "calendar.txt") if r["service_id"] in services] if "calendar.txt" in files else []
    exceptions = [r for r in rows(zf, "calendar_dates.txt") if r["service_id"] in services] \
        if "calendar_dates.txt" in files else []
    info = next(rows(zf, "feed_info.txt"), {}) if "feed_info.txt" in files else {}
    return dict(routes=routes, trips={k: tuple(v) for k, v in trips.items()}, service_of=service_of,
                stops=stops, shapes={k: tuple(v) for k, v in shapes.items()},
                calendar=calendar, exceptions=exceptions, start=info.get("feed_start_date"))


def service_days(fp, start, end):
    """{service_id: frozenset of dates it runs}, between start and end inclusive."""
    days = {}
    for r in fp["calendar"]:
        day, last = max(d(r["start_date"]), start), min(d(r["end_date"]), end)
        while day <= last:
            if r[WEEKDAYS[day.weekday()]] == "1":
                days.setdefault(r["service_id"], set()).add(day)
            day += timedelta(days=1)
    for r in fp["exceptions"]:
        day = d(r["date"])
        if start <= day <= end:
            s = days.setdefault(r["service_id"], set())
            (s.add if r["exception_type"] == "1" else s.discard)(day)
    return {k: frozenset(v) for k, v in days.items()}


def compare(old, new):
    """What `new` changes compared with `old`, for our operators, from its first service day onwards."""
    start = d(new["start"]) if new["start"] else date.today()
    end = date(9999, 12, 31)
    old_days, new_days = service_days(old, start, end), service_days(new, start, end)
    none = frozenset()
    dates_old = {t: old_days.get(s, none) for t, s in old["service_of"].items()}
    dates_new = {t: new_days.get(s, none) for t, s in new["service_of"].items()}
    shared = new["trips"].keys() & old["trips"].keys()
    out = {
        "new trips": new["trips"].keys() - old["trips"].keys(),
        "trips with changed stops, times or details": {t for t in shared if new["trips"][t] != old["trips"][t]},
        "trips running on different days": {t for t in shared if dates_new[t] != dates_old[t]},
        "trips removed that were still due to run": {t for t, days in dates_old.items() if days and t not in new["trips"]},
    }
    for kind in ("routes", "stops", "shapes"):
        out[f"new or changed {kind}"] = {k for k, v in new[kind].items() if old[kind].get(k) != v}
    return out


def differences(old, new):
    """Human-readable list of what `new` changes compared with `old` (empty = nothing for our operators)."""
    return [f"{len(v)} {label}" for label, v in compare(old, new).items() if v]


def is_minor(changes, n_trips, pct=1.0):
    """A change touching only a few trips (no route, stop or shape changes) is not worth a new ~1.8 GB version.
    shortcut: those few trips go unlabelled until changes add up past pct and the next zip is loaded."""
    if any(changes[f"new or changed {k}"] for k in ("routes", "stops", "shapes")):
        return False
    affected = set().union(*(v for label, v in changes.items() if "trips" in label))
    return len(affected) < pct / 100 * n_trips


if __name__ == "__main__":
    # Compare two zips directly:  python static_diff.py OLD.zip NEW.zip
    import sys
    import zipfile
    with zipfile.ZipFile(sys.argv[1]) as a, zipfile.ZipFile(sys.argv[2]) as b:
        result = differences(fingerprint(a), fingerprint(b))
    print("no changes for our operators" if not result else "; ".join(result))
