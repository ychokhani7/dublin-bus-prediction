"""Run: python model/test_baseline.py  (no database or data file needed)"""
import numpy as np
import pandas as pd

from baseline import M, predict


def frame(rows):
    return pd.DataFrame(rows, columns=["route_id", "direction_id", "stop_id", "daytype", "hour", "late"])


def test_shrinkage_and_fallback():
    # route A at stop S1, 8am: 100 arrivals, 80 late. Route B: 100 arrivals, none late.
    train = frame([("A", 0, "S1", "weekday", 8, 1.0)] * 80 + [("A", 0, "S1", "weekday", 8, 0.0)] * 20
                  + [("B", 0, "S2", "weekday", 8, 0.0)] * 100)
    test = frame([("A", 0, "S1", "weekday", 8, 1.0),     # seen group: close to its own 80%
                  ("A", 0, "S9", "weekday", 8, 0.0),     # unseen stop: falls back to route A's estimate
                  ("C", 0, "S1", "weekday", 8, 0.0)])    # unseen route: overall rate, 40%
    *_, (_, p) = predict(train, test)
    assert 0.75 < p[0] < 0.8, p                            # 80% shrunk slightly towards the parents
    assert 0.75 < p[1] < p[0], p                           # unseen stop: route/direction/hour estimate, one level coarser
    assert abs(p[2] - 0.4) < 1e-9, p
    small = frame([("A", 0, "S1", "weekday", 8, 1.0)] * 2 + [("B", 0, "S2", "weekday", 8, 0.0)] * 198)
    *_, (_, q) = predict(small, test[:1])
    assert q[0] < 0.3, q                                   # 2 late out of 2 is not trusted: nowhere near 100%


if __name__ == "__main__":
    test_shrinkage_and_fallback()
    print("ok")
