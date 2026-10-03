-- Loads the old Supabase vehicle_positions export into vehicle_obs.
-- Run from the folder holding vehicle_positions.csv.gz (\copy reads the file on your machine):
--   psql "postgresql://bus:bus@localhost:5432/bus" -f migrate_supabase.sql
CREATE TEMP TABLE vp_raw (recorded_at timestamp, trip_id text, route_id text, lat double precision, lon double precision);
\copy vp_raw FROM PROGRAM 'gunzip -c vehicle_positions.csv.gz' WITH CSV HEADER

-- recorded_at has no timezone. Render and Supabase run on UTC, so we read it as UTC
-- (confirm with the hour-of-day check first). Consecutive identical fixes per trip are dropped:
-- they are stale GPS or a parked bus, and only the first one matters for arrival time.
INSERT INTO vehicle_obs (observed_at, trip_id, route_id, lat, lon)
SELECT recorded_at AT TIME ZONE 'UTC', trip_id, route_id, lat, lon
FROM (
  SELECT *, lag(lat) OVER w AS prev_lat, lag(lon) OVER w AS prev_lon
  FROM vp_raw
  WINDOW w AS (PARTITION BY trip_id ORDER BY recorded_at)
) x
WHERE prev_lat IS DISTINCT FROM lat OR prev_lon IS DISTINCT FROM lon
ON CONFLICT DO NOTHING;

SELECT (SELECT count(*) FROM vp_raw) AS exported_rows,
       (SELECT count(*) FROM vehicle_obs WHERE start_date IS NULL) AS kept_rows,
       pg_size_pretty(pg_total_relation_size('vehicle_obs')) AS vehicle_obs_size;

-- Is the old data usable? Share of each day's trips that exist in the current timetable.
-- ~95%+ on every day: the timetable hasn't changed and all of it can be labelled.
-- A sudden drop on earlier days: a timetable change happened, and those days need the older static version.
SELECT v.observed_at::date AS day,
       count(DISTINCT v.trip_id) AS trips,
       round(100.0 * count(DISTINCT v.trip_id) FILTER (WHERE t.trip_id IS NOT NULL)
                   / count(DISTINCT v.trip_id), 1) AS pct_in_timetable
FROM vehicle_obs v
LEFT JOIN trips t ON t.trip_id = v.trip_id AND t.version_id = (SELECT max(id) FROM static_versions)
WHERE v.start_date IS NULL
GROUP BY 1 ORDER BY 1;
