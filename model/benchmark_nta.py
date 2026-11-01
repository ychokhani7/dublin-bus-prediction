"""Benchmark our late-arrival model against NTA's own realtime predictions, at fixed prediction horizons.

For each test arrival and each horizon H (5, 15, 30, 60 min), NTA's prediction is what its TripUpdates feed said in
the latest archived snapshot at or before scheduled_at - H: the stop's own delay if listed, else the delay of the
nearest earlier stop on the same trip (GTFS-Realtime carries a delay forward to later stops). No snapshot, trip not
in it, or no stop at or before this one with a delay means "no prediction", which a rider reads as "on time" (0 s).

Our model (model/gbm.py) uses no live data, so its predictions are the same at every horizon. Only NTA changes.

stop_obs in the database is not used: it keeps only NTA's last prediction per stop, made seconds before arrival.
The raw archive keeps every minute, so we can ask "what did NTA say H minutes before?".

Usage (on the Mac, after copying the test week's raw TripUpdates folders from the VM):
  python model/benchmark_nta.py data/labels.csv.gz data/raw/trip_updates [first_day last_day]
first_day/last_day (YYYY-MM-DD) narrow the test days; they must lie inside gbm.py's test days (the last TEST_DAYS).
"""
import gzip
import sys
from bisect import bisect_right
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2 as rt

from baseline import LATE_S, auc

HORIZONS_MIN = [5, 15, 30, 60]
STALE_MIN = 5          # a snapshot older than this before its target means a gap in the archive, reported
CANCELED = rt.TripDescriptor.CANCELED
SKIPPED = rt.TripUpdate.StopTimeUpdate.SKIPPED


def snapshot_files(folder, first, last):
    """[(UTC time, path)] in time order for the UTC days first - 1 .. last (one day early so the first targets of
    the day still find the snapshot before them). File name = feed header time, as collector.py writes it."""
    files = []
    for day in pd.date_range(first - timedelta(days=1), last, freq="D"):
        for f in (Path(folder) / f"{day:%Y-%m-%d}").glob("*.pb.gz"):
            hms = f.name[:6]
            files.append((day.tz_localize("UTC") + pd.Timedelta(hours=int(hms[:2]), minutes=int(hms[2:4]),
                                                                 seconds=int(hms[4:])), f))
    return sorted(files)


def trip_states(feed, wanted):
    """FeedMessage -> {(start_date, trip_id): (stop_sequences, delays)} for the wanted trips, both lists sorted by
    stop_sequence. Same fields as collector.parse(). CANCELED trips are left out (as if not in the feed); SKIPPED
    stops and stops without a delay are left out of the lists."""
    states = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        key = (tu.trip.start_date, tu.trip.trip_id)
        if key not in wanted or tu.trip.schedule_relationship == CANCELED:
            continue
        seq_delay = []
        for s in tu.stop_time_update:
            if not s.HasField("stop_sequence") or s.schedule_relationship == SKIPPED:
                continue
            d = s.arrival.delay if s.arrival.HasField("delay") else (
                s.departure.delay if s.departure.HasField("delay") else None)
            if d is not None:
                seq_delay.append((s.stop_sequence, d))
        seq_delay.sort()
        states[key] = ([q for q, _ in seq_delay], [d for _, d in seq_delay])
    return states


def nta_predictions(files, targets):
    """One time-ordered pass over the snapshots. targets: DataFrame with start_date (YYYYMMDD), trip_id,
    stop_sequence, at (UTC prediction time). Returns (delay, running, age_s) aligned with targets: NTA's predicted
    delay (NaN = no prediction), whether the trip had any delay in that snapshot (already running), and the
    snapshot's age at the target time (NaN = no snapshot). Only one snapshot is held in memory at a time."""
    times = pd.DatetimeIndex([t for t, _ in files], dtype="datetime64[ns, UTC]").asi8
    at = pd.DatetimeIndex(targets["at"]).as_unit("ns").asi8
    idx = np.searchsorted(times, at, side="right") - 1          # latest snapshot at or before the target, never after
    order = np.argsort(idx, kind="stable")
    bounds = np.searchsorted(idx[order], np.arange(len(files) + 1))
    sds, tids, seqs = targets.start_date.to_numpy(), targets.trip_id.to_numpy(), targets.stop_sequence.to_numpy()
    wanted = set(zip(sds, tids))
    delay, running = np.full(len(targets), np.nan), np.zeros(len(targets), bool)
    age = np.where(idx >= 0, (at - times[np.maximum(idx, 0)]) / 1e9, np.nan)
    for i, (_, path) in enumerate(files):
        rows = order[bounds[i]:bounds[i + 1]]
        if len(rows) == 0:
            continue
        feed = rt.FeedMessage()
        feed.ParseFromString(gzip.decompress(path.read_bytes()))
        states = trip_states(feed, wanted)
        for j in rows:
            st = states.get((sds[j], tids[j]))
            if st is None or not st[0]:
                continue
            running[j] = True
            k = bisect_right(st[0], seqs[j]) - 1                # this stop, else the nearest earlier one
            if k >= 0:
                delay[j] = st[1][k]
    return delay, running, age


