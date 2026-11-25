import json

import numpy as np
from sgp4 import omm as sgp4_omm
from sgp4.api import Satrec

import elements as E
from conftest import ISS_L1, ISS_L2, T0, omm
from engine import MAX_OBJECTS, OLD_EPOCH_S, TRACK_POINTS, Engine
from skymath import azel, gmst, unix_to_jd


def _subpoint(unix):
    """Rough latitude/longitude directly under the ISS at a time (good to a fraction of a degree)."""
    jd = unix_to_jd(unix)
    _, r, _ = Satrec.twoline2rv(ISS_L1, ISS_L2).sgp4(np.floor(jd), jd - np.floor(jd))
    th = float(gmst(jd))
    x = np.cos(th) * r[0] + np.sin(th) * r[1]
    y = -np.sin(th) * r[0] + np.cos(th) * r[1]
    return float(np.rad2deg(np.arctan2(r[2], np.hypot(x, y)))), float(np.rad2deg(np.arctan2(y, x)))


def _engine(rows, tags=None, fetched=T0 - 7200):
    elems = E.parse_omm_json(json.dumps(rows).encode())
    eng = Engine()
    eng.set_screens({"nasa": (elems, tags or [""] * len(elems))}, {"nasa": fetched})
    eng.tick(T0)
    return eng


def test_object_overhead_is_returned_with_a_full_track():
    lat, lon = _subpoint(T0)
    out = _engine([omm(25544, "ISS")]).query("nasa", round(lat, 1), round(lon, 1), 60)
    assert out["count"] == 1 and out["next"] is None and out["age_h"] == 2.0
    obj = out["objects"][0]
    assert obj["n"] == "ISS" and len(obj["trk"]) == TRACK_POINTS
    assert obj["trk"][0][1] > 80 and all(0 <= az < 360 for az, _ in obj["trk"])


def test_nothing_overhead_gives_next_pass_that_really_happens():
    out = _engine([omm(25544, "ISS")]).query("nasa", 42.0, -71.3, 20)
    assert out["count"] == 0 and out["objects"] == []
    nxt = out["next"]
    assert nxt["n"] == "ISS" and 0 < nxt["in_s"] <= 24 * 3600 and nxt["max_el"] >= 20
    assert nxt["from"] in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    when = T0 + nxt["in_s"]
    jd = unix_to_jd(when)
    _, r, _ = Satrec.twoline2rv(ISS_L1, ISS_L2).sgp4(np.floor(jd), jd - np.floor(jd))
    _, el, _ = azel(np.array(r)[None, :], np.array([jd]), 42.0, -71.3)
    assert float(el[0]) >= 19.0                    # 30 s search step, integer seconds
    jd = unix_to_jd(when - 60)                     # and it was below the cutoff a minute earlier
    _, r, _ = Satrec.twoline2rv(ISS_L1, ISS_L2).sgp4(np.floor(jd), jd - np.floor(jd))
    _, el, _ = azel(np.array(r)[None, :], np.array([jd]), 42.0, -71.3)
    assert float(el[0]) < 20.0


def test_cap_keeps_the_highest_and_reports_true_count():
    lat, lon = _subpoint(T0)
    rows = [omm(1000 + i, f"S{i}", ma=85.6398 + (i - 40) * 0.01) for i in range(80)]
    out = _engine(rows).query("nasa", round(lat, 1), round(lon, 1), 30)
    assert out["count"] == 80 and len(out["objects"]) == MAX_OBJECTS
    els = [o["trk"][0][1] for o in out["objects"]]
    assert els == sorted(els, reverse=True)


def test_tags_follow_objects_and_unknown_screen_is_unavailable():
    lat, lon = _subpoint(T0)
    eng = _engine([omm(1, "A"), omm(2, "B", ma=85.7)], tags=["HOBBY", "MIL"])
    got = {o["n"]: o["tag"] for o in eng.query("nasa", round(lat, 1), round(lon, 1), 30)["objects"]}
    assert got == {"A": "HOBBY", "B": "MIL"}
    assert eng.query("starlink", 0.0, 0.0, 60) == {"unavailable": True}


def test_object_that_fails_to_propagate_is_dropped_not_fatal():
    lat, lon = _subpoint(T0)
    good = E.parse_omm_json(json.dumps([omm(1, "GOOD")]).encode())[0]
    bad_sat = Satrec()
    sgp4_omm.initialize(bad_sat, omm(2, "DECAYED", MEAN_MOTION=16.9, BSTAR=0.9, MEAN_MOTION_DOT=0.5))
    bad = E.Elem("DECAYED", 2, bad_sat, "", T0)
    eng = Engine()
    eng.set_screens({"nasa": ([good, bad], ["", ""])}, {"nasa": T0})
    eng.tick(T0)
    out = eng.query("nasa", round(lat, 1), round(lon, 1), 0)
    names = [o["n"] for o in out["objects"]]
    assert "GOOD" in names
    assert all(np.isfinite(v) for o in out["objects"] for p in o["trk"] for v in p)


def test_objects_measured_long_ago_are_marked_old():
    lat, lon = _subpoint(T0)
    elems = E.parse_omm_json(json.dumps([omm(1, "FRESH"), omm(2, "STALE", ma=85.7)]).encode())
    elems[1] = type(elems[1])("STALE", 2, elems[1].sat, "", T0 - OLD_EPOCH_S - 60)
    eng = Engine()
    eng.set_screens({"nasa": (elems, ["", ""])}, {"nasa": T0})
    eng.tick(T0)
    got = {o["n"]: o.get("old", False) for o in eng.query("nasa", round(lat, 1), round(lon, 1), 30)["objects"]}
    assert got == {"FRESH": False, "STALE": True}


def test_tick_age():
    eng = Engine()
    assert eng.tick_age(T0) == float("inf")
    eng.tick(T0)
    assert eng.tick_age(T0 + 7) == 7


def test_unknown_fetch_time_reports_minus_one():
    lat, lon = _subpoint(T0)
    out = _engine([omm(25544, "ISS")], fetched=0.0).query("nasa", round(lat, 1), round(lon, 1), 60)
    assert out["age_h"] == -1.0
