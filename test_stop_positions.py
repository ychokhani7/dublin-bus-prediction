"""Run: python test_stop_positions.py  (no database needed)"""
import numpy as np

from stop_positions import place_stops

# Out-and-back route: east along y=0 for 1,000 m, then back west along y=15 (a parallel road 15 m away).
LINE = np.array([[0, 0], [1000, 0], [1000, 15], [0, 15]], dtype=float)


def test_loop_stop_on_return_leg():
    # stop 3 is near the start, but on the return road: it must be placed near the END of the route
    placed = place_stops(LINE, np.array([[0, -3], [500, -3], [100, 18]], dtype=float))
    assert [round(a) for a, _ in placed] == [0, 500, 1915], placed


def test_one_ambiguous_stop_does_not_drag_the_rest():
    # stop 2 sits between the two roads, a hair closer to the return road (y=8: 8 m vs 7 m).
    # Placing stops one at a time would put it on the return leg and drag stops 3-4 there too.
    stops = np.array([[0, -2], [300, 8], [500, -2], [700, -2]], dtype=float)
    placed = place_stops(LINE, stops)
    assert [round(a) for a, _ in placed] == [0, 300, 500, 700], placed
    assert max(off for _, off in placed) < 10, placed


if __name__ == "__main__":
    test_loop_stop_on_return_leg()
    test_one_ambiguous_stop_does_not_drag_the_rest()
    print("ok")
