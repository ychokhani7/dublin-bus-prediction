"""Label stages 2-3: when each vehicle actually passed each stop, and how late it was (table stop_arrivals).

For each trip on a service day:
  1. place every GPS fix along the trip's route shape. A vehicle only moves forward, so we choose fix
     positions together (dynamic programming, like stop_positions.py), which stops a fix on a loop route
     jumping to the other leg. Fixes far from the route (depot, diversion) are dropped.
  2. interpolate the moment the vehicle passed each stop's distance between the two surrounding fixes.
  3. delay = actual passage time - scheduled time (GTFS times are relative to the service day, Dublin time).

Missing rather than invented: the first stop of a trip is skipped (waiting at the terminus is not a delay),
stops between fixes more than MAX_GAP_S apart are not interpolated, and only GPS fixes from 30 min before
the scheduled start to 90 min after the scheduled end are used (NTA sometimes tags a bus with a trip_id hours early).

Usage (service date = the trip's start_date, Dublin local; default: yesterday):
  docker compose run --rm --no-deps collector python build_arrivals.py 2026-10-04
Re-running a date replaces that date's rows.
"""
import os
import sys
from datetime import datetime, timezone

import numpy as np
import psycopg

from stop_positions import candidates, coords

MAX_OFFSET_M = 80      # GPS fixes further than this from the route are ignored
BACK_TOL_M = 30        # GPS jitter: small backward moves are free
MAX_SPEED_MS = 30      # 108 km/h; faster implied moves are penalised
PENALTY = 1000.0
MAX_GAP_S = 300        # don't interpolate a stop passage across a GPS gap longer than 5 minutes
REACHED_TOL_M = 5      # a stop counts as reached within 5 m (the last stop sits at the very end of the line)
BATCH = 400            # trips per database round trip
# NTA sometimes tags a vehicle with a trip_id hours before (or after) the trip really runs, while the bus is doing
# other runs on the same roads. Only GPS fixes inside this window around the scheduled trip are used.
WINDOW_BEFORE_S = 30 * 60
WINDOW_AFTER_S = 90 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS stop_arrivals (
  service_date  date NOT NULL,
  trip_id       text NOT NULL,
  stop_sequence int  NOT NULL,
  stop_id       text NOT NULL,
  version_id    smallint NOT NULL,   -- timetable version used (old versions are pruned, so don't join on it)
  scheduled_at  timestamptz NOT NULL,
  actual_at     timestamptz NOT NULL,
  delay_s       int NOT NULL,        -- positive = late
  gap_s         smallint NOT NULL,   -- seconds between the two GPS fixes around the passage: smaller = more precise
  PRIMARY KEY (service_date, trip_id, stop_sequence)
);
-- Copied from the timetable, so labels stand on their own after old timetable versions are pruned.
ALTER TABLE stop_arrivals ADD COLUMN IF NOT EXISTS route_id text;
ALTER TABLE stop_arrivals ADD COLUMN IF NOT EXISTS direction_id smallint;
ALTER TABLE stop_arrivals ADD COLUMN IF NOT EXISTS block_id text;
ALTER TABLE stop_arrivals ADD COLUMN IF NOT EXISTS timepoint smallint"""

GPS_SQL = """
SELECT trip_id, extract(epoch FROM observed_at), ST_X(p), ST_Y(p) FROM (
  SELECT trip_id, observed_at, ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 2157) AS p
  FROM vehicle_obs WHERE trip_id = ANY(%(ids)s) AND start_date = %(sd)s) q
ORDER BY trip_id, observed_at"""

# GTFS: a stop time is seconds after "noon minus 12 h" of the service day, in local (Dublin) time.
SCHEDULE_SQL = """
SELECT t.trip_id, t.shape_id, st.stop_sequence, st.stop_id, ss.dist_m,
       extract(epoch FROM (%(d)s::date + time '12:00') AT TIME ZONE 'Europe/Dublin' - interval '12 hours'
                          + make_interval(secs => COALESCE(st.arrival_secs, st.departure_secs))),
       t.route_id, t.direction_id, t.block_id, st.timepoint
FROM trips t
JOIN stop_times st  ON st.version_id = t.version_id AND st.trip_id = t.trip_id
JOIN shape_stops ss ON ss.version_id = t.version_id AND ss.shape_id = t.shape_id
                   AND ss.stop_sequence = st.stop_sequence AND ss.stop_id = st.stop_id
