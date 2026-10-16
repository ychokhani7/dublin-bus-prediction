"""Loads a tiny fake GTFS zip twice, then runs the nightly load/prune cycle, against the database at DATABASE_URL.
Run against your local docker db: DATABASE_URL=postgresql://bus:bus@localhost:5432/bus python test_load_static.py"""
import io
import os
import tempfile
from pathlib import Path
import zipfile

import psycopg

import load_static

FILES = {
    "agency.txt": "agency_id,agency_name\n1,Bus Átha Cliath – Dublin Bus\n2,Bus Éireann\n10000,LUAS\n",
    "feed_info.txt": "feed_publisher_name,feed_version,feed_start_date,feed_end_date\nNTA,ABC-123,20261002,20271002\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                  "1 39A b a,1,39A,Ongar - UCD,3\nRX,2,X,Elsewhere,3\nGRN,10000,Green,Luas Green,0\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id,block_id,shape_id\n"
                 "1 39A b a,S1,T1,UCD,0,B7,SH1\nRX,S2,TX,Cork,0,B9,SHX\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence,timepoint\n"
                      "T1,23:58:00,23:58:00,A,1,1\nT1,24:10:30,24:10:30,B,2,0\nTX,10:00:00,10:00:00,C,1,1\n",
    "stops.txt": "stop_id,stop_code,stop_name,stop_lat,stop_lon\nA,1358,Stop A,53.3438,-6.2546\nB,7581,Stop B,53.3500,-6.2600\nC,9,Stop C,52.0,-8.0\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled\n"
                  "SH1,53.3500,-6.2600,2,0.78\nSH1,53.3438,-6.2546,1,0\nSHX,52,-8,1,0\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "S1,1,1,1,1,1,0,0,20261001,20261231\nS2,1,1,1,1,1,1,1,20261001,20261231\n",
}

if __name__ == "__main__":
    assert load_static.secs("24:10:30") == 87030
    path = os.path.join(tempfile.mkdtemp(), "20261003_test.zip")
    with zipfile.ZipFile(path, "w") as z:
        for name, text in FILES.items():
            z.writestr(name, text)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        load_static.load(conn, path)
        load_static.load(conn, path)                       # second run must replace, not duplicate
        q = lambda sql: conn.execute(sql).fetchall()
        assert q("SELECT trip_id FROM trips JOIN static_versions v ON v.id = version_id WHERE v.name = '20261003_test'") == [("T1",)]
        assert q("SELECT stop_id, arrival_secs, timepoint FROM stop_times st JOIN static_versions v ON v.id = st.version_id "
                 "WHERE v.name = '20261003_test' ORDER BY stop_sequence") == [("A", 86280, 1), ("B", 87030, 0)]
        assert q("SELECT block_id FROM trips t JOIN static_versions v ON v.id = t.version_id "
                 "WHERE v.name = '20261003_test'") == [("B7",)]
        assert q("SELECT feed_version, feed_start_date::text FROM static_versions "
                 "WHERE name = '20261003_test'") == [("ABC-123", "2026-10-02")]
        length = q("SELECT ST_Length(geom) FROM shapes s JOIN static_versions v ON v.id = s.version_id WHERE v.name = '20261003_test'")[0][0]
        assert 700 < length < 900, length                  # ~780 m between the two points, in metres thanks to EPSG:2157
        conn.execute("DELETE FROM static_versions WHERE name = '20261003_test'")
        conn.commit()

        # Nightly cycle: three changed zips load, prune keeps the newest two, and a pruned zip is not reloaded.
        assert not q("SELECT 1 FROM static_versions"), "needs a database with no timetable versions: prune deletes them"
        load_static.DATA = Path(tempfile.mkdtemp())
        (load_static.DATA / "static").mkdir()
        for day in ("01", "02", "03"):            # each day starts later and adds one trip: a real change
            files = dict(FILES, **{
                "feed_info.txt": f"feed_publisher_name,feed_version,feed_start_date,feed_end_date\nNTA,V{day},202610{day},20271001\n",
                "trips.txt": FILES["trips.txt"] + f"1 39A b a,S1,T{day},UCD,0,B7,SH1\n"})
            with zipfile.ZipFile(load_static.DATA / "static" / f"202610{day}_cycle.zip", "w") as z:
                for name, text in files.items():
                    z.writestr(name, text)
        names = lambda: sorted(n for (n,) in q("SELECT name FROM static_versions"))
        load_static.load_new(conn)
        assert names() == ["20261001_cycle", "20261002_cycle", "20261003_cycle"], names()
        load_static.prune(conn)
        load_static.load_new(conn)                 # used to reload 20261001_cycle every night
        assert names() == ["20261002_cycle", "20261003_cycle"], names()
        load_static.load(conn, load_static.DATA / "static" / "20261001_cycle.zip")   # old zip reloaded by hand
        conn.commit()
        load_static.prune(conn)                    # must drop the old timetable, not the newest one
        assert names() == ["20261002_cycle", "20261003_cycle"], names()
        conn.execute("DELETE FROM static_versions WHERE name LIKE '%\\_cycle'")
        conn.execute("DELETE FROM static_skipped WHERE name LIKE '%\\_cycle'")
    print("ok")
