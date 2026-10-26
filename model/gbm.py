"""Gradient boosting (LightGBM): P(more than 5 minutes late) and delay quantiles, from what is known before the bus leaves.

Version 1 uses no live data: timetable position, the bus's day (block), calendar and weather. That suits planning a
trip in advance and compares directly with model/baseline.py. Live features (the bus's current delay) come later.

Split by date, never randomly: the last TEST_DAYS days are the test (same as the baseline), the VALID_DAYS before them
only pick the number of trees (early stopping), then each model is refit on all non-test days with that many trees.

Usage (on your Mac, after copying the export from the VM):
  python model/gbm.py data/labels.csv.gz
"""
import json
import sys
from pathlib import Path
from urllib.request import urlopen

import lightgbm as lgb
import numpy as np
import pandas as pd

from baseline import auc, load, predict

TEST_DAYS, VALID_DAYS = 7, 3
QUANTILES = [0.1, 0.5, 0.9]
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=53.35&longitude=-6.26"
               "&hourly=precipitation,temperature_2m,wind_speed_10m&past_days=92&forecast_days=1&timezone=UTC")
CATEGORIES = ["route_id", "stop_id"]
FEATURES = CATEGORIES + [
    "direction_id", "timepoint", "weekday", "minute_of_day",                     # where and when
    "stop_sequence", "min_into_trip", "trip_min", "trip_frac",                   # position in the trip
    "trip_in_block", "min_since_block_start", "layover_min",                     # the bus's day so far
    "precipitation", "temperature_2m", "wind_speed_10m",                         # weather that hour
]
PARAMS = dict(learning_rate=0.03, num_leaves=255, min_data_in_leaf=200, min_data_per_group=200, cat_smooth=50,
              feature_fraction=0.9, verbose=-1)


def add_features(df):
    """Timetable, block and calendar features. All are known from the timetable before the trip runs.
    shortcut: trip and block times come from the labelled stops (the first stop of a trip never has a label, and the
    timetable versions for old days are pruned), so a trip "starts" at its second stop; export trip schedules from
    the timetable if this ever matters."""
    sched = pd.to_datetime(df.scheduled_at, utc=True)
    local = sched.dt.tz_convert("Europe/Dublin")
    df["sched"] = sched
    df["minute_of_day"] = local.dt.hour * 60 + local.dt.minute
    df["weekday"] = df.service_date.dt.dayofweek
    trip = df.groupby(["service_date", "trip_id"]).sched
    start, end = trip.transform("min"), trip.transform("max")
    df["min_into_trip"] = (sched - start).dt.total_seconds() / 60
    df["trip_min"] = (end - start).dt.total_seconds() / 60
    df["trip_frac"] = df.min_into_trip / df.trip_min.where(df.trip_min > 0)

    trips = (df.groupby(["service_date", "block_id", "trip_id"]).sched.agg(["min", "max"]).reset_index()
             .sort_values(["service_date", "block_id", "min"]))
    block = trips.groupby(["service_date", "block_id"])
    trips["trip_in_block"] = block.cumcount()
    trips["min_since_block_start"] = (trips["min"] - block["min"].transform("min")).dt.total_seconds() / 60
    trips["layover_min"] = (trips["min"] - block["max"].shift()).dt.total_seconds() / 60   # NaN for the first trip
    cols = ["service_date", "block_id", "trip_id", "trip_in_block", "min_since_block_start", "layover_min"]
    return df.merge(trips[cols], on=["service_date", "block_id", "trip_id"], how="left")


def add_weather(df, cache):
    """Hourly Dublin weather from Open-Meteo, cached next to the labels. Observed values: in production these
    would be the forecast for that hour."""
    if not cache.exists() or pd.read_csv(cache).time.max() < df.sched.max().strftime("%Y-%m-%dT%H:00"):
        pd.DataFrame(json.load(urlopen(WEATHER_URL))["hourly"]).to_csv(cache, index=False)
    w = pd.read_csv(cache)
    w["hour"] = pd.to_datetime(w.time).dt.tz_localize("UTC")
    df["hour"] = df.sched.dt.floor("h")
    return df.merge(w.drop(columns="time"), on="hour", how="left")