WHERE t.version_id = %(v)s AND t.trip_id = ANY(%(ids)s)
ORDER BY t.trip_id, st.stop_sequence"""


def track(line, xy, t):
    """GPS fixes -> (times, distance along the route). Off-route fixes dropped; result never decreases."""
    cands, times = [], []
    for p, ti in zip(xy, t):
        c = candidates(line, p)
        if min(off for _, off in c) <= MAX_OFFSET_M:
            cands.append(c)
            times.append(ti)
    if len(cands) < 2:
        return np.array([]), np.array([])

    cost, back = [np.array([off for _, off in cands[0]])], []
    for i in range(1, len(cands)):
        prev = np.array([a for a, _ in cands[i - 1]])
        dt = max(times[i] - times[i - 1], 1.0)
        row_cost, row_back = [], []
        for along, off in cands[i]:
            move = along - prev
            bad = np.maximum(-BACK_TOL_M - move, 0) + np.maximum(move - (MAX_SPEED_MS * dt + 50), 0)
            total = cost[-1] + np.where(bad > 0, PENALTY + bad, 0.0)
            j = int(np.argmin(total))
            row_cost.append(total[j] + off)
            row_back.append(j)
        cost.append(np.array(row_cost))
        back.append(row_back)
    j = int(np.argmin(cost[-1]))
    chosen = [j]
    for row in reversed(back):
        j = row[j]
        chosen.append(j)
    chosen.reverse()
    along = np.array([cands[i][c][0] for i, c in enumerate(chosen)])
    return np.array(times), np.maximum.accumulate(along)


def passages(times, progress, stops):
    """stops: [(stop_sequence, stop_id, dist_m, scheduled_epoch)] in order. Yields arrival rows (minus ids)."""
    first_seq = stops[0][0]
    for seq, stop_id, dist, sched in stops:
        if seq == first_seq:
            continue
        k = int(np.searchsorted(progress, dist - REACHED_TOL_M, side="left"))
        if k == 0 or k == len(progress):
            continue                              # passed before the first fix, or not reached yet
        gap = times[k] - times[k - 1]
        if gap > MAX_GAP_S:
            continue
        p0, p1 = progress[k - 1], progress[k]
        frac = (dist - p0) / (p1 - p0) if p1 > p0 else 1.0   # tolerance decides "reached", not the timing
        actual = times[k - 1] + min(max(frac, 0.0), 1.0) * gap
        yield seq, stop_id, sched, actual, int(round(actual - sched)), int(round(gap))


def version_for(conn, day):
    row = conn.execute("""SELECT id FROM static_versions WHERE feed_start_date <= %s
                          ORDER BY feed_start_date DESC, id DESC LIMIT 1""", (day,)).fetchone()
    if not row:   # building from a later timetable would replace the day's labels with wrong ones
        sys.exit(f"no timetable version covers {day}; reload its zip first")
    return row[0]


def build(conn, day):
    sd = day.replace("-", "")
    vid = version_for(conn, day)
    conn.execute(SCHEMA)
    conn.execute("DELETE FROM stop_arrivals WHERE service_date = %s", (day,))
    trip_ids = [r[0] for r in conn.execute("SELECT DISTINCT trip_id FROM vehicle_obs WHERE start_date = %s", (sd,))]
    shapes = {sid: coords(g) for sid, g in conn.execute(
        "SELECT shape_id, ST_AsGeoJSON(geom) FROM shapes WHERE version_id = %s", (vid,))}

    stats = dict(trips=len(trip_ids), not_in_timetable=0, too_few_fixes=0, with_arrivals=0, arrivals=0, possible=0)
    for b in range(0, len(trip_ids), BATCH):
        ids = trip_ids[b:b + BATCH]
        gps, sched, shape_of, trip_info, timepoint = {}, {}, {}, {}, {}
        for tid, ts, x, y in conn.execute(GPS_SQL, {"ids": ids, "sd": sd}):
            gps.setdefault(tid, []).append((float(ts), x, y))
        for tid, shape_id, seq, stop_id, dist, epoch, route, direction, block, tp in conn.execute(
                SCHEDULE_SQL, {"d": day, "v": vid, "ids": ids}):
            sched.setdefault(tid, []).append((seq, stop_id, dist, float(epoch)))
            shape_of[tid] = shape_id
            trip_info[tid] = (route, direction, block)
            timepoint[tid, seq] = tp

        rows = []
        for tid in ids:
            if tid not in sched or shape_of[tid] not in shapes:
                stats["not_in_timetable"] += 1
                continue
            fixes = np.array(gps.get(tid, []))
            if len(fixes):
                first, last = sched[tid][0][3], sched[tid][-1][3]
                fixes = fixes[(fixes[:, 0] >= first - WINDOW_BEFORE_S) & (fixes[:, 0] <= last + WINDOW_AFTER_S)]
            if len(fixes) < 3:
                stats["too_few_fixes"] += 1
                continue
            times, progress = track(shapes[shape_of[tid]], fixes[:, 1:], fixes[:, 0])
            stats["possible"] += len(sched[tid]) - 1
            if len(times) < 2:
                stats["too_few_fixes"] += 1
                continue
            found = list(passages(times, progress, sched[tid]))
            if found:
                stats["with_arrivals"] += 1
            rows += [(day, tid, seq, stop_id, vid, s, a, delay, gap, *trip_info[tid], timepoint[tid, seq])
                     for seq, stop_id, s, a, delay, gap in found]

        with conn.cursor().copy("""COPY stop_arrivals (service_date, trip_id, stop_sequence, stop_id, version_id,
                                   scheduled_at, actual_at, delay_s, gap_s, route_id, direction_id, block_id,
                                   timepoint) FROM STDIN""") as cp:
            for day_, tid, seq, stop_id, v, s, a, delay, gap, *rest in rows:
                cp.write_row((day_, tid, seq, stop_id, v, datetime.fromtimestamp(s, timezone.utc),
                              datetime.fromtimestamp(a, timezone.utc), delay, gap, *rest))
        stats["arrivals"] += len(rows)
        print(f"  {min(b + BATCH, len(trip_ids))}/{len(trip_ids)} trips, {stats['arrivals']:,} arrivals so far", flush=True)
    return vid, stats


REPORT_SQL = {
    "delay (s)": """SELECT percentile_cont(ARRAY[0.1, 0.5, 0.9]) WITHIN GROUP (ORDER BY delay_s), min(delay_s), max(delay_s)
                    FROM stop_arrivals WHERE service_date = %(d)s""",
    "share more than 5 min late": """SELECT round(100.0 * avg((delay_s > 300)::int), 1) FROM stop_arrivals WHERE service_date = %(d)s""",
    "GPS gap around passage (s)": """SELECT percentile_cont(ARRAY[0.5, 0.9]) WITHIN GROUP (ORDER BY gap_s)
                                     FROM stop_arrivals WHERE service_date = %(d)s""",
    "vs NTA's last prediction for the same stop: n, median |difference| s, share within 60 s": """
        SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(a.delay_s - o.arrival_delay)),
               round(100.0 * avg((abs(a.delay_s - o.arrival_delay) <= 60)::int), 1)
        FROM stop_arrivals a
        JOIN stop_obs o ON o.start_date = to_char(a.service_date, 'YYYYMMDD') AND o.trip_id = a.trip_id
                       AND o.stop_sequence = a.stop_sequence
        WHERE a.service_date = %(d)s AND o.arrival_delay IS NOT NULL""",
}

EXAMPLE_SQL = """
SELECT a.stop_sequence, s.stop_name,
       to_char(a.scheduled_at AT TIME ZONE 'Europe/Dublin', 'HH24:MI:SS') AS scheduled,
       to_char(a.actual_at    AT TIME ZONE 'Europe/Dublin', 'HH24:MI:SS') AS actual,
       a.delay_s, o.arrival_delay AS nta_last_prediction_s, a.gap_s
