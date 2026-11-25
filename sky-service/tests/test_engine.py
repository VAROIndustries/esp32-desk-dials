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


def _el_of(sat, unix, lat, lon):
    """Independent azimuth/elevation of one satellite at one time."""
    jd = unix_to_jd(unix)
    _, r, _ = sat.sgp4(np.floor(jd), jd - np.floor(jd))
    az, el, _ = azel(np.array(r)[None, :], np.array([jd]), lat, lon)
    return float(az[0]), float(el[0])


def _iss_el(unix, lat, lon):
    return _el_of(Satrec.twoline2rv(ISS_L1, ISS_L2), unix, lat, lon)[1]


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
    elems = E.parse_omm_json(json.dumps(rows).encode())
    true_el = {e.name: _el_of(e.sat, T0, round(lat, 1), round(lon, 1))[1] for e in elems}
    highest = set(sorted(true_el, key=true_el.get, reverse=True)[:MAX_OBJECTS])
    assert {o["n"] for o in out["objects"]} == highest


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
    assert "GOOD" in names and "DECAYED" not in names
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


# ---- review round 1 -------------------------------------------------------------------

import threading
import time

import engine as EN


def _elems(rows):
    return E.parse_omm_json(json.dumps(rows).encode())


def _multi(screens, now=T0):
    """screens: name -> rows. Ticked once at now."""
    eng = Engine()
    built = {n: (_elems(r), [""] * len(r)) for n, r in screens.items()}
    eng.set_screens(built, {n: now for n in screens})
    eng.tick(now)
    return eng


def test_azimuth_rounding_never_gives_360():
    assert EN._az(359.96) == 0.0
    assert EN._az(359.94) == 359.9
    assert EN._az(0.04) == 0.0 and EN._az(180.25) == 180.2


def test_cached_next_pass_is_not_served_after_it_has_risen():
    # reviewer's case: ISS from lat 42.0, lon -71.3, cutoff 40 is a short pass
    first = _engine([omm(25544, "ISS")]).query("nasa", 42.0, -71.3, 40)["next"]
    ta = T0 + first["in_s"] - 60
    eng = _engine([omm(25544, "ISS")])
    eng.tick(ta)
    before = eng.query("nasa", 42.0, -71.3, 40)
    assert before["count"] == 0 and 0 < before["next"]["in_s"] <= 60
    eng.tick(ta + 240)                                   # the pass has risen and set again
    after = eng.query("nasa", 42.0, -71.3, 40)
    assert after["count"] == 0
    nxt = after["next"]
    assert nxt["in_s"] > 0
    assert _iss_el(ta + 240 + nxt["in_s"], 42.0, -71.3) >= 39.0


def test_query_many_returns_one_tick_time_for_several_screens():
    lat, lon = _subpoint(T0)
    lat, lon = round(lat, 1), round(lon, 1)
    eng = Engine()
    assert eng.query_many(lat, lon, {"nasa": 60}) == (0.0, {"nasa": {"unavailable": True}})
    eng = _multi({"nasa": [omm(25544, "ISS")], "starlink": [omm(1, "S1")]})
    t0, res = eng.query_many(lat, lon, {"nasa": 60, "starlink": 60, "spy": 20})
    assert t0 == T0
    assert res["nasa"]["count"] == 1 and res["starlink"]["count"] == 1
    assert res["spy"] == {"unavailable": True}
    assert res["nasa"] == eng.query("nasa", lat, lon, 60)


