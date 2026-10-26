# How it works, end to end

This walks through the system in the order data flows through it: from the NTA API, to the raw archive and database, to the timetable, to the nightly label job. For each file it explains **what it does**, **how the code is structured**, and **why it was built that way**. Function and constant names match the code, so you can read this side by side with the source.

---

## 1. The big picture

There are two kinds of input and two kinds of job.

| Input | What it is | How often it changes |
|---|---|---|
| **Realtime feeds** (GTFS-Realtime, protobuf) | `TripUpdates`: NTA's predicted delays and cancellations. `Vehicles`: each bus's GPS position. | Every ~30 seconds |
| **Static timetable** (GTFS, a zip of CSV files) | Routes, trips, scheduled stop times, stop locations, route shapes, calendars | Republished daily, really changes every month or two |

| Job | Runs | Produces |
|---|---|---|
| `collector.py` | All the time, inside Docker | Raw archive files plus `vehicle_obs`, `trip_obs`, `stop_obs` |
| `nightly.sh` | Once a day at 07:00 UTC via cron | New timetable versions (when changed), `shape_stops`, yesterday's `stop_arrivals`, and pruning of old timetable versions |

The one table everything is heading towards is **`stop_arrivals`**: one row per bus per stop, with the scheduled time, the actual time and the delay. That is the training data for the models.

Everything runs from **one Docker image** (`Dockerfile` copies all `*.py` files into it). The collector is the long-running container; the nightly scripts run in short-lived containers from the same image with `docker compose run --rm --no-deps collector python <script>.py`. `--no-deps` stops Compose from starting the local Postgres container, because on Azure the database is the managed server named in `.env`.

---

## 2. Configuration and infrastructure files

### `.env` (never committed) and `.env.example`
Holds secrets and settings, read as environment variables:
- `NTA_API_KEY`: sent as the `x-api-key` header.
- `DATABASE_URL`: the Postgres connection string. On Azure it ends in `/bus?sslmode=require`.
- `HEALTHCHECK_URL`: pinged after every successful poll.
- `NIGHTLY_HEALTHCHECK_URL`: pinged after a fully successful nightly run.
- Optional overrides: `GTFS_AGENCY_IDS` (operators to keep, default `1,03C,3,10000`), `POLL_SECONDS`, feed URLs.

`set -a; . ./.env; set +a` in a shell exports every line, which is why `psql "$DATABASE_URL"` works afterwards.

### `Dockerfile`
Python 3.12 slim, installs `requirements.txt` (`requests`, `psycopg`, `gtfs-realtime-bindings`, `numpy`), copies the scripts, and runs `collector.py` by default.

### `docker-compose.yml`
Two services:
- `db`: local Postgres 17 with PostGIS, built from `db/Dockerfile` (the official `postgres:17` image plus the `postgresql-17-postgis-3` package). Only used on your Mac.
- `collector`: the image above. `DATABASE_URL: ${DATABASE_URL:-postgresql://bus:bus@db:5432/bus}` means "use `.env`'s value if set, otherwise the local container". `./data` on the host is mounted as `/data` in the container, which is why scripts inside Docker see the archive at `/data/...`. `restart: unless-stopped` brings the collector back after a crash or VM reboot.

### `schema.sql`
Creates the PostGIS extension and every table except `shape_stops` and `stop_arrivals` (those are created by their own scripts with `CREATE TABLE IF NOT EXISTS`). The tables are described where they are filled, below.

---

## 3. `collector.py`: polling the realtime feeds

### What it does
Every 60 seconds, for each of the two feeds:
1. download the protobuf,
2. save it gzipped to `data/raw/<feed>/<YYYY-MM-DD>/<HHMMSS>.pb.gz`,
3. parse it and write the in-scope rows to Postgres.

Every 6 hours it also downloads the static timetable zip.

### How the code is structured

**`main()`** is a plain loop. It records `time.monotonic()` at the start of each round, polls both feeds, then sleeps for whatever is left of 60 seconds, so rounds don't drift. Each feed is wrapped in its own `try/except`: if one poll fails (network, 429, database), it is logged and the loop continues. A one-minute gap is better than a dead collector. The healthcheck is pinged only if both feeds succeeded.

