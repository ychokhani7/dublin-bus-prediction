"""Run: python test_collector.py  (no framework needed)"""
from google.transit import gtfs_realtime_pb2 as rt

from collector import parse, parse_vehicles


def test_parse():
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"

    tu = feed.entity.add(id="1").trip_update
    tu.trip.CopyFrom(rt.TripDescriptor(trip_id="T1", route_id="R39A", start_date="20261003", start_time="08:00:00",
                                       direction_id=1))
    tu.vehicle.id = "V9"
    tu.stop_time_update.add(stop_sequence=5, stop_id="S5").arrival.delay = 240
    tu.stop_time_update.add(stop_sequence=6, stop_id="S6")   # no arrival info
    tu.stop_time_update.add(stop_id="S7")                    # no stop_sequence -> skipped

    c = feed.entity.add(id="2").trip_update
    c.trip.CopyFrom(rt.TripDescriptor(trip_id="T2", start_date="20261003",
                                      schedule_relationship=rt.TripDescriptor.CANCELED))

    feed.entity.add(id="3").vehicle.vehicle.id = "V1"           # not a trip update -> ignored

    trips, stops = parse(feed)
    assert trips == [
        ("20261003", "T1", "R39A", 1, "08:00:00", "SCHEDULED", "V9"),
        ("20261003", "T2", None, None, None, "CANCELED", None),
    ], trips
    assert stops == [
        ("20261003", "T1", 5, "R39A", "S5", 240, None, None, "SCHEDULED"),
        ("20261003", "T1", 6, "R39A", "S6", None, None, None, "SCHEDULED"),
    ], stops


def test_parse_vehicles():
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1759478460
    v = feed.entity.add(id="1").vehicle
    v.trip.CopyFrom(rt.TripDescriptor(trip_id="T1", route_id="R39A", start_date="20261003"))
    v.vehicle.id = "V9"
    v.position.latitude, v.position.longitude, v.position.bearing = 53.3438, -6.2546, 90.0
    v.timestamp = 1759478400
    w = feed.entity.add(id="2").vehicle                          # no GPS fix time -> falls back to feed time
    w.trip.trip_id = "T2"
    w.position.latitude, w.position.longitude = 53.35, -6.26
    feed.entity.add(id="3").vehicle.trip.trip_id = "T3"          # no position -> skipped
    feed.entity.add(id="4").vehicle.position.latitude = 53.0     # no trip -> skipped

    rows = parse_vehicles(feed)
    assert [(r[0].timestamp(), r[1], r[2], r[5], r[8]) for r in rows] == [
        (1759478400, "T1", "20261003", "V9", 90.0), (1759478460, "T2", None, None, None)], rows
    assert abs(rows[0][6] - 53.3438) < 1e-4 and abs(rows[0][7] + 6.2546) < 1e-4


if __name__ == "__main__":
    test_parse()
    test_parse_vehicles()
    print("ok")