FROM stop_arrivals a
JOIN stops s ON s.version_id = a.version_id AND s.stop_id = a.stop_id
LEFT JOIN stop_obs o ON o.start_date = to_char(a.service_date, 'YYYYMMDD') AND o.trip_id = a.trip_id
                    AND o.stop_sequence = a.stop_sequence
WHERE a.service_date = %(d)s AND a.trip_id = (
    SELECT trip_id FROM stop_arrivals WHERE service_date = %(d)s GROUP BY trip_id ORDER BY count(*) DESC LIMIT 1)
ORDER BY a.stop_sequence"""


def report(conn, day, vid, stats):
    print(f"\nservice date {day}, timetable version {vid}")
    print(f"trips with GPS: {stats['trips']:,}; not in timetable: {stats['not_in_timetable']:,}; "
          f"too few usable fixes: {stats['too_few_fixes']:,}; with arrivals: {stats['with_arrivals']:,}")
    pct = 100 * stats["arrivals"] / stats["possible"] if stats["possible"] else 0
    print(f"arrivals: {stats['arrivals']:,} of {stats['possible']:,} scheduled stops on tracked trips ({pct:.0f}%)")
    for label, sql in REPORT_SQL.items():
        print(f"{label}: {conn.execute(sql, {'d': day}).fetchone()}")
    rows = conn.execute(EXAMPLE_SQL, {"d": day}).fetchall()
    print("\nexample trip (seq, stop, scheduled, actual, delay s, NTA's last prediction s, gap s):")
    for r in rows[:25]:
        print("  ", r)
    if len(rows) > 25:
        print(f"   ... {len(rows) - 25} more stops")


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        day = sys.argv[1] if len(sys.argv) > 1 else str(conn.execute(
            "SELECT (now() AT TIME ZONE 'Europe/Dublin')::date - 1").fetchone()[0])
        print(f"building arrivals for {day} ...")
        vid, stats = build(conn, day)
        conn.commit()
        report(conn, day, vid, stats)


if __name__ == "__main__":
    main()
