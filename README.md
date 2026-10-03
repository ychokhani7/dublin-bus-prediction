# Dublin Bus Delay Predictor

Predicts the chance that a Dublin bus or tram gets you somewhere **on time**, and picks the route most likely to arrive before your deadline rather than just the fastest one on paper. It is built on a live pipeline that has collected the National Transport Authority (NTA) realtime feeds every minute since October 2026.

> **Status:** data collection is live (Dublin Bus, Go-Ahead Ireland, LUAS). Arrival-time labels, models and routing are in progress. See [Roadmap](#roadmap).

## Architecture

```
NTA GTFS-Realtime API  ──every 60 s──>  collector.py (Docker, Azure VM)
  TripUpdates + Vehicles                  │
                                          ├─> data/raw/<feed>/<day>/*.pb.gz   full national feed, as received (source of truth)
                                          └─> PostgreSQL + PostGIS (Azure)    Dublin operators only
                                                ├─ vehicle_obs   GPS fixes (ground truth)
                                                ├─ trip_obs      trips, incl. cancellations
                                                └─ stop_obs      NTA's delay predictions

NTA static GTFS zip  ──every 6 h──>  data/static/*.zip (one file per timetable version)
                                          └─ load_static.py ─> routes, trips, stop_times, stops, shapes, calendar
                                                               (versioned; geometry in EPSG:2157, metres)
```

## Design decisions

**GPS positions are the ground truth, not NTA's predictions.** TripUpdates contain *predicted* delays, and NTA sends only a few stops per trip (median 3, with 31% of trips carrying a single stop update). Vehicle positions record where each bus actually was, so actual arrival times are derived by projecting GPS fixes onto the route shape and interpolating when the bus passed each stop. A naive "GPS ping within 50 m of a stop" approach was rejected: at 60 s polling a bus covers 300 to 500 m between pings, so it misses most stops and over-samples the ones where the bus was stuck, biasing labels toward delay.

**Raw first.** Every response is written to disk, gzipped, before any parsing. Database tables are a rebuildable view: if the labelling logic improves or a new field is needed, the archive is re-parsed rather than recollected.

**Versioned timetables.** Realtime `trip_id`s only make sense against the timetable that was live at the time, and NTA publishes new versions every month or two. Each archived zip is loaded as its own snapshot (`static_versions`), never merged into the previous one.

**Store only the operators we model.** The NTA feed is national (Bus Éireann, Irish Rail and others too). The database keeps Dublin Bus, Go-Ahead Ireland and LUAS to stay within a 32 GB budget; the raw archive keeps everything, so widening scope later costs a re-parse, not lost history.

**Infrastructure on free tiers.** Azure for Students: a B2ats v2 VM (collector + raw archive) and a managed PostgreSQL Flexible Server B1ms with PostGIS. Alternatives considered: Supabase free (500 MB cap, filled in about a week), Render free (ephemeral disk, needs keep-alive pings), Oracle Always Free (viable fallback when the Azure credit ends).

## What the data looks like

Measured from live feeds on 3 October 2026:

| | |
|---|---|
| Realtime trips per snapshot (national) | ~2,700, of which ~1,500 are Dublin Bus, Go-Ahead or LUAS |
| Vehicles per snapshot (national) | ~1,050, of which ~675 in scope |
| Cancelled trips in a snapshot | ~2.4% |
| Realtime `trip_id`s found in the static timetable | 99.8% |
| Median GPS fix age | 25 s (p90 82 s) |
| Feed size per poll (uncompressed) | ~680 KB TripUpdates, ~90 KB Vehicles |
| Timetable loaded (in-scope operators) | 182 routes, 87,421 trips, 4.26 M stop times, 1,559 shapes |

## Repository layout

| File | Purpose |
|---|---|
| `collector.py` | Polls both realtime feeds every minute, archives raw data, writes in-scope rows to Postgres, archives new timetable versions |
| `load_static.py` | Loads one timetable zip into PostGIS as a versioned snapshot |
| `schema.sql` | All tables |
| `check_feeds.py` | One-off profile of the live feeds and timetable (fields present, match rates, sizes) |
| `migrate_supabase.sql` | Imports the earlier Supabase vehicle-position export |
| `test_collector.py`, `test_load_static.py` | Tests for parsing and loading |
| `docker-compose.yml`, `Dockerfile`, `db/Dockerfile` | Collector image; local Postgres + PostGIS for development |

## Quick start (local)

Requires Docker and an NTA API key from [developer.nationaltransport.ie](https://developer.nationaltransport.ie/).

```bash
cp .env.example .env                                      # add NTA_API_KEY
docker compose up -d --build                              # local Postgres + PostGIS and the collector
docker compose exec collector python load_static.py       # after the first timetable zip is downloaded
docker compose logs -f collector
```

Until `load_static.py` has run, the collector writes only the raw archive (it does not know which routes are in scope yet).

## Deploying with a managed database

```bash
# .env on the server
NTA_API_KEY=...
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/bus?sslmode=require
HEALTHCHECK_URL=https://hc-ping.com/...        # optional, emails you if polling stops

set -a; . ./.env; set +a
psql "$DATABASE_URL" -f schema.sql                                   # PostGIS must be allow-listed on Azure (azure.extensions)
docker compose build collector
docker compose run --rm --no-deps collector python load_static.py   # load the timetable first
docker compose up -d --no-deps collector                            # --no-deps: skip the local Postgres container
```

## Operations

| Task | Command |
|---|---|
| Collector logs | `docker compose logs -f --since 10m collector` |
| Running containers | `docker compose ps` |
| Rows collected | `psql "$DATABASE_URL" -c "SELECT count(*) FROM vehicle_obs"` |
| Raw archive size | `du -sh data/raw/*` |
| Database size | `psql "$DATABASE_URL" -c "SELECT pg_size_pretty(pg_database_size('bus'))"` |
| Load a new timetable version | `docker compose run --rm --no-deps collector python load_static.py` |
| Profile the live feeds | `docker compose run --rm --no-deps collector python check_feeds.py` |
| Run tests | `python test_collector.py` and `DATABASE_URL=... python test_load_static.py` |

Monitoring: the collector pings `HEALTHCHECK_URL` after each successful poll; a missed ping for about 10 minutes triggers an email.

Rate limits: polling both feeds once a minute is about 2,900 requests a day. Run only one collector per API key, or NTA returns `429 Too Many Requests`.

## Roadmap

- [x] Realtime collection with raw archive, versioned timetable, monitoring
- [ ] Arrival-time labels: project GPS fixes onto route shapes (PostGIS linear referencing), interpolate stop passage times, nightly job
- [ ] Storage lifecycle: compact old raw data, move older GPS rows out of the database
- [ ] Baseline: historical delay distribution by route, stop, hour and weekday
- [ ] Gradient boosting with weather and previous-trip-in-block delay; predict delay quantiles, not just a threshold
- [ ] Deadline routing: choose the itinerary with the highest probability of arriving on time (OpenTripPlanner or r5py for candidates)
- [ ] LangGraph agent with planner and predictor as tools ("get me to Trinity by 9")
- [ ] Optional: retrieval over NTA service disruption notices

## Data licence

Realtime and timetable data © National Transport Authority, used under the terms of the [NTA open data licence](https://www.transportforireland.ie/transitData/PT_Data.html).
