# CLAUDE.md

Context for Claude Code working in this repo. Read `README.md` (what and why) and `docs/HOW_IT_WORKS.md` (every file and algorithm) before changing code.

## Project in one paragraph
Portfolio project: predict the probability that a Dublin bus or LUAS arrives late, route by deadline, and later expose it through a LangGraph agent. A collector polls NTA GTFS-Realtime every minute into a raw archive and PostGIS; a nightly job turns GPS into actual stop arrival times (`stop_arrivals`), the training labels. Collection has run since 3 October 2026. Owner: Yash, backend engineer (Java background, learning Python, LangChain/LangGraph, ML), targeting Dublin and Amsterdam roles.

## Where things run
- **Azure VM** (Ubuntu 24.04, B2ats v2, India South Central, user `azureuser`, project in `~/dublin-bus`): the collector container (`docker compose up -d --no-deps collector`, always on), the raw archive in `data/`, and cron running `nightly.sh` at 07:00 UTC (log: `nightly.log`). SSH is restricted to Yash's IP. Connection details are in his `~/.ssh/config`, not in this repo.
- **Azure PostgreSQL Flexible Server** (B1ms, 32 GiB, PostGIS allow-listed), database `bus`. Connection string only in `.env` on the VM. Firewall allows the VM's IP.
- **Mac** (this repo): development, tests, and model training on exported labels (`data/` is gitignored).
- **Monitoring:** healthchecks.io pings from the collector (`HEALTHCHECK_URL`) and the nightly job (`NIGHTLY_HEALTHCHECK_URL`), emailing on silence.

## Conventions
- One Docker image for everything on the VM. Scripts run as `docker compose run --rm --no-deps collector python <script>.py`. `--no-deps` matters: never start the local `db` service on the VM.
- In a VM shell, `set -a; . ./.env; set +a` before using `psql "$DATABASE_URL"`.
- Deploy: copy changed files to `~/dublin-bus/` on the VM, then `docker compose build collector`. The running collector only picks up code changes after `docker compose up -d --no-deps collector`.
- Every new non-trivial logic gets a small test (`test_*.py`, plain asserts, run with `python test_x.py`). `test_load_static.py` needs a Postgres with PostGIS via `DATABASE_URL`.
- Keep `README.md` and `docs/HOW_IT_WORKS.md` updated in the same change as the code.
- Writing style for docs and messages: no em dashes.
- Working style: follow Yash's instructions as given; offer suggestions alongside, don't swap in a different approach or scope. Ask when something is unclear instead of improvising. Explain the why, he is learning.
- Never commit `.env`, `data/`, `nightly.log`. Never run two collectors on one NTA API key (429 errors).
- Nothing destructive on the VM or database (deleting data, dropping tables, `VACUUM FULL`) without Yash agreeing first.

## Gotchas learned the hard way
- Run `ANALYZE` after bulk loads (already in `load_static.py`); stale statistics once turned a 14 s query into 16+ minutes.
- NTA republishes the timetable daily and relabels `service_id`s; `static_diff.py` compares actual dates of operation, and only real changes touching 1%+ of trips (or any route, stop, shape) become a new version. Only the newest 2 versions are kept (`load_static.py --prune`); labels carry route, direction, block and timepoint so they don't need old versions.
- Feed trip tags can be wrong for hours: `build_arrivals.py` only uses GPS within 30 min before to 90 min after the scheduled trip.
- Delete a timetable version only after rebuilding labels that used it.
- Database disk usage only flattens after deletes (space is reused, not returned).

## Current state (16 October 2026)
- Labels exist from 4 October, about 470k arrivals per weekday, 92-93% of scheduled stops, median delay about +2 min, about 25% more than 5 min late.
- Work through 16 Oct is committed on branch `nightly-labels` (not merged or pushed).
- Models re-run 26 Oct on labels 4-25 Oct (9.39M rows; test 19-25 Oct, valid 16-18 Oct for early stopping, train 4-15 Oct). Baseline: Brier 0.164, skill 10.5% over the overall rate, AUC 0.72. LightGBM v1 (`model/gbm.py`, no live features, learning rate 0.03, 255 leaves): Brier 0.155, skill 15.2%, AUC 0.75, calibrated within ~3 points, predicts 25.0% late vs 24.1% actual. Results in the README. The daily late share still swings 15-31% and skill per day 3-21%; 25 Oct (road closures, probably the marathon, 249k arrivals) changes no ranking. Weather in `data/weather.csv` refreshes itself when labels run past it. Python for the model: `.venv` on the Mac (`pip install -r model/requirements.txt`).

## Open items, in order
1. **Database growth.** Storage went from about 25% to 35% of 32 GiB in 6 days (about 0.5 GB/day, ~40 days left). Measured 16 Oct: about 4 GB of "Storage used" is fixed Azure overhead; observation tables grow about 0.23 GB/day; the rest was a bug (pruned timetable zips were reloaded every night, leaving about 1.5 GB of reusable free space in `stop_times`), fixed and deployed 16 Oct. Re-check growth after a few nights. Likely next fix, to agree with Yash: nightly delete `vehicle_obs` rows older than ~7 days once their labels exist (the raw archive keeps every GPS fix, so they can be rebuilt). `stop_obs` keeps only NTA's last prediction (nearly the actual time), so it is not the benchmark; the raw archive is. It can be trimmed the same way.
2. **Gradient boosting v2** (v1 done in `model/gbm.py`). Compared 26 Oct on a 7-day test (Brier skill): LightGBM lr 0.03 + 255 leaves 15.2% (won 6 of 7 days, now the default), lr 0.1 + 127 leaves 14.6%, average of baseline and LightGBM 14.6%, CatBoost depth 8 11.7% (8x slower). VALID_DAYS 3 beats 2 (a weekend-only validation stopped at 73 trees). Model settings are settled; next gains come from features. Then: add live features (the bus's current delay, previous trip in the block) at a fixed prediction horizon, and school holidays once the late-October mid-term is in the data. Split by date, never randomly. Benchmark against NTA's predictions at the same horizon, read from `data/raw/trip_updates/` (`stop_obs` keeps only the last one). Watch the Irish school mid-term break (late October) in any test period.
3. Later: deadline routing (OpenTripPlanner or r5py), then the LangGraph agent.