**`poll_once(session, db_url, kind, last_feed_ts)`** handles one feed:
- Parses the response into a `FeedMessage`.
- If `feed.header.timestamp` equals the previous poll's, NTA hasn't refreshed the feed yet, so it skips (no duplicate work).
- **Writes the raw file first**, before touching the database. If the database is down, nothing is lost: the archive can be re-parsed later.
- Opens a connection, gets the allowed routes, filters, and writes. `with psycopg.connect(...)` commits when the block finishes cleanly and rolls back on an error, so a poll is all-or-nothing.

**`allowed_routes(cur)`** returns the set of `route_id`s in the `routes` table, which `load_static.py` filled with only our four operators. It is cached and refreshed every 30 minutes (`ROUTES_REFRESH`). This is how "store only Dublin operators" works without hard-coding route lists: whatever the timetable loader kept defines what the collector keeps. Until a timetable is loaded the set is empty, so only the raw archive is written, and a warning is logged.

**`parse(feed)`** turns TripUpdates into two lists of tuples:
- trip rows: service day, trip, route, direction, start time, schedule relationship (`SCHEDULED`, `CANCELED`, `ADDED`), vehicle.
- stop rows: one per `stop_time_update`, with arrival and departure delay. Updates without `stop_sequence` are skipped (they have no stable key).

It is a pure function (feed in, tuples out) so it can be tested without a database. `HasField` is used for optional protobuf fields, because protobuf returns `0` for a missing number, and "0 seconds late" is different from "unknown".

**`parse_vehicles(feed)`** turns Vehicles into rows of GPS fix time, trip, start date, route, direction, vehicle, lat, lon, bearing. It uses the vehicle's own `timestamp` (when the GPS fix was taken), falling back to the feed time. Vehicles without a trip or position are skipped.

**The three SQL statements** encode the storage design:
- `UPSERT_TRIP` into `trip_obs`, key `(start_date, trip_id)`: one row per trip per day, updated in place (`ON CONFLICT DO UPDATE`). Cancellations end up here.
- `UPSERT_STOP` into `stop_obs`, key `(start_date, trip_id, stop_sequence)`: keeps the **latest** NTA prediction per stop and counts updates in `n_updates`. This is NTA's forecast, not ground truth. Because it is overwritten until the bus arrives, the stored value is NTA's last word, made seconds before arrival, so it is nearly the actual time. A fair benchmark needs NTA's prediction at a fixed horizon (say 15 minutes ahead), which only the raw archive in `data/raw/trip_updates/` keeps.
- `INSERT_VEHICLE` into `vehicle_obs`, key `(trip_id, observed_at)` with `ON CONFLICT DO NOTHING`: one row per GPS fix. When the bus hasn't sent a new fix, the next poll repeats the same timestamp and the primary key silently drops the duplicate. That is the deduplication, done by the database.

`lat`/`lon` are `real` (4 bytes, about 1 m precision), and there is no geometry column or spatial index. Points are built only when a job needs them, which is why this table is a fraction of the size of the old Supabase one.

**`archive_static()`** downloads the timetable zip, hashes it (SHA-256, first 12 characters) and saves it as `data/static/<date>_<hash>.zip` only if no file with that hash exists. Deciding whether it is a *meaningful* change is left to `load_static.py`.

### Tested by
`test_collector.py`: builds small fake feeds with protobuf objects and checks the exact tuples `parse` and `parse_vehicles` return, including cancelled trips, missing fields and skipped entities.

---

## 4. `load_static.py` and `static_diff.py`: the timetable

### Why versions
A realtime `trip_id` only means something against the timetable that was live at the time. So each meaningful timetable is stored as its own **version**: every static table has a `version_id` column pointing at `static_versions`, and deleting a version (`ON DELETE CASCADE`) removes all its rows.

