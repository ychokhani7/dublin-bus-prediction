"""Run: python model/test_gbm.py  (no database, data file or network needed)"""
import io
import json
import tempfile
from pathlib import Path

import pandas as pd

import gbm
from gbm import add_features, add_weather


def test_trip_and_block_features():
    # One bus (block B1) runs trip T1 08:00-08:30 then T2 08:40-09:00 (UTC; Dublin is UTC+1 in October).
    rows = [("T1", 2, "08:00"), ("T1", 3, "08:15"), ("T1", 4, "08:30"), ("T2", 2, "08:40"), ("T2", 5, "09:00")]
    df = pd.DataFrame([dict(service_date=pd.Timestamp("2026-10-05"), trip_id=t, stop_sequence=s, block_id="B1",
                            scheduled_at=f"2026-10-05 {hm}:00+00:00") for t, s, hm in rows])
    f = add_features(df).set_index(["trip_id", "stop_sequence"])
    assert f.loc[("T1", 3), "min_into_trip"] == 15 and f.loc[("T1", 3), "trip_frac"] == 0.5
    assert f.loc[("T1", 2), "minute_of_day"] == 9 * 60                      # 08:00 UTC is 09:00 in Dublin
    assert f.loc[("T2", 5), "trip_in_block"] == 1 and f.loc[("T2", 5), "min_since_block_start"] == 40
    assert f.loc[("T2", 2), "layover_min"] == 10                            # T1 ends 08:30, T2 starts 08:40
    assert pd.isna(f.loc[("T1", 2), "layover_min"])                         # first trip of the day: no layover
    assert f.loc[("T1", 2), "weekday"] == 0                                 # Monday


def test_weather_refresh_keeps_old_hours():
    # The cache has 4 Oct; the API (92 days back) no longer returns it but has 5 Oct with a corrected 4 Oct 23:00.
    cache_rows = dict(time=["2026-10-04T08:00", "2026-10-04T23:00"], precipitation=[1.0, 2.0])
    api_rows = dict(time=["2026-10-04T23:00", "2026-10-05T08:00"], precipitation=[2.5, 3.0])
    gbm.urlopen = lambda url: io.StringIO(json.dumps({"hourly": api_rows}))
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "weather.csv"
        pd.DataFrame(cache_rows).to_csv(cache, index=False)
        df = pd.DataFrame(dict(sched=pd.to_datetime(["2026-10-04 08:10", "2026-10-05 08:20"], utc=True)))
        out = add_weather(df, cache)
        assert out.precipitation.tolist() == [1.0, 3.0], out              # 4 Oct kept although the API dropped it
        assert pd.read_csv(cache).precipitation.tolist() == [1.0, 2.5, 3.0]  # newer value wins, one row per hour


if __name__ == "__main__":
    test_trip_and_block_features()
    test_weather_refresh_keeps_old_hours()
    print("ok")
