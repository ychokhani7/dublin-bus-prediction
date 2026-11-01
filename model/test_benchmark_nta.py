"""Run: python model/test_benchmark_nta.py  (no database, data file, network or LightGBM needed)"""
import gzip
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2 as rt

from benchmark_nta import nta_predictions, same_recall_precision, snapshot_files


def write_feed(folder, hhmmss, trips):
    """trips: [(trip_id, canceled, [(stop_sequence, arrival_delay, departure_delay, skipped)])]"""
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for n, (tid, canceled, stops) in enumerate(trips):
        tu = feed.entity.add(id=str(n)).trip_update
        tu.trip.CopyFrom(rt.TripDescriptor(trip_id=tid, start_date="20261019"))
        if canceled:
            tu.trip.schedule_relationship = rt.TripDescriptor.CANCELED
        for seq, arr, dep, skipped in stops:
            s = tu.stop_time_update.add(stop_sequence=seq)
            if arr is not None:
                s.arrival.delay = arr
            if dep is not None:
                s.departure.delay = dep
            if skipped:
                s.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED
    day = folder / "2026-10-19"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{hhmmss}.pb.gz").write_bytes(gzip.compress(feed.SerializeToString()))


def test_nta_predictions():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        write_feed(folder, "080000", [("T1", False, [(3, 60, None, False)])])
        write_feed(folder, "080100", [("T1", False, [(3, 120, None, False), (5, None, 200, False), (6, 400, None, True)]),
                                      ("T2", True, [(1, 900, None, False)]),           # cancelled: ignored
                                      ("T3", False, [(7, 300, None, False)])])         # only a later stop
        write_feed(folder, "080200", [("T1", False, [(3, 999, None, False)])])
        files = snapshot_files(folder, pd.Timestamp("2026-10-19"), pd.Timestamp("2026-10-19"))
        assert [t.strftime("%H:%M:%S") for t, _ in files] == ["08:00:00", "08:01:00", "08:02:00"]

        t = lambda hms: pd.Timestamp(f"2026-10-19 {hms}", tz="UTC")
        targets = pd.DataFrame([
            ("T1", 3, t("08:00:30")),     # between snapshots: the 08:00 one, never the later 08:01
            ("T1", 3, t("08:01:00")),     # exactly at a snapshot time: that snapshot
            ("T1", 4, t("08:01:59")),     # no update for stop 4: carried forward from stop 3
            ("T1", 5, t("08:01:10")),     # own stop, departure delay only
            ("T1", 6, t("08:01:10")),     # own stop SKIPPED: carried forward from stop 5
            ("T1", 3, t("07:59:59")),     # before the first snapshot: no prediction
            ("T2", 1, t("08:01:30")),     # cancelled trip: no prediction, not running
            ("T3", 2, t("08:01:30")),     # running, but nothing at or before stop 2: no prediction
            ("T9", 1, t("08:01:30")),     # trip not in the feed
            ("T1", 3, t("08:02:00")),     # latest snapshot
        ], columns=["trip_id", "stop_sequence", "at"]).assign(start_date="20261019")
        delay, running, age = nta_predictions(files, targets)

    expect = [60, 120, 120, 200, 200, np.nan, np.nan, np.nan, np.nan, 999]
    assert np.array_equal(delay, expect, equal_nan=True), delay
    assert running.tolist() == [True, True, True, True, True, False, False, True, False, True], running
    assert age[0] == 30 and age[1] == 0 and np.isnan(age[5]), age


def test_same_recall_precision():
    y = np.array([1, 1, 0, 1, 0, 0.])
    p = np.array([.9, .8, .7, .6, .2, .1])
    assert same_recall_precision(y, p, 2 / 3) == 1.0           # top 2 are both late
    assert same_recall_precision(y, p, 1.0) == 0.75            # catching all 3 late flags 4 arrivals
    assert np.isnan(same_recall_precision(y, p, 0.0))


if __name__ == "__main__":
    test_nta_predictions()
    test_same_recall_precision()
    print("ok")