### `load(conn, zip_path)`: loading one zip
1. Reads `agency.txt` and keeps the agencies in `AGENCY_IDS`. If none match, it stops and prints the available ones.
2. Deletes any existing version with the same name (so an explicit reload replaces it), and inserts a `static_versions` row with NTA's `feed_version` and date window from `feed_info.txt`.
3. Streams each file in dependency order, filtering as it goes: routes of our agencies, then trips of those routes, then stop times of those trips, then stops used by those stop times, shapes used by those trips, and calendars of those services. Each step collects the IDs the next step needs (`route_ids`, `trip_ids`, `stop_ids`, ...).
4. Writes with Postgres `COPY` (`cur.copy(...)` / `write_row`), which streams rows in bulk. The nationwide `stop_times.txt` (8.6 M rows, 450 MB) is never held in memory.
5. Converts GTFS times with `secs()`: `"25:10:00"` becomes `90600`. GTFS allows hours past 24 for trips after midnight that belong to the previous service day; storing seconds keeps that simple.
6. Converts coordinates to geometry in **EPSG:2157** (Irish Transverse Mercator) via the `TO_ITM` SQL snippet. In this projection units are metres, so lengths and distances need no conversion. Stops and shapes go through temporary staging tables (`stg_stops`, `stg_shapes`) because the geometry is built in SQL; shapes become one `LineString` per shape with `ST_MakeLine(... ORDER BY seq)`.
7. Runs `ANALYZE` on each table. Without it, Postgres plans the next queries as if the new rows didn't exist (this caused a 16-minute stall once).

The whole load is one transaction: it either fully appears or not at all.

### `load_new(conn)`: the nightly decision
NTA republishes the full timetable daily. `load_new`:
1. Lists zips in `data/static/` whose names are in neither `static_versions` nor `static_skipped`, oldest first.
2. For each, compares it with the zip of the newest timetable loaded (latest `feed_start_date`, not highest id: a zip reloaded by hand gets the highest id) using `static_diff`.
3. **Changed enough:** `load()` it as a new version and print what changed. **Unchanged or minor:** insert into `static_skipped` (`same_as` = the version it duplicates, `reason` = `duplicate` or `minor: ...`), commit, then delete the zip.

"Minor" (`static_diff.is_minor`) means no route, stop or shape changed and fewer than 1% of trips are affected. A full version costs about 1.8 GB, which is not worth it for, say, 2 new trips. Because each zip is compared with the latest *loaded* version, small changes accumulate day by day until they pass 1%, and then the next zip (which contains all of them) is loaded. The cost is that those few trips get no labels in the meantime.

The baseline fingerprint is computed once and reused while the latest version stays the same.

Run with an explicit path (`python load_static.py path.zip`) it skips the check and (re)loads that zip.

### `prune(conn)`: keeping storage bounded
`python load_static.py --prune` deletes every version except the `KEEP_VERSIONS` (2) with the latest `feed_start_date`, and records their names in `static_skipped` (`reason` = `pruned`), because their zips stay on disk and `load_new` would otherwise reload them the next night (this happened every night from 10 to 16 October 2026). The cascade removes their routes, trips, stop times, stops, shapes, calendars and stop positions. This is safe because labels no longer depend on old versions (see section 6), and because zips that were loaded are never deleted from `data/static/`: to rebuild an old day, reload its zip with an explicit path and re-run `stop_positions.py` and `build_arrivals.py` for that date. The nightly job prunes only after yesterday's labels are built.

