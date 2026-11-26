import time
from datetime import datetime, timezone

import pytest

import app as A
import planets as P
from conftest import de421_path

PATH, WHY = de421_path()
need_de421 = pytest.mark.skipif(PATH is None, reason=WHY or "")
SEGMENTS = {"MOON": "moon", "MERCURY": "mercury", "VENUS": "venus", "MARS": "mars",
            "JUPITER": "jupiter barycenter", "SATURN": "saturn barycenter"}
# (lat, lon, unix): the Moon is up at all three; between them all six bodies are up somewhere
CASES = [(42.0, -71.3, 1795667160.0), (64.8, -147.7, 1767225600.0), (-33.9, 151.2, 1780032400.0)]
ALL_DOWN = (42.0, -71.3, 1771754400.0)      # 2026-02-22 10:00 UTC: all six below the horizon


@pytest.fixture(scope="module")
def sky():
    """Skyfield used directly: the oracle (no cache, its own time conversion)."""
    from skyfield.api import load_file, load
    eph = load_file(PATH)
    yield eph, load.timescale(builtin=True)
    eph.close()


@pytest.fixture
def new():
    """Planets instances, closed after the test."""
    made = []

    def make(path=PATH, **kw):
        made.append(P.Planets(path, **kw))
        return made[-1]
    yield make
    for pl in made:
        pl.close()


def _oracle(sky, lat, lon, unix, name):
    from skyfield.api import wgs84
    eph, ts = sky
    t = ts.from_datetime(datetime.fromtimestamp(unix, timezone.utc))
    alt, az, _ = (eph["earth"] + wgs84.latlon(lat, lon)).at(t).observe(eph[SEGMENTS[name]]).apparent().altaz()
    return az.degrees, alt.degrees


def _daz(a, b):
    return abs((a - b + 180) % 360 - 180)


@need_de421
@pytest.mark.parametrize("lat,lon,t0", CASES)
def test_positions_match_skyfield(sky, new, lat, lon, t0):
    out = new().query(lat, lon, 0, t0)
    names = [o["n"] for o in out["objects"]]
    up = [n for n in SEGMENTS if _oracle(sky, lat, lon, t0, n)[1] >= 0]
    assert "MOON" in names and sorted(names) == sorted(up) and out["count"] == len(up)
    assert out["age_h"] == 0 and out["next"] is None
    assert [o["trk"][0][1] for o in out["objects"]] == sorted((o["trk"][0][1] for o in out["objects"]), reverse=True)
    for o in out["objects"]:
        assert o["tag"] == ("MOON" if o["n"] == "MOON" else "PLANET") and len(o["trk"]) == 13
        for k in (0, 6, 12):
            az, el = _oracle(sky, lat, lon, t0 + 5 * k, o["n"])
            assert _daz(o["trk"][k][0], az) < 0.1 and abs(o["trk"][k][1] - el) < 0.1


@need_de421
def test_cut_is_honoured(new):
    lat, lon, t0 = CASES[0]                     # Moon 65, Saturn 28, Jupiter 10, Mars 7
    out = new().query(lat, lon, 20, t0)
    assert [o["n"] for o in out["objects"]] == ["MOON", "SATURN"] and out["count"] == 2
    assert all(o["trk"][0][1] >= 20 for o in out["objects"])


@need_de421
def test_next_rise_when_nothing_is_up(sky, new):
    from skyfield import almanac
    from skyfield.api import wgs84
    lat, lon, t0 = ALL_DOWN
    pl = new()
    out = pl.query(lat, lon, 0, t0)
    assert out["count"] == 0 and out["objects"] == []
    eph, ts = sky
    here = eph["earth"] + wgs84.latlon(lat, lon)
    start = ts.from_datetime(datetime.fromtimestamp(t0, timezone.utc))
    best = None
    for n, seg in SEGMENTS.items():
        t, y = almanac.find_risings(here, eph[seg], start, start + 1.0, horizon_degrees=0.0)
        t = t[y]
        if len(t) and (best is None or t[0].tt < best[1].tt):
            best = (n, t[0])
    in_s = best[1].utc_datetime().timestamp() - t0
    az = here.at(best[1]).observe(eph[SEGMENTS[best[0]]]).apparent().altaz()[1].degrees
    nxt = out["next"]
    assert nxt["n"] == best[0] and abs(nxt["in_s"] - in_s) <= 2 and nxt["max_el"] == 0
    assert nxt["from"] == P.compass(az)
    # the rise is reused for 10 minutes: a later tick counts down the same rise
    later = pl.query(lat, lon, 0, t0 + 60)
    assert later["next"]["n"] == nxt["n"] and later["next"]["in_s"] == nxt["in_s"] - 60


def test_missing_ephemeris_is_unavailable(new, tmp_path):
    logged = []
    pl = new(str(tmp_path / "none.bsp"), log=logged.append)
    assert pl.query(42.0, -71.3, 0, 1795631160.0) == {"unavailable": True}
    assert len(logged) == 1 and not list(tmp_path.iterdir())          # nothing downloaded


@need_de421
def test_uncached_query_time(new):
    pl = new()
    pl.available()                               # loading the file is a start-up cost
    lat, lon, t0 = ALL_DOWN                      # the worst case: the rise search runs too
    s = time.perf_counter()
    pl.query(lat, lon, 0, t0)
    worst = time.perf_counter() - s
    s = time.perf_counter()
    pl.query(*CASES[0][:2], 0, CASES[0][2])
    usual = time.perf_counter() - s
    print(f"\nplanets uncached: {usual * 1000:.1f} ms with something up, {worst * 1000:.1f} ms with the rise search")
    assert worst < 0.5 and usual < 0.5


@need_de421
def test_planets_mix_with_a_satellite_screen_and_share_its_tick(new, tmp_path):
    from test_app import T0, _app
    app, here, _ = _app(tmp_path, {"PLANETS_EPHEMERIS": PATH})
    try:
        status, body, _ = app.handle(f"/v1/sky?{here}&screens=starlink,planets,nasa&cut=starlink:30", {}, "1.1.1.1")
        assert status == 200 and body["lag_ms"] == 0 and body["screens"]["starlink"]["count"] == 80
        lat, lon = (float(v.split("=")[1]) for v in here.split("&"))
        assert body["screens"]["planets"] == new().query(lat, lon, 0, T0)       # tracks start at the tick
        assert body["screens"]["planets"]["count"] > 0 and "nasa" in body["screens"]
    finally:
        app.planets.close()


@need_de421
def test_uncached_planets_count_toward_the_calculation_ceiling(tmp_path):
    from test_app import _app
    app, here, _ = _app(tmp_path, {"PLANETS_EPHEMERIS": PATH, "CALC_PER_SEC": "1", "RATE_PER_MIN": "100"})
    try:
        assert app.handle(f"/v1/sky?{here}&screens=starlink,planets", {}, "1.1.1.1")[0] == 200
        assert app.handle(f"/v1/sky?{here}&screens=starlink,planets", {}, "1.1.1.1")[0] == 200   # all cached
        assert app.handle(f"/v1/sky?{here}&screens=planets&cut=planets:5", {}, "1.1.1.1")[0] == 503
    finally:
        app.planets.close()