def test_result_computed_from_an_old_tick_is_never_cached():
    lat, lon = _subpoint(T0)
    lat, lon = round(lat, 1), round(lon, 1)
    eng = _engine([omm(25544, "ISS")])
    plan = eng._begin(lat, lon, {"nasa": 30})          # snapshot taken at tick T0
    eng.tick(T0 + 5)                                    # a tick lands while it computes
    t0, res = eng._finish(plan)
    assert t0 == T0                                     # the answer is labelled with its own tick
    assert res["nasa"] == _engine([omm(25544, "ISS")]).query("nasa", lat, lon, 30)
    assert not eng.is_cached("nasa", lat, lon, 30)
    fresh = _engine([omm(25544, "ISS")])
    fresh.tick(T0 + 5)
    assert eng.query_many(lat, lon, {"nasa": 30}) == (T0 + 5, {"nasa": fresh.query("nasa", lat, lon, 30)})


def test_pass_computed_from_old_elements_is_never_served():
    old_rows, new_rows = [omm(25544, "OLD")], [omm(25544, "NEW", ma=265.6)]
    eng = _engine(old_rows)
    plan = eng._begin(42.0, -71.3, {"nasa": 20})        # nothing overhead: needs a pass search
    eng.set_screens({"nasa": (_elems(new_rows), [""])}, {"nasa": T0 - 7200})
    eng.tick(T0)
    _, res = eng._finish(plan)
    assert res["nasa"]["next"]["n"] == "OLD"            # the in-flight caller gets its own answer
    assert ("nasa", 42.0, -71.3, 20) not in eng._pcache  # ...but it is not kept
    assert eng.query("nasa", 42.0, -71.3, 20) == _engine(new_rows).query("nasa", 42.0, -71.3, 20)