### `static_diff.py`: how "changed" is decided
**`fingerprint(zf)`** reads the in-scope part of a zip and summarises it without keeping rows in memory:
- `h(*fields)` hashes a row's fields into a 64-bit number (BLAKE2b).
- Per trip: a hash of route, direction, shape, headsign, block, plus the **sum** of the hashes of its stop-time rows. A sum is order-independent, so the same rows in a different file order give the same result, and any changed stop or time changes it.
- Per route, stop and shape: a hash of their contents (a shape's points are summed the same way).
- It also keeps each trip's `service_id` and the calendar rows, but **not** as part of the trip's hash.

**`service_days(fp, start, end)`** expands `calendar.txt` (weekday flags between two dates) and applies `calendar_dates.txt` exceptions (1 = extra day, 2 = removed day), giving `{service_id: set of dates}`.

**`differences(old, new)`** compares from the new zip's first day onwards and returns human-readable reasons:
- new trips, trips with changed details or stop times,
- **trips running on different days**: each trip's actual set of dates is compared, so a relabelled `service_id` that runs on the same dates is not a change, while a new bank-holiday exception is,
- trips removed that were still due to run (trips whose service has finished are allowed to disappear),
- new or changed routes, stops and shapes.

`compare()` returns the affected IDs per category; `differences()` turns them into those readable counts. An empty list means "nothing changed for our operators". Run as a script, `python static_diff.py OLD.zip NEW.zip` prints the verdict.

### Tested by
`test_static_diff.py`: a base timetable and NTA-style "republished next day" copies with one change each (changed time, new trip, holiday, removed trip, moved stop, relabelled service, a change to another operator only). `test_load_static.py`: loads a tiny zip twice into a real database and checks filtering, after-midnight times, `timepoint`, `block_id`, `feed_info` and shape lengths in metres.

---

## 5. `stop_positions.py`: where each stop sits on its route

### The problem
NTA's timetable gives stop locations and a route line, but not how far along the route each stop is. Arrival times need that number ("stop 27 is at 8,430 m").

### How the code works
**`candidates(line, point)`** finds every plausible position of a point on a polyline, with numpy:
- For every segment `a→b`, project the point onto it (`t`, clipped to the segment), giving the closest point, its distance from the stop (`dist`) and how far along the line it is (`along`).
- Keep segments within `NEAR_M` (150 m) of the closest one, and merge neighbours less than `MERGE_GAP_M` (30 m) apart. On a loop route a stop typically ends up with two candidates: one on the outbound road and one on the return road.

**`place_stops(line, points)`** chooses one candidate per stop with **dynamic programming** (Viterbi):
- `cost[i][c]`: the best total distance-from-line for stops `0..i` if stop `i` uses candidate `c`.
- Moving to the next stop, each candidate looks at all candidates of the previous stop. If it would be *behind* the previous one, it pays `BACKWARD_PENALTY` (1,000) plus the distance backwards; otherwise nothing extra. It keeps the cheapest predecessor in `back`.
- At the end, take the cheapest final candidate and follow `back` pointers to recover the whole sequence.

Because the whole route is optimised at once, one ambiguous stop can't drag the others onto the wrong leg. (Two earlier attempts, nearest-point and forward-only placement, failed exactly that way on 145 shapes and 219 stops; that's documented in the tests.)

**`process_version(conn, vid)`** loads shapes and stops for one timetable version as GeoJSON (`coords()` turns that into numpy arrays), builds each shape's stop pattern from `trips` + `stop_times` (`SELECT DISTINCT shape, sequence, stop`), places the stops, and `COPY`s the results into `shape_stops` (`dist_m`, plus `offset_m`, the distance from the stop to its chosen point, a quality measure).

**`main()`** processes every version not yet in `shape_stops`, then **`report()`** prints quality numbers (median and max offset, stops over 50 m, stops out of order).

### Tested by
`test_stop_positions.py`: an out-and-back route with a stop on the return road, and a stop sitting between the two roads 1 m closer to the wrong one.

---

## 6. `build_arrivals.py`: actual arrival times (the labels)

### What it does
For one service day (default: yesterday, Dublin time), for every trip with GPS: work out when the vehicle passed each stop and how late it was, and write the rows into `stop_arrivals`.

### How the code works
**`build(conn, day)`**
1. Picks the timetable version for that day (`version_for`: the newest version whose `feed_start_date` is on or before the day). If none is loaded it stops with an error instead of using a later timetable, since step 2 would otherwise replace good labels with wrong ones.
2. Deletes that day's existing rows, so re-running a day replaces it.
3. Lists the trips with GPS that day and loads every shape of the version.
4. Processes trips in batches of `BATCH` (400): one query for their GPS fixes (`GPS_SQL`, projected to EPSG:2157 in SQL) and one for their schedule (`SCHEDULE_SQL`). Batching keeps the number of database round trips small without loading the whole day into memory.

**`SCHEDULE_SQL` and the time conversion.** GTFS defines stop times relative to "noon minus 12 hours" of the service day in local time (this handles clock changes correctly). The SQL builds that from `day + 12:00` in `Europe/Dublin`, subtracts 12 hours and adds the stop's seconds, returning an absolute time. A stop at `25:05:00` on 4 October correctly becomes 01:05 on 5 October. It also joins `shape_stops` to attach each stop's `dist_m`.

