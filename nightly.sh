#!/bin/sh
# Nightly label job (run by cron on the VM, after the last after-midnight trips have finished):
#   1. load a new timetable version if the collector downloaded one
#   2. place stops along any new route shapes
#   3. build yesterday's stop arrivals (the training labels)
#   4. drop all but the newest two timetable versions (labels no longer need them)
# If any step fails, the script stops and the healthcheck ping is not sent, so you get an alert email.
set -eu
cd "$(dirname "$0")"
set -a; . ./.env; set +a
run() { /usr/bin/docker compose run --rm --no-deps -T collector "$@"; }

echo "=== $(date -u '+%Y-%m-%d %H:%M UTC') ==="
run python load_static.py
run python stop_positions.py
run python build_arrivals.py
run python load_static.py --prune
if [ -n "${NIGHTLY_HEALTHCHECK_URL:-}" ]; then
  curl -fsS -m 10 -o /dev/null "$NIGHTLY_HEALTHCHECK_URL"
fi
echo "=== done ==="
