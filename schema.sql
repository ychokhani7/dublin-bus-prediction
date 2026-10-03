CREATE EXTENSION IF NOT EXISTS postgis;

-- ===================== Realtime (filled by collector.py) =====================
-- Only the operators loaded into `routes` are stored here; the raw archive on disk keeps the full national feed.

-- One row per trip per service day. Cancellations live here.
CREATE TABLE trip_obs (
  start_date            text NOT NULL,   -- service day YYYYMMDD (Dublin local), exactly as the feed sends it
  trip_id               text NOT NULL,
  route_id              text,
  direction_id          smallint,
  start_time            text,            -- GTFS time, can exceed 24:00:00 for after-midnight trips
  schedule_relationship text,            -- SCHEDULED / CANCELED / ADDED
  vehicle_id            text,
  first_seen            timestamptz NOT NULL,
  last_seen             timestamptz NOT NULL,
  PRIMARY KEY (start_date, trip_id)
);

-- NTA's predictions: one row per stop NTA mentions, holding the LATEST prediction seen for it.
-- NTA sends only a few stops per trip (delays carry forward to later stops), so this is not one row per stop.
CREATE TABLE stop_obs (
  start_date            text NOT NULL,
  trip_id               text NOT NULL,
  stop_sequence         int  NOT NULL,
  route_id              text,
  stop_id               text,
  arrival_delay         int,             -- seconds, positive = late
  departure_delay       int,
  arrival_time          timestamptz,
  schedule_relationship text,            -- SCHEDULED / SKIPPED
  first_seen            timestamptz NOT NULL,
  last_seen             timestamptz NOT NULL,
  n_updates             int NOT NULL DEFAULT 1,
  PRIMARY KEY (start_date, trip_id, stop_sequence)
);

-- Ground truth: where each vehicle actually was. One row per GPS fix (stale repeats are dropped by the PK).
CREATE TABLE vehicle_obs (
  observed_at  timestamptz NOT NULL,   -- the feed's vehicle.timestamp (GPS fix time)
  trip_id      text NOT NULL,
  start_date   text,
  route_id     text,
  direction_id smallint,
  vehicle_id   text,
  lat          real NOT NULL,          -- real (4 bytes) is ~1 m precision at Dublin's latitude
  lon          real NOT NULL,
  bearing      real,                   -- degrees; NTA fills it for ~40% of vehicles
  PRIMARY KEY (trip_id, observed_at)
);

-- ===================== Static timetable (filled by load_static.py) =====================
-- One snapshot per timetable version: realtime trip_ids only make sense against the version live at the time.
-- Geometry is EPSG:2157 (Irish Transverse Mercator), so distances are in metres.
CREATE TABLE static_versions (
  id              smallserial PRIMARY KEY,
  name            text UNIQUE NOT NULL,  -- zip file stem, e.g. 20261003_76249ab123ce
  feed_version    text,                  -- from feed_info.txt
  feed_start_date date,
  feed_end_date   date,
  loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE routes (
  version_id smallint REFERENCES static_versions ON DELETE CASCADE,
  route_id text, agency_id text, route_short_name text, route_long_name text,
  route_type int,                        -- 0 = tram (LUAS), 3 = bus
  PRIMARY KEY (version_id, route_id)
);

CREATE TABLE trips (
  version_id smallint REFERENCES static_versions ON DELETE CASCADE,
  trip_id text, route_id text, service_id text, direction_id smallint, shape_id text, trip_headsign text,
  block_id text,                         -- consecutive trips run by the same vehicle: lateness carries over
  PRIMARY KEY (version_id, trip_id)
);

CREATE TABLE stop_times (
  version_id smallint REFERENCES static_versions ON DELETE CASCADE,
  trip_id text, stop_sequence int, stop_id text,
  arrival_secs int, departure_secs int,  -- seconds since service-day midnight; 25:10:00 -> 90600
  timepoint smallint,                    -- 1 = exact time (buses may wait here if early), 0 = approximate
  PRIMARY KEY (version_id, trip_id, stop_sequence)
);

CREATE TABLE stops (
  version_id smallint REFERENCES static_versions ON DELETE CASCADE,
  stop_id text, stop_code text, stop_name text, geom geometry(Point, 2157),
  PRIMARY KEY (version_id, stop_id)
);

CREATE TABLE shapes (
  version_id smallint REFERENCES static_versions ON DELETE CASCADE,
  shape_id text, geom geometry(LineString, 2157),
  PRIMARY KEY (version_id, shape_id)
);

CREATE TABLE calendar (
  version_id smallint REFERENCES static_versions ON DELETE CASCADE,
  service_id text,
  monday bool, tuesday bool, wednesday bool, thursday bool, friday bool, saturday bool, sunday bool,
  start_date date, end_date date,
  PRIMARY KEY (version_id, service_id)
);

CREATE TABLE calendar_dates (
  version_id smallint REFERENCES static_versions ON DELETE CASCADE,
  service_id text, date date, exception_type smallint,   -- 1 = service added, 2 = removed
  PRIMARY KEY (version_id, service_id, date)
);