**Per trip:**
- **Schedule window.** Only GPS fixes between 30 minutes before the first scheduled stop (`WINDOW_BEFORE_S`) and 90 minutes after the last (`WINDOW_AFTER_S`) are kept. This removes fixes from earlier runs mislabelled with this trip ID.
- **`track(line, xy, t)`** turns GPS fixes into "time → metres along the route". It reuses `candidates()` from `stop_positions.py`, drops fixes more than `MAX_OFFSET_M` (80 m) from the route (depot, diversion), then runs the same Viterbi idea over time: moving backwards more than `BACK_TOL_M` (30 m, GPS jitter) or faster than `MAX_SPEED_MS` (30 m/s) between fixes is penalised. Finally `np.maximum.accumulate` makes progress never decrease.
- **`passages(times, progress, stops)`** finds, for each stop, the first fix at or beyond it (`np.searchsorted`), and linearly interpolates the time the bus crossed the stop's distance between that fix and the previous one. It skips the trip's first stop (terminus dwell), stops passed before the first fix or never reached, and gaps over `MAX_GAP_S` (300 s). Each result carries `gap_s`, the time between the two fixes, so label precision can be filtered later.

5. Rows are written with `COPY`. `delay_s = actual - scheduled`.

**`report()`** prints coverage, delay percentiles, the share more than 5 minutes late, GPS gap percentiles, the comparison with NTA's last prediction from `stop_obs`, and one full example trip stop by stop.

### The `stop_arrivals` table
Key `(service_date, trip_id, stop_sequence)`. Columns: `stop_id`, `scheduled_at`, `actual_at`, `delay_s`, `gap_s`, plus **`route_id`, `direction_id`, `block_id` and `timepoint` copied from the timetable** (fetched by `SCHEDULE_SQL`). Copying them makes each label self-contained, so old timetable versions can be pruned. `version_id` records which version was used but is not meant for joins, since that version may be gone. Hour, weekday and so on are derived when building features. The `SCHEMA` string adds the four copied columns with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so an older table upgrades itself.

### Known limits
- The label is when the bus **passed** the stop, interpolated, so it includes part of the dwell time at the stop. That slightly raises the median delay.
- About 0.15% of rows look like tagging noise (more than 10 minutes early or more than 60 minutes late). They are kept in the table and dropped at training time by a documented rule.

---

## 7. `nightly.sh`: tying the nightly steps together

```
set -eu                     # stop at the first failing command
cd to the project folder; load .env
run python load_static.py       # 1. new timetable? load it or record it as duplicate
run python stop_positions.py    # 2. stop positions for any new version
run python build_arrivals.py    # 3. yesterday's labels
run python load_static.py --prune   # 4. keep only the newest 2 timetable versions
curl NIGHTLY_HEALTHCHECK_URL    # only reached if all three succeeded
```

`run()` is `docker compose run --rm --no-deps -T collector "$@"`: a throwaway container from the collector image (`-T` because cron has no terminal). The order matters: a new timetable must be loaded before its stops are placed, both before arrivals are built, and pruning comes last so it never removes the version yesterday's labels needed.

Cron runs it at **07:00 UTC** (8 am Irish summer time, 7 am winter), after the previous day's after-midnight trips are over: the latest ends about 06:20 Dublin time, so 05:00 UTC would cut them off in winter. Output goes to `nightly.log`. If anything fails, `set -e` stops the script, the healthcheck is not pinged, and healthchecks.io emails you.

---

## 7b. `model/`: predicting P(late)
Run on the Mac from a labels export (`data/labels.csv.gz`, see the README), in `.venv` with `pip install -r model/requirements.txt` (LightGBM on macOS also needs `brew install libomp`).

