"""Run: python model/test_gbm.py  (no database, data file or network needed)"""
import pandas as pd

from gbm import add_features


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


if __name__ == "__main__":
    test_trip_and_block_features()
    print("ok")