def test_threads_hammering_query_many_get_tracks_consistent_with_t0():
    lat, lon = _subpoint(T0)
    lat, lon = round(lat, 1), round(lon, 1)
    rows = [omm(10 + i, f"S{i}", ma=85.6398 + (i - 2) * 0.3) for i in range(5)]
    eng = _engine(rows)
    got, done = [], threading.Event()

    def ticker():
        for k in range(1, 30):
            eng.tick(T0 + 5 * k)
            seen, deadline = len(got), time.monotonic() + 1.0
            while len(got) < seen + 4 and time.monotonic() < deadline:
                time.sleep(0.0005)                  # let the askers in between ticks
        done.set()

    def asker():
        while not done.is_set():
            t0, res = eng.query_many(lat, lon, {"nasa": 0})
            got.append((t0, res["nasa"]))

    threads = [threading.Thread(target=asker) for _ in range(4)] + [threading.Thread(target=ticker)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len({t0 for t0, _ in got}) > 10
    ref = {}
    for t0, out in {(t0, id(out)): (t0, out) for t0, out in got}.values():   # each distinct answer
        if t0 not in ref:
            e = _engine(rows)
            e.tick(t0)
            ref[t0] = e.query("nasa", lat, lon, 0)
        assert out["count"] == ref[t0]["count"] and out["objects"] == ref[t0]["objects"]
        assert all(0 <= az < 360 for o in out["objects"] for az, _ in o["trk"])


def test_tick_at_unix_zero_counts_as_a_tick():
    eng = Engine()
    eng.tick(0.0)
    assert eng.tick_age(5.0) == 5.0


def test_has_and_is_cached():
    lat, lon = _subpoint(T0)
    lat, lon = round(lat, 1), round(lon, 1)
    eng = Engine()
    eng.set_screens({"nasa": (_elems([omm(25544, "ISS")]), [""])}, {"nasa": T0})
    assert not eng.has("nasa") and not eng.has("spy")
    eng.tick(T0)
    assert eng.has("nasa") and not eng.has("spy")
    assert not eng.is_cached("nasa", lat, lon, 30)
    eng.query("nasa", lat, lon, 30)
    assert eng.is_cached("nasa", lat, lon, 30) and not eng.is_cached("nasa", lat, lon, 31)
    eng.tick(T0 + 5)
    assert not eng.is_cached("nasa", lat, lon, 30)


def test_nothing_rising_within_the_horizon_gives_no_next():
    out = _engine([omm(25544, "ISS")]).query("nasa", 80.0, 0.0, 20)   # ISS never gets that far north
    assert out["count"] == 0 and out["next"] is None and "next_busy" not in out


def test_pass_still_in_progress_at_the_end_of_the_horizon():
    # starlink-length horizon (20 min); the ISS is overhead 21 min from now, so it is still
    # climbing when the search window ends
    lat, lon = _subpoint(T0 + 1260)
    lat, lon = round(lat, 1), round(lon, 1)
    out = _multi({"starlink": [omm(25544, "ISS")]}).query("starlink", lat, lon, 30)
    assert out["count"] == 0
    nxt = out["next"]
    assert nxt["n"] == "ISS" and 0 < nxt["in_s"] <= EN.SCREENS["starlink"]["horizon_s"]
    end_el = _iss_el(T0 + 1200, lat, lon)
    assert nxt["max_el"] == int(round(end_el)) and end_el < _iss_el(T0 + 1230, lat, lon)


def test_lookahead_table_shape_steps_and_refresh():
    eng = _multi({"nasa": [omm(25544, "ISS")], "starlink": [omm(1, "S1"), omm(2, "S2")]})
    nasa, star = eng._screens["nasa"].table, eng._screens["starlink"].table
    assert nasa.r.dtype == np.float32 and star.r.dtype == np.float32
    assert star.step == 30 and nasa.step == 60
    assert star.r.shape[:2] == (2, (20 * 60 + EN.LOOKAHEAD_REFRESH_S) // 30)
    assert star.t[-1] - T0 >= 20 * 60 + EN.LOOKAHEAD_REFRESH_S
    eng.tick(T0 + EN.LOOKAHEAD_REFRESH_S)
    assert eng._screens["starlink"].table is star                    # not yet older than the limit
    eng.tick(T0 + EN.LOOKAHEAD_REFRESH_S + 5)
    assert eng._screens["starlink"].table is not star
    assert eng._screens["starlink"].table.built == T0 + EN.LOOKAHEAD_REFRESH_S + 5
    eng.set_screens({"nasa": (_elems([omm(25544, "ISS")]), [""])}, {"nasa": T0})
    assert eng._staged["nasa"].table is None                   # a new element set starts without one


class _NoPropagation:
    def __init__(self, arr):
        self.arr, self.calls = arr, 0

    def sgp4(self, *a):
        self.calls += 1
        return self.arr.sgp4(*a)


def test_request_time_pass_search_does_no_propagation():
    eng = _multi({"nasa": [omm(25544, "ISS")], "starlink": [omm(1, "S1")]})
    spies = {}
    for name, sc in eng._screens.items():
        spies[name] = sc.arr = _NoPropagation(sc.arr)
    out = eng.query("nasa", 42.0, -71.3, 20)
    assert out["next"]["n"] == "ISS"
    eng.query_many(42.1, -71.3, {"nasa": 20, "starlink": 60})
    assert all(s.calls == 0 for s in spies.values())


def test_pass_search_slots_exhausted_gives_next_busy():
    eng = _engine([omm(25544, "ISS")])
    for _ in range(EN.PASS_SEARCH_SLOTS):
        assert eng._slots.acquire(blocking=False)
    out = eng.query("nasa", 42.0, -71.3, 20)
    assert out["count"] == 0 and out["next"] is None and out["next_busy"] is True
    assert not eng.is_cached("nasa", 42.0, -71.3, 20)            # a busy answer is not kept
    for _ in range(EN.PASS_SEARCH_SLOTS):
        eng._slots.release()
    out = eng.query("nasa", 42.0, -71.3, 20)
    assert out["next"]["n"] == "ISS" and "next_busy" not in out


def test_screen_without_lookahead_table_gives_next_busy():
    eng = _engine([omm(25544, "ISS")])
    eng._screens["nasa"].table = None
    out = eng.query("nasa", 42.0, -71.3, 20)
    assert out["next"] is None and out["next_busy"] is True


def test_pass_search_skips_objects_with_propagation_errors():
    rows = [omm(1, "A"), omm(2, "B", ma=205.6)]
    first = _engine(rows).query("nasa", 42.0, -71.3, 20)["next"]
    named = first["n"]
    other = "B" if named == "A" else "A"
    eng = _engine(rows)                                   # error at the step it would rise at
    sc = eng._screens["nasa"]
    sc.table.bad[sc.names.index(named), first["in_s"] // sc.table.step - 1] = True
    assert eng.query("nasa", 42.0, -71.3, 20)["next"]["n"] == other
    eng = _engine(rows)                                   # error in the current track
    sc = eng._screens["nasa"]
    sc.ok[sc.names.index(named)] = False
    assert eng.query("nasa", 42.0, -71.3, 20)["next"]["n"] == other


def test_cached_no_pass_is_searched_again_once_the_horizon_moves_on():
    # ISS rises (cut 60) at T0 + 1230 s: just past a 20-minute horizon at T0, inside it at T0 + 60
    lat, lon = _subpoint(T0 + 1260)
    lat, lon = round(lat, 1), round(lon, 1)
    eng = _multi({"starlink": [omm(25544, "ISS")]})
    assert eng.query("starlink", lat, lon, 60)["next"] is None
    eng.tick(T0 + 5)                                    # window still ends at the same step
    assert eng.query("starlink", lat, lon, 60)["next"] is None
    eng.tick(T0 + 60)
    nxt = eng.query("starlink", lat, lon, 60)["next"]
    assert nxt["n"] == "ISS" and nxt["in_s"] == 1170


def test_per_tick_result_cache_has_its_own_small_bound():
    import engine as EN
    assert EN.QCACHE_MAX == 256 and EN.CACHE_MAX == 5000
    eng = _engine([omm(25544, "ISS")])
    lat, lon = (round(v, 1) for v in _subpoint(T0))
    seen = 0
    for i in range(20):                      # 300 distinct squares near the ISS, all with it overhead
        for j in range(15):
            out = eng.query("nasa", round(lat + (i - 10) * 0.1, 1), round(lon + (j - 7) * 0.1, 1), 0)
            assert out["count"] == 1
            seen += 1
            assert len(eng._qcache) <= EN.QCACHE_MAX
    assert seen == 300 and len(eng._qcache) == EN.QCACHE_MAX
    assert eng.is_cached("nasa", round(lat + 9 * 0.1, 1), round(lon + 7 * 0.1, 1), 0)    # newest kept
    assert not eng.is_cached("nasa", round(lat - 10 * 0.1, 1), round(lon - 7 * 0.1, 1), 0)   # oldest evicted


def test_new_screens_take_over_only_once_a_tick_has_tracked_them():
    lat, lon = _subpoint(T0)
    lat, lon = round(lat, 1), round(lon, 1)
    eng = _engine([omm(25544, "ISS")])
    before = eng.query("nasa", lat, lon, 60)
    eng.set_screens({"nasa": (_elems([omm(25544, "NEW")]), [""])}, {"nasa": T0})
    assert eng.query("nasa", lat, lon, 60) == before                 # still answering while it rebuilds
    eng.tick(T0 + 5)
    assert eng.query("nasa", lat, lon, 60)["objects"][0]["n"] == "NEW"


def test_pass_cache_does_not_keep_old_lookahead_tables_alive():
    import gc
    import weakref
    eng = _engine([omm(25544, "ISS")])
    refs = []
    for i in range(3):
        eng.tick(T0 + i * (EN.LOOKAHEAD_REFRESH_S + 5))
        refs.append(weakref.ref(eng._screens["nasa"].table))
        assert eng.query("nasa", 42.0 + i, -71.3, 20)["next"] is not None   # stores a pass search
    gc.collect()
    assert [r() is not None for r in refs] == [False, False, True]
