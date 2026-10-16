"""Baseline: P(more than 5 minutes late) from history alone, the yardstick every later model must beat.

For each arrival in the test days, the prediction is the share of late arrivals in the training days for the most
specific group available, shrunk towards the next coarser group when that group has few samples:

    everyone -> route + day type -> + direction + hour -> + stop

    p_group = (late_in_group + M * p_parent) / (arrivals_in_group + M)

A group with 3 arrivals barely moves from its parent's rate; one with 3,000 is almost its own rate. Groups never
seen in training keep the parent's estimate. Days are split by date (train on earlier days, test on the last ones),
never randomly, so the test is "predict days the model has not seen", like real use.

Usage (on your Mac, after copying the export from the VM):
  python model/baseline.py data/labels.csv.gz [test_days]       # default: hold out the last 3 days
"""
import sys

import numpy as np
import pandas as pd

LATE_S = 300            # target: more than 5 minutes late
M = 20                  # shrinkage strength: a group needs ~20 arrivals before it outweighs its parent
LEVELS = [
    ["route_id", "daytype"],
    ["route_id", "direction_id", "daytype", "hour"],
    ["route_id", "direction_id", "stop_id", "daytype", "hour"],
]


def load(path):
    df = pd.read_csv(path, dtype={"route_id": str, "stop_id": str, "trip_id": str, "block_id": str})
    n = len(df)
    df = df[df.delay_s.between(-600, 3600)]           # documented rule: likely GPS-tagging noise
    print(f"{n:,} arrivals, {n - len(df):,} dropped as noise (>10 min early or >60 min late)")
    df["service_date"] = pd.to_datetime(df.service_date)
    dow = df.service_date.dt.dayofweek
    df["daytype"] = np.select([dow == 5, dow == 6], ["sat", "sun"], "weekday")
    df["hour"] = pd.to_datetime(df.scheduled_at, utc=True).dt.tz_convert("Europe/Dublin").dt.hour
    df["late"] = (df.delay_s > LATE_S).astype(float)
    return df


def predict(train, test):
    """Yield (level name, predicted probabilities for test), from coarsest to finest.
    shortcut: a group whose rows equal its parent's is counted once per level, so tiny groups drift a little
    further from the parent than true empirical Bayes would allow; fine at real group sizes, revisit if not."""
    p = np.full(len(test), train.late.mean())
    yield "everyone (overall late rate)", p
    for keys in LEVELS:
        g = train.groupby(keys).late.agg(["sum", "count"])
        j = test[keys].join(g, on=keys)
        p = np.where(j["count"].notna(), (j["sum"] + M * p) / (j["count"] + M), p)
        yield " + ".join(keys), p


def auc(y, p):
    """Probability a random late arrival gets a higher score than a random on-time one (rank formula)."""
    r = pd.Series(p).rank().values
    n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


def main():
    path = sys.argv[1]
    n_test = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    df = load(path)
    days = sorted(df.service_date.unique())
    test_days = days[-n_test:]
    train, test = df[~df.service_date.isin(test_days)], df[df.service_date.isin(test_days)]
    fmt = lambda ds: ", ".join(pd.Timestamp(d).strftime("%a %d %b") for d in ds)
    print(f"train: {len(train):,} arrivals, {len(days) - n_test} days ({fmt(days[:-n_test])})")
    print(f"test:  {len(test):,} arrivals, {n_test} days ({fmt(test_days)})")
    print(f"late (>{LATE_S // 60} min): train {train.late.mean():.1%}, test {test.late.mean():.1%}\n")

    y = test.late.values
    base_brier = None
    print(f"{'prediction uses':58} {'Brier':>7} {'skill':>7} {'log loss':>9} {'AUC':>6}")
    for name, p in predict(train, test):
        brier = np.mean((p - y) ** 2)
        base_brier = base_brier or brier
        pc = np.clip(p, 1e-6, 1 - 1e-6)
        logloss = -np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc))
        print(f"{name:58} {brier:7.4f} {1 - brier / base_brier:7.1%} {logloss:9.4f} {auc(y, p):6.3f}")

    print("\ncalibration of the final level (predicted vs actual share late):")
    bins = pd.cut(p, [0, .05, .1, .2, .3, .4, .5, .7, 1], include_lowest=True)
    cal = pd.DataFrame({"bin": bins, "p": p, "y": y}).groupby("bin", observed=True).agg(
        arrivals=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
    print(cal.to_string(formatters={"predicted": "{:.1%}".format, "actual": "{:.1%}".format}))


if __name__ == "__main__":
    main()