def fit(objective, train, valid, label, **extra):
    """Pick the number of trees on `valid`, then refit on train + valid with that many."""
    params = dict(PARAMS, objective=objective, **extra)
    dtrain = lgb.Dataset(train[FEATURES], train[label])
    probe = lgb.train(params, dtrain, 2000, valid_sets=[lgb.Dataset(valid[FEATURES], valid[label], reference=dtrain)],
                      callbacks=[lgb.early_stopping(50, verbose=False)])
    both = pd.concat([train, valid])
    return lgb.train(params, lgb.Dataset(both[FEATURES], both[label]), probe.best_iteration), probe.best_iteration


def scores(y, p):
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    return np.mean((p - y) ** 2), -np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc)), auc(y, p)


def pinball(y, p, q):
    d = y - p
    return np.mean(np.maximum(q * d, (q - 1) * d))


def main():
    path = Path(sys.argv[1])
    df = load(path)
    days = sorted(df.service_date.unique())
    test_days, valid_days = days[-TEST_DAYS:], days[-TEST_DAYS - VALID_DAYS:-TEST_DAYS]
    is_test, is_valid = df.service_date.isin(test_days), df.service_date.isin(valid_days)
    p_base = list(predict(df[~is_test], df[is_test]))[-1][1]      # baseline trains on every non-test day

    n = len(df)
    df = add_weather(add_features(df), path.parent / "weather.csv")
    assert len(df) == n, "feature joins must keep one row per arrival"
    for c in CATEGORIES:                                          # one category list for train and test alike
        df[c] = df[c].astype("category")
    is_test, is_valid = df.service_date.isin(test_days), df.service_date.isin(valid_days)
    train, valid, test = df[~is_test & ~is_valid], df[is_valid], df[is_test]
    fmt = lambda ds: f"{pd.Timestamp(ds[0]):%d %b}-{pd.Timestamp(ds[-1]):%d %b}"
    print(f"train {fmt(days[:-TEST_DAYS - VALID_DAYS])} ({len(train):,}), valid {fmt(valid_days)} ({len(valid):,}), "
          f"test {fmt(test_days)} ({len(test):,})")

    model, trees = fit("binary", train, valid, "late")
    p = model.predict(test[FEATURES])
    y = test.late.values
    rate_brier = np.mean((pd.concat([train, valid]).late.mean() - y) ** 2)   # always predicting the overall rate
    print(f"\nP(late), {trees} trees     {'Brier':>7} {'skill':>7} {'log loss':>9} {'AUC':>6}")
    for name, pr in [("baseline (history by stop)", p_base), ("gradient boosting", p)]:
        brier, logloss, area = scores(y, pr)
        print(f"{name:26} {brier:7.4f} {1 - brier / rate_brier:7.1%} {logloss:9.4f} {area:6.3f}")

    bins = pd.cut(p, [0, .05, .1, .2, .3, .4, .5, .7, 1], include_lowest=True)
    cal = pd.DataFrame({"bin": bins, "p": p, "y": y}).groupby("bin", observed=True).agg(
        arrivals=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
    print("\ncalibration (predicted vs actual share late):")
    print(cal.to_string(formatters={"predicted": "{:.1%}".format, "actual": "{:.1%}".format}))

    print(f"\ndelay quantiles     {'pinball s':>9} {'vs constant':>11} {'actual below':>12}")
    for q in QUANTILES:
        m, _ = fit("quantile", train, valid, "delay_s", alpha=q)
        pq = m.predict(test[FEATURES])
        const = np.quantile(pd.concat([train, valid]).delay_s, q)
        print(f"p{round(q * 100):<18} {pinball(test.delay_s.values, pq, q):9.1f} "
              f"{pinball(test.delay_s.values, const, q):11.1f} {np.mean(test.delay_s.values < pq):12.1%}")

    gain = pd.Series(model.feature_importance("gain"), FEATURES).sort_values(ascending=False)
    print("\nP(late) feature importance (share of total gain):")
    print((gain / gain.sum()).map("{:.1%}".format).to_string())


if __name__ == "__main__":
    main()
