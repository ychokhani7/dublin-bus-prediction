"""Label stage 1: how far along its route shape each stop is, in metres (table shape_stops).

NTA's stop_times.txt has no shape_dist_traveled, so we compute it. Placing each stop at its nearest point on
the line fails on loop routes (a stop can snap to the return leg), and placing stops one by one, forward only,
lets a single misplaced stop drag every later stop with it. So for each shape we:
  1. find every plausible position for each stop (one per stretch of line that passes close to it),
  2. pick, with dynamic programming, the combination that keeps stops in order along the line and
     minimises their total distance from it (Viterbi-style, as in map matching).

Run after each new timetable version is loaded (versions already in shape_stops are skipped):
  docker compose run --rm --no-deps collector python stop_positions.py
"""
import json
import os

import numpy as np
import psycopg

NEAR_M = 150         # a stretch of line within (closest distance + 150 m) of a stop is a candidate position
MERGE_GAP_M = 30     # candidate positions closer than this along the line are the same stretch
BACKWARD_PENALTY = 1000.0  # cost of placing a stop before the previous one (only used if nothing else fits)


def candidates(line, point):
    """All plausible (along_m, offset_m) positions of `point` on polyline `line` (N x 2, metres)."""
    a, b = line[:-1], line[1:]
    d = b - a
    seg_len = np.hypot(d[:, 0], d[:, 1])
    start = np.concatenate([[0.0], np.cumsum(seg_len)[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.clip(((point - a) * d).sum(axis=1) / seg_len**2, 0, 1)
    t = np.nan_to_num(t)                       # zero-length segments
    proj = a + t[:, None] * d
    dist = np.hypot(*(point - proj).T)
    along = start + t * seg_len

    keep = np.flatnonzero(dist <= dist.min() + NEAR_M)
    keep = keep[np.argsort(along[keep])]
    out = []                                   # one candidate per stretch: the closest point in that stretch
    for i in keep:
        if out and along[i] - out[-1][0] < MERGE_GAP_M:
            if dist[i] < out[-1][1]:
                out[-1] = (along[i], dist[i])
        else:
            out.append((along[i], dist[i]))
    return out


def place_stops(line, points):
    """Choose one candidate per stop, in order along the line, minimising total offset. Returns [(along, offset)]."""
    cands = [candidates(line, p) for p in points]
    cost = [np.array([off for _, off in cands[0]])]
    back = []
    for i in range(1, len(points)):
        prev_along = np.array([al for al, _ in cands[i - 1]])
        row_cost, row_back = [], []
        for along, off in cands[i]:
            step = np.where(prev_along <= along + 5, 0.0, BACKWARD_PENALTY + (prev_along - along))
            total = cost[-1] + step
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
    return [cands[i][c] for i, c in enumerate(chosen)]


def coords(geojson):
    return np.array(json.loads(geojson)["coordinates"], dtype=float)


def process_version(conn, vid):
    lines = {sid: coords(g) for sid, g in conn.execute(
        "SELECT shape_id, ST_AsGeoJSON(geom) FROM shapes WHERE version_id = %s", (vid,))}
    stop_xy = {sid: coords(g) for sid, g in conn.execute(
        "SELECT stop_id, ST_AsGeoJSON(geom) FROM stops WHERE version_id = %s", (vid,))}
    patterns = {}
    for shape_id, seq, stop_id in conn.execute("""
            SELECT DISTINCT t.shape_id, st.stop_sequence, st.stop_id
            FROM trips t JOIN stop_times st ON st.version_id = t.version_id AND st.trip_id = t.trip_id
            WHERE t.version_id = %s AND t.shape_id IS NOT NULL
            ORDER BY 1, 2, 3""", (vid,)):
        patterns.setdefault(shape_id, []).append((seq, stop_id))

    rows = []
    for shape_id, stops in patterns.items():
        if shape_id not in lines:
            continue
        placed = place_stops(lines[shape_id], [stop_xy[s] for _, s in stops])
        rows += [(vid, shape_id, seq, stop_id, float(al), float(off))
                 for (seq, stop_id), (al, off) in zip(stops, placed)]

    with conn.cursor() as cur:
        cur.execute("DELETE FROM shape_stops WHERE version_id = %s", (vid,))
        with cur.copy("COPY shape_stops (version_id, shape_id, stop_sequence, stop_id, dist_m, offset_m) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
    return rows


def report(rows):
    offsets = np.array([r[5] for r in rows])
    print(f"stop positions: {len(rows):,} across {len({r[1] for r in rows}):,} shapes")
    print(f"distance to assigned point (m): median {np.median(offsets):.1f}, p95 {np.percentile(offsets, 95):.1f}, "
          f"max {offsets.max():.1f}, over 50 m: {(offsets > 50).sum()}")
    backwards, prev = 0, {}
    for _, shape_id, seq, _, along, _ in sorted(rows, key=lambda r: (r[1], r[2])):
        if shape_id in prev and along < prev[shape_id] - 5:
            backwards += 1
        prev[shape_id] = along
    print(f"stops placed before the previous stop: {backwards}")
    worst = sorted(rows, key=lambda r: -r[5])[:5]
    print("worst 5 (shape, stop_sequence, stop_id, offset m):",
          [(r[1], r[2], r[3], round(r[5])) for r in worst])


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS shape_stops (
            version_id smallint REFERENCES static_versions ON DELETE CASCADE,
            shape_id text, stop_sequence int, stop_id text,
            dist_m real,    -- distance of the stop along the shape, metres from the start
            offset_m real,  -- distance from the stop to its assigned point on the line
            PRIMARY KEY (version_id, shape_id, stop_sequence, stop_id))""")
        todo = [v for (v,) in conn.execute(
            "SELECT id FROM static_versions WHERE id NOT IN (SELECT DISTINCT version_id FROM shape_stops) ORDER BY id")]
        if not todo:
            print("all timetable versions already processed")
        for vid in todo:
            rows = process_version(conn, vid)
            print(f"version {vid}:")
            report(rows)


if __name__ == "__main__":
    main()