def same_recall_precision(y, p, recall):
    """Our precision when flagging the highest-P(late) arrivals until we catch the same share of late ones."""
    k = int(round(recall * y.sum()))
    if k == 0:
        return np.nan
    thr = np.sort(p[y == 1])[::-1][k - 1]
    return y[p >= thr].mean()


def scores(df):
    """One report row: NTA vs ours on the same arrivals. NTA's missing predictions count as 0 s (on time)."""
    y, nta, ours = df.late.to_numpy(), df.nta.fillna(0).to_numpy(), df.p_late.to_numpy()
    flag = nta > LATE_S
    recall = flag[y == 1].mean() if y.sum() else np.nan
    both = 0 < y.sum() < len(y)
    return dict(arrivals=len(df), coverage=df.nta.notna().mean(), late=y.mean(),
                auc_nta=auc(y, nta) if both else np.nan, auc_ours=auc(y, ours) if both else np.nan,
                prec_nta=y[flag].mean() if flag.any() else np.nan, recall_nta=recall,
                prec_ours=same_recall_precision(y, ours, recall) if both else np.nan,
                mae_nta=np.abs(nta - df.delay_s).mean(), mae_ours=np.abs(df.p50 - df.delay_s).mean())


def main():
    from gbm import train_predict                              # imported here so the tests don't need LightGBM
    labels, folder = Path(sys.argv[1]), sys.argv[2]
    test = train_predict(labels)[0]
    if len(sys.argv) > 4:
        first, last = pd.Timestamp(sys.argv[3]), pd.Timestamp(sys.argv[4])
        assert test.service_date.min() <= first <= last <= test.service_date.max(), "days outside gbm.py's test days"
        test = test[test.service_date.between(first, last)]
    test = test[["service_date", "trip_id", "stop_sequence", "sched", "delay_s", "late", "p_late", "p50"]]
    test = test.assign(start_date=test.service_date.dt.strftime("%Y%m%d"))

    targets = pd.concat([test.assign(horizon=h, at=test.sched - pd.Timedelta(minutes=h)) for h in HORIZONS_MIN],
                        ignore_index=True)
    files = snapshot_files(folder, test.service_date.min(), test.sched.max().tz_localize(None).normalize())
    print(f"\n{len(test):,} test arrivals {test.service_date.min():%d %b}-{test.service_date.max():%d %b}, "
          f"{len(files):,} TripUpdates snapshots")
    targets["nta"], targets["running"], age = nta_predictions(files, targets)
    stale = ~(age <= STALE_MIN * 60)
    if stale.any():
        print(f"warning: {stale.mean():.1%} of predictions have no snapshot within {STALE_MIN} min before them "
              "(archive gaps); they count as NTA 'no prediction'")

    print("\nOur model uses no live data: its P(late) and p50 are the same at every horizon; only NTA's change.")
    print(f"NTA 'no prediction' counts as 0 s (on time). Late = more than {LATE_S} s. NTA flags late at its own "
          f"{LATE_S} s; ours is shown at the P(late) threshold that catches the same share of late arrivals.")
    rows = []
    for h, g in targets.groupby("horizon"):
        for name, part in [("all", g), ("running", g[g.running]), ("not started", g[~g.running])]:
            if len(part):
                rows.append(dict(horizon=f"{h} min", trips=name, **scores(part)))
    out = pd.DataFrame(rows).set_index(["horizon", "trips"])
    pct = "{:.1%}".format
    print(out.to_string(formatters=dict(arrivals="{:,}".format, coverage=pct, late=pct, prec_nta=pct, recall_nta=pct,
                                        prec_ours=pct, auc_nta="{:.3f}".format, auc_ours="{:.3f}".format,
                                        mae_nta="{:.0f}".format, mae_ours="{:.0f}".format)))
    print("\nrunning = the trip had at least one stop delay in NTA's snapshot at prediction time; "
          "not started = not in the feed, cancelled, or no delays yet. MAE in seconds (NTA's delay vs our p50).")


if __name__ == "__main__":
    main()
