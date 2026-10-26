# Dublin Bus Delay Predictor

Predicts the chance that a Dublin bus or tram gets you somewhere **on time**, and picks the route most likely to arrive before your deadline rather than just the fastest one on paper. It is built on a live pipeline that has collected the National Transport Authority (NTA) realtime feeds every minute since 3 October 2026, and turns raw GPS into actual stop arrival times every night.

> **Status:** collection and nightly arrival labels are live (Dublin Bus, Go-Ahead Ireland, LUAS). The baseline and a first LightGBM model are done (see [What the data looks like](#what-the-data-looks-like)); live features, routing and the agent are next. See [Roadmap](#roadmap).
>
> For a file-by-file explanation of how the code works, read **[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)**.

## Architecture

```
NTA GTFS-Realtime API ──every 60 s──> collector.py (Docker, Azure VM, runs 24/7)
  TripUpdates + Vehicles                │
                                        ├─> data/raw/<feed>/<day>/*.pb.gz   full national feed, as received (source of truth)
                                        └─> PostgreSQL + PostGIS (Azure)    Dublin operators only
                                              ├─ vehicle_obs   GPS fixes (ground truth)
                                              ├─ trip_obs      trips, incl. cancellations
                                              └─ stop_obs      NTA's own delay predictions

NTA static GTFS zip ──every 6 h──> data/static/*.zip (kept only when the file changes)

nightly.sh (cron, 07:00 UTC)
  1. load_static.py     new zip really changes the timetable? load it as a new version : record as duplicate, delete
  2. stop_positions.py  where each stop sits along each route shape (metres)          -> shape_stops
  3. build_arrivals.py  when each vehicle actually passed each stop, and its delay    -> stop_arrivals (training labels)
  4. load_static.py --prune   keep only the newest 2 timetable versions
```

## Design decisions

**GPS positions are the ground truth, not NTA's predictions.** TripUpdates contain *predicted* delays, and NTA sends only a few stops per trip (median 3; 31% of trips carry a single stop update, with the delay carried forward). Vehicle positions record where each bus actually was, so actual arrival times are derived from GPS. A naive "GPS ping within 50 m of a stop" approach was rejected: at 60 s polling a bus covers 300 to 500 m between pings, so it misses most stops and over-samples the ones where the bus was stuck, biasing labels towards delay.

**Linear referencing with dynamic programming.** Stops and GPS fixes are both converted to "metres along the route shape". Placing each point at its nearest spot on the line fails on loop routes (9% of Dublin shapes), and placing them one by one lets one mistake drag every later point. Both stages instead choose all positions together with a Viterbi-style search that keeps them in order and close to the line, the same idea map-matching software uses. Arrival time at a stop is interpolated between the two GPS fixes either side of it.

**Missing rather than invented.** First stops (terminus waiting) are skipped, stop passages are not interpolated across GPS gaps over 5 minutes, and only GPS fixes from 30 minutes before to 90 minutes after a trip's schedule are used (NTA sometimes tags a bus with a trip ID hours before the trip runs).

**Raw first.** Every response is written to disk, gzipped, before any parsing. Database tables are a rebuildable view of the archive.

**Versioned timetables, loaded only when they change.** NTA republishes the whole timetable every day with a new `feed_version` and a moving date window, and sometimes relabels `service_id`s without changing any trip. Loading each copy would add about 1.8 GB a day. `load_static.py` fingerprints the in-scope part of each new zip (trips compared by stops, times and **actual dates of operation**, not ID labels) and loads it only when something real changed. Changes touching fewer than 1% of trips (and no routes, stops or shapes) are skipped until they add up. After each night's labels are built, only the newest two versions are kept: each label row carries the trip details the model needs (route, direction, block, timepoint), and zips of real changes stay on disk, so an old day can still be rebuilt by reloading its zip. Timetable storage is bounded at two versions (about 1.8 GB each) however long collection runs; pruned names are remembered so their zips are not reloaded.

**Store only the operators we model.** The feed is national. The database keeps Dublin Bus, Go-Ahead Ireland and LUAS to stay inside 32 GB; the raw archive keeps everything.

**Infrastructure on free tiers.** Azure for Students: a B2ats v2 VM (collector, raw archive, nightly job) and a managed PostgreSQL Flexible Server B1ms with PostGIS, both in India South Central (the student subscription restricts regions). The only real cost is the VM's static public IP, about $3.60 a month from the student credit. Alternatives considered: Supabase free (500 MB cap, filled in about a week), Render free (ephemeral disk, needs keep-alive pings), Oracle Always Free (fallback when the credit ends).

## What the data looks like

| Live feeds, 3 October 2026 | |
|---|---|
| Realtime trips per snapshot (national) | ~2,700, of which ~1,500 in scope |
| Vehicles per snapshot (national) | ~1,050, of which ~675 in scope |
| Cancelled trips in a snapshot | ~2.4% |
| Realtime `trip_id`s found in the static timetable | 99.8% |
| Median GPS fix age | 25 s (p90 82 s) |
| Timetable loaded (in-scope operators) | 182 routes, 87,421 trips, 4.26 M stop times, 1,559 shapes |

| Labels, Sunday 4 October 2026 | |
|---|---|
| Trips with GPS / with arrivals | 6,614 / 6,554 |
| Arrivals built | 308,744 (93% of scheduled stops on tracked trips) |
| Stop placement on route shapes | median 6 m from line, max 46 m, 0 out of order |
| GPS gap around each stop passage | median 69 s, p90 122 s |
| Delay | p10 -65 s, median +122 s, p90 +552 s; 24.8% more than 5 min late |
| Agreement with NTA's last prediction for the same stop | median difference 41 s, 59% within 60 s |
| Likely label noise (>10 min early or >60 min late) | ~0.15%, to be dropped at training time |

| Storage (first weekend) | |
|---|---|
| Raw archive on VM disk | ~170 MB/day, ~11 months on the free disk |
| Database growth (excluding timetable) | ~70 MB/day |

| Baseline, `model/baseline.py` (train 4-18 Oct, test 19-25 Oct, target: more than 5 min late) | Brier | Skill | Log loss | AUC |
|---|---|---|---|---|
| Overall late rate (23.6% train, 24.1% test) | 0.183 | 0% | 0.553 | 0.50 |
| + route, day type | 0.179 | 2.2% | 0.541 | 0.60 |
| + direction, hour | 0.172 | 6.2% | 0.524 | 0.67 |
| + stop | 0.164 | 10.5% | 0.504 | 0.72 |
| LightGBM, `model/gbm.py` (train 4-15 Oct, early stopping on 16-18 Oct, refit on 4-18 Oct) | **0.155** | **15.2%** | **0.475** | **0.75** |

Brier is the mean squared error of the predicted probability (lower is better), skill is the Brier improvement over always predicting the overall rate, and AUC is the chance a late arrival gets a higher score than an on-time one (0.5 = guessing). History alone explains about a tenth of the uncertainty. Its calibration is off at both ends: stop-hours it rates almost never late (2.5% predicted) were late 6.7% of the time, and the riskiest ones (78% predicted) only 64%.

LightGBM adds timetable position, the bus's day (trip number in its block, scheduled layover after the previous trip), minute of day and hourly weather (Open-Meteo, cached in `data/weather.csv` and refreshed when the labels run past it), all known before the bus leaves. On a full test week it beats the baseline clearly (skill 10.5% to 15.2%, AUC 0.72 to 0.75) and is well calibrated: within about 1 point up to 30% predicted and at most 3 points high above it (44.7% predicted, 42.0% actual in the 0.4-0.5 bin). It predicts 25.0% late against 24.1% actual. What it cannot explain is day-to-day variation: the late share in the test week ran from 15% (Monday 19 October) to 31% (Thursday and Friday), and skill per day from 3% (Saturday 24) to 21% (Thursday 22). Sunday 25 October had road closures (probably the Dublin Marathon; only 249k arrivals labelled against 386k the Saturday before) and was 29% late; dropping it raises skill by about half a point and changes no ranking. Its delay quantiles beat a constant (pinball loss p90 63 s vs 77 s, 88.8% of delays below the predicted p90), with the median a little high (53.6% below p50). The most useful features are route, minute of day, stop, layover, stop sequence and minutes into the trip.

Model choice (26 October, same features and split, Brier skill): LightGBM with learning rate 0.03 and 255 leaves 15.2% (2 min to train, now the default in `gbm.py`), the earlier settings (learning rate 0.1, 127 leaves) 14.6%, the average of baseline and LightGBM 14.6%, CatBoost (depth 8) 11.7% and 8x slower. The slower LightGBM won on 6 of 7 test days and tied the seventh, so the small gap is real. Three validation days (Friday to Sunday) beat two (a weekend only): two weekend days stopped training after 73 trees, too early for weekday traffic. Next: the mid-term break (26-30 October) as a school-holiday feature, then live features (the bus's current delay) with a fixed prediction horizon.

## Repository layout

| File | Purpose |
|---|---|
| `collector.py` | Polls both realtime feeds every minute, archives raw data, writes in-scope rows to Postgres, archives the timetable zip when it changes |
| `load_static.py` | Loads a timetable zip into PostGIS as a versioned snapshot; nightly mode loads only zips that change something |
| `static_diff.py` | Fingerprints and compares two timetable zips; also a command: `python static_diff.py OLD.zip NEW.zip` |
| `stop_positions.py` | Places every stop along its route shape (table `shape_stops`) |
| `build_arrivals.py` | Builds one day's actual stop arrival times and delays (table `stop_arrivals`) |
| `nightly.sh` | Runs the three nightly steps in order, pings a healthcheck if all succeed |
| `schema.sql` | All tables created up front (`shape_stops` and `stop_arrivals` are created by their scripts) |
| `migrate_supabase.sql` | Imports the earlier Supabase vehicle-position export (not used yet) |
| `model/baseline.py` | Baseline P(late): historical late rate by route, direction, stop, hour and day type |
| `model/gbm.py` | LightGBM P(late) and delay quantiles from timetable, block, calendar and weather features |
| `test_*.py`, `model/test_*.py` | Tests for parsing, loading, timetable comparison, stop placement, baseline and features |
| `docker-compose.yml`, `Dockerfile`, `db/Dockerfile` | Collector image; local Postgres + PostGIS for development |
| `docs/HOW_IT_WORKS.md` | End-to-end explanation of every file and algorithm |

## Quick start (local)

Requires Docker and an NTA API key from [developer.nationaltransport.ie](https://developer.nationaltransport.ie/).

```bash
cp .env.example .env                                      # add NTA_API_KEY
docker compose up -d --build                              # local Postgres + PostGIS and the collector
docker compose exec collector python load_static.py       # after the first timetable zip is downloaded
docker compose logs -f collector
```

Until the timetable is loaded, the collector writes only the raw archive (it does not know which routes are in scope yet).

## Deploying with a managed database

```bash
# .env on the server
NTA_API_KEY=...
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/bus?sslmode=require
HEALTHCHECK_URL=https://hc-ping.com/...          # optional: email if polling stops
NIGHTLY_HEALTHCHECK_URL=https://hc-ping.com/...  # optional: email if a nightly run fails

set -a; . ./.env; set +a
psql "$DATABASE_URL" -f schema.sql                                   # PostGIS must be allow-listed on Azure (azure.extensions)
docker compose build collector
docker compose run --rm --no-deps collector python load_static.py   # load the timetable first
docker compose up -d --no-deps collector                            # --no-deps: skip the local Postgres container
crontab -e   # add: 0 7 * * * /home/azureuser/dublin-bus/nightly.sh >> /home/azureuser/dublin-bus/nightly.log 2>&1
```

## Operations

| Task | Command |
|---|---|
| Collector logs | `docker compose logs -f --since 10m collector` |
| Nightly job log | `tail -40 nightly.log` |
| Run the nightly job by hand | `./nightly.sh` |
| Rebuild one day's labels | `docker compose run --rm --no-deps collector python build_arrivals.py 2026-10-04` |
| Labels per day | `psql "$DATABASE_URL" -c "SELECT service_date, count(*) FROM stop_arrivals GROUP BY 1 ORDER BY 1"` |
| Prune old timetable versions | `docker compose run --rm --no-deps collector python load_static.py --prune` |
| Timetable versions / skipped duplicates | `psql "$DATABASE_URL" -c "SELECT * FROM static_versions" -c "SELECT * FROM static_skipped"` |
| Compare two timetable zips | `docker compose run --rm --no-deps collector python static_diff.py /data/static/A.zip /data/static/B.zip` |
| What the database is doing | `psql "$DATABASE_URL" -c "SELECT pid, state, now()-query_start, left(query,60) FROM pg_stat_activity WHERE datname='bus' AND state<>'idle'"` |
| Raw archive / database size | `du -sh data/raw/*` and `psql "$DATABASE_URL" -c "SELECT pg_size_pretty(pg_database_size('bus'))"` |
| Run tests | `python test_collector.py`, `python test_stop_positions.py`, `python test_static_diff.py`, `DATABASE_URL=... python test_load_static.py` |

Rate limits: polling both feeds once a minute is about 2,900 requests a day. Run only one collector per API key, or NTA returns `429 Too Many Requests`.

## Lessons learned

- **Bulk loads need `ANALYZE`.** After loading 3.8 M rows, a query that normally takes 14 s ran for 16+ minutes because Postgres still planned for an empty table. `load_static.py` now refreshes statistics after every load.
- **Not every change deserves a version.** One day's real change was 2 new trips, yet stored a full 1.8 GB copy, so small trip-only changes are now batched until they pass 1%.
- **Store what the labels need, not every timetable.** Full timetable copies were 1.7 GB of a 2.7 GB database after a week. Copying four trip fields into each label made old versions disposable.
- **NTA timetable IDs are not stable.** A whole day's copy differed from the previous one only in relabelled `service_id`s on 43,140 trips, with zero changed stop times. Comparing dates of operation instead of labels fixed false "changes".
- **Feed trip tags can be wrong for hours.** Without a schedule window, a few trips showed arrivals four hours early.

## Roadmap

- [x] Realtime collection with raw archive, monitoring
- [x] Versioned timetable, loaded only when it really changes
- [x] Arrival-time labels: stop placement and GPS tracking along route shapes, nightly job
- [x] Baseline: historical late rate by route, direction, stop, hour and day type, shrunk towards coarser groups (Brier skill 10.5%, AUC 0.72 on 19-25 Oct)
- [x] Gradient boosting v1 (LightGBM): timetable, block, calendar and weather features; P(late) and delay quantiles (Brier skill 15.2%, AUC 0.75 on 19-25 Oct)
- [ ] Gradient boosting v2: live features (current delay, previous trip in the block) at a fixed horizon, school holidays; benchmark against NTA's predictions at the same horizon
- [ ] Storage lifecycle: compact old raw data, move old GPS rows out of the database
- [ ] Deadline routing: the itinerary with the highest probability of arriving on time (OpenTripPlanner or r5py for candidates)
- [ ] LangGraph agent with planner and predictor as tools ("get me to Trinity by 9")
- [ ] Optional: retrieval over NTA service disruption notices

## Data licence

Realtime and timetable data © National Transport Authority, used under the terms of the [NTA open data licence](https://www.transportforireland.ie/transitData/PT_Data.html).
