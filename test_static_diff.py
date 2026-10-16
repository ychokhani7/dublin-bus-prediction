"""Run: python test_static_diff.py  (no database needed)"""
import io
import zipfile

import test_load_static as t
from static_diff import compare, differences, fingerprint, is_minor

# Base timetable: the test feed, a second Dublin Bus trip on S1, and a service (S0) that ends on 2 October.
BASE = dict(t.FILES)
BASE["feed_info.txt"] = "feed_publisher_name,feed_version,feed_start_date,feed_end_date\nNTA,V1,20261001,20271001\n"
BASE["trips.txt"] += "1 39A b a,S0,T0,UCD,0,B7,SH1\n1 39A b a,S1,T2,UCD,0,B7,SH1\n"
BASE["stop_times.txt"] += "T0,07:00:00,07:00:00,A,1,1\nT0,07:10:00,07:10:00,B,2,0\nT2,09:00:00,09:00:00,A,1,1\n"
BASE["calendar.txt"] += "S0,1,1,1,1,1,1,1,20261001,20261002\n"
BASE["calendar_dates.txt"] = "service_id,date,exception_type\n"


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return zipfile.ZipFile(buf)


def republished_files(**changes):
    """NTA's next-day copy: new feed_version, window starts 3 Oct, finished service S0 and its trip T0 dropped."""
    f = dict(BASE)
    f["feed_info.txt"] = "feed_publisher_name,feed_version,feed_start_date,feed_end_date\nNTA,V2,20261003,20271003\n"
    f["trips.txt"] = f["trips.txt"].replace("1 39A b a,S0,T0,UCD,0,B7,SH1\n", "")
    f["stop_times.txt"] = f["stop_times.txt"].replace("T0,07:00:00,07:00:00,A,1,1\nT0,07:10:00,07:10:00,B,2,0\n", "")
    f["calendar.txt"] = f["calendar.txt"].replace("S0,1,1,1,1,1,1,1,20261001,20261002\n", "")
    for name, (old, new) in changes.items():
        name = name.replace("_txt", ".txt")
        assert not old or old in f[name], f"test fixture: {old!r} not in {name}"
        f[name] = f[name].replace(old, new) if old else f[name] + new
    return f


def diff(**changes):
    return differences(fingerprint(zipped(BASE)), fingerprint(zipped(republished_files(**changes))))


def test_all():
    assert diff() == [], diff()                                       # daily republish: nothing changed
    assert diff(stop_times_txt=("T1,24:10:30,24:10:30", "T1,24:12:30,24:12:30")) == \
        ["1 trips with changed stops, times or details"]
    assert diff(trips_txt=("", "1 39A b a,S1,T9,UCD,0,B7,SH1\n")) == ["1 new trips"]
    assert diff(calendar_dates_txt=("", "S1,20261007,2\n")) == ["2 trips running on different days"]  # holiday
    relabelled = {"trips_txt": (",S1,", ",S9,"), "calendar_txt": ("S1,1,1,1,1,1,0,0", "S9,1,1,1,1,1,0,0")}
    assert diff(**relabelled) == [], diff(**relabelled)                # NTA relabels service_ids: not a change
    assert diff(trips_txt=("1 39A b a,S1,T1,UCD,0,B7,SH1\n", "")) == ["1 trips removed that were still due to run"]
    assert diff(stops_txt=("53.3500,-6.2600", "53.3501,-6.2600")) == ["1 new or changed stops"]
    assert diff(trips_txt=("RX,S2,TX,Cork,0,B9,SHX\n", "RX,S2,TX,Cork,0,B9,SHY\n")) == []   # other operator


def test_minor():
    base = fingerprint(zipped(BASE))
    new_trip = fingerprint(zipped(republished_files(trips_txt=("", "1 39A b a,S1,T9,UCD,0,B7,SH1\n"))))
    changes = compare(base, new_trip)
    assert not is_minor(changes, len(new_trip["trips"]))           # 1 of 3 trips = 33%: load
    assert is_minor(changes, 1000)                                   # 1 of 1,000 trips: skip
    moved = fingerprint(zipped(republished_files(stops_txt=("53.3500,-6.2600", "53.3501,-6.2600"))))
    assert not is_minor(compare(base, moved), 1000)                  # a moved stop always loads


if __name__ == "__main__":
    test_minor()
    test_all()
    print("ok")