- **`baseline.py`**: for each test arrival, the share of late arrivals in the training days for the most specific group seen (route + day type, then + direction + hour, then + stop), shrunk towards the coarser group: `(late + 20 * p_parent) / (n + 20)`. `load()` drops likely label noise (more than 10 min early or 60 min late) and adds `daytype`, `hour` (Dublin) and `late` (delay > 300 s); `gbm.py` reuses it.
- **`gbm.py`**: LightGBM, version 1, using only what is known before the bus leaves. `add_features()` derives, per arrival: minute of day and weekday (Dublin time); stop sequence, minutes into the trip, trip length and fraction done; and from the block (one bus's day), the trip's number in the block, minutes since the block's first departure and the scheduled layover since the previous trip ended. Trip and block times come from labelled stops, so a trip "starts" at its second stop (the first never has a label; old timetables are pruned). `add_weather()` joins hourly Open-Meteo rain, temperature and wind for Dublin, cached in `data/weather.csv`. `route_id` and `stop_id` are LightGBM categorical features. Settings (`PARAMS`): learning rate 0.03 and 255 leaves. A smaller learning rate means each tree corrects less, so it needs more trees (357 vs 180) but fits more finely; on the 19-25 Oct test it beat learning rate 0.1 with 127 leaves on 6 of 7 days (Brier skill 15.2% vs 14.6%), for about 2 minutes of training instead of 40 s.
- **Split by date.** The last 7 days (`TEST_DAYS`, one full week, so every weekday counts once) are the test, the 3 before them (`VALID_DAYS`) only choose the number of trees (early stopping), then each model is refit on all non-test days with that many trees. Three validation days instead of two because the two days before a Monday-to-Sunday test week are a weekend: training stopped where weekend traffic stopped improving (73 trees), too early for weekdays. A random split would leak: arrivals on the same trip and day are nearly copies of each other. `gbm.py` scores the baseline on the same 7 test days; `baseline.py` on its own still defaults to 3 (`python model/baseline.py data/labels.csv.gz 7` to match).
- **Outputs.** P(late) (binary log loss) with Brier, log loss, AUC and a calibration table next to the baseline, and three quantile models (p10, p50, p90 of `delay_s`) scored by pinball loss against a constant and by coverage (the share of actual delays below each predicted quantile should be about 10/50/90%).
- **Tested by** `model/test_baseline.py` (shrinkage and fallback to coarser groups) and `model/test_gbm.py` (trip, block and time-zone features on a hand-made two-trip block).

## 8. Other files

- **`migrate_supabase.sql`**: loads a CSV export of the old Supabase `vehicle_positions` table into `vehicle_obs`, dropping consecutive duplicate positions, and reports how many old trips still match the current timetable. Kept for the paused Supabase backup; not run.
- **`db/Dockerfile`**: the local Postgres + PostGIS image.

---

## 9. Data model at a glance

| Table | Filled by | One row is | Key |
|---|---|---|---|
| `vehicle_obs` | collector | one GPS fix | `(trip_id, observed_at)` |
| `trip_obs` | collector | one trip on one service day (incl. cancelled) | `(start_date, trip_id)` |
| `stop_obs` | collector | NTA's latest prediction for one stop of one trip | `(start_date, trip_id, stop_sequence)` |
| `static_versions` | load_static | one loaded timetable | `id` |
| `static_skipped` | load_static | one daily zip not loaded (duplicate, or a minor trip-only change), or a pruned version | `name` |
| `routes`, `trips`, `stop_times`, `stops`, `shapes`, `calendar`, `calendar_dates` | load_static | GTFS rows of one version | `version_id` + GTFS key |
| `shape_stops` | stop_positions | one stop on one route shape, with its distance | `(version_id, shape_id, stop_sequence, stop_id)` |
| `stop_arrivals` | build_arrivals | one vehicle passing one stop: scheduled, actual, delay, plus route, direction, block, timepoint | `(service_date, trip_id, stop_sequence)` |

---

## 10. How to explain it in an interview (60 seconds)

> I collect Ireland's GTFS-Realtime feeds every minute into a raw protobuf archive and a PostGIS database on Azure. The official delay feed is a forecast with only a few stops per trip, so I derive ground truth from GPS instead: I place every stop and every GPS fix along the route shape using a Viterbi-style dynamic program, which handles loop routes where naive nearest-point snapping fails, then interpolate when each bus passed each stop. A nightly job turns each day into about 300,000 labelled arrivals, rejects mis-tagged GPS with a schedule window, and only loads a new timetable when it really changes, because NTA republishes it daily with relabelled IDs. Those labels train the delay model, and NTA's own predictions are the benchmark to beat.
