import numpy as np
import pytest
from sgp4.api import Satrec

from conftest import ISS_L1, ISS_L2, T0
from skymath import azel, compass, unix_to_jd

# Reference values computed with Skyfield 1.55 for the ISS element set in conftest,
# observer 42.0 N, 71.3 W, sea level: (seconds after T0, azimuth, elevation, range km).
SKYFIELD = [(0, 61.455, -61.422, 11682.42), (1200, 233.277, -80.075, 12981.01), (2700, 250.844, -31.638, 7417.51)]


def _iss_r(unix):
    jd = unix_to_jd(unix)
    err, r, _ = Satrec.twoline2rv(ISS_L1, ISS_L2).sgp4(np.floor(jd), jd - np.floor(jd))
    assert err == 0
    return np.array(r)


@pytest.mark.parametrize("dt,az,el,rng", SKYFIELD)
def test_azel_matches_skyfield_reference(dt, az, el, rng):
    got_az, got_el, got_rng = azel(_iss_r(T0 + dt)[None, :], np.array([unix_to_jd(T0 + dt)]), 42.0, -71.3)
    assert abs(float(got_az[0]) - az) < 0.01
    assert abs(float(got_el[0]) - el) < 0.01
    assert abs(float(got_rng[0]) - rng) < 0.2


def test_azel_matches_live_skyfield():
    import skyfield.api as sf
    ts = sf.load.timescale(builtin=True)
    sat = sf.EarthSatellite(ISS_L1, ISS_L2, "ISS", ts)
    for dt, lat, lon in [(300, -33.9, 151.2), (4000, 64.1, -21.9), (9000, 0.0, 0.0)]:
        t = ts.utc(2019, 12, 9, 17, 0, dt)
        alt, az, _ = (sat - sf.wgs84.latlon(lat, lon)).at(t).altaz()
        got_az, got_el, _ = azel(_iss_r(T0 + dt)[None, :], np.array([unix_to_jd(T0 + dt)]), lat, lon)
        assert abs(float(got_el[0]) - alt.degrees) < 0.01
        assert abs(((float(got_az[0]) - az.degrees + 180) % 360) - 180) < 0.02


def test_azel_shapes_for_many_objects_and_times():
    r = np.stack([np.stack([_iss_r(T0 + 5 * k) for k in range(13)])] * 4)      # (4, 13, 3)
    az, el, rng = azel(r, unix_to_jd(T0 + 5 * np.arange(13)), 42.0, -71.3)
    assert az.shape == el.shape == rng.shape == (4, 13)


@pytest.mark.parametrize("az,name", [(0, "N"), (22.4, "N"), (22.6, "NE"), (90, "E"), (225, "SW"), (337.4, "NW"), (359, "N")])
def test_compass(az, name):
    assert compass(az) == name


# --- the Sun: recorded with Skyfield 1.55 and JPL DE421 (the ephemeris is not shipped with the tests).
# (unix time, apparent geocentric Sun in Skyfield's TEME frame in km, Sun elevation in degrees at 42.0 N 71.3 W)
SKYFIELD_SUN = [
    (1768453200, 62293334.1, -122322466.4, -53030092.5, -69.0911),
    (1771149600, 123538615.8, -74391830.1, -32249847.5, -19.3634),
    (1773586800, 148221414.3, -11823549.1, -5124235.5, 39.0183),
    (1776283200, 135076302.8, 60058488.4, 26038934.7, 36.7921),
    (1778806800, 88384400.7, 112564584.6, 48801247.1, -10.5836),
    (1781503200, 15429814.0, 138692929.6, 60127427.0, -22.4668),
    (1784113200, -59387460.3, 128431455.9, 55677495.2, 15.7531),
    (1786809600, -120757938.4, 83951169.0, 36392871.5, 59.9086),
    (1789506000, -149321528.8, 16733441.8, 7251561.5, 20.2888),
    (1792029600, -138590133.1, -50697022.6, -21980998.2, -42.8157),
    (1794726000, -89222126.8, -108326454.7, -46963451.9, -50.4444),
    (1797336000, -16625662.1, -134235320.4, -58193710.5, -1.8286),
]
# ISS (conftest elements) at T0 + s: sunlit per Skyfield's is_sunlit(DE421); none within 30 s of shadow entry/exit.
SKYFIELD_SUNLIT = [(0, False), (4320, True), (8640, True), (12960, True), (17280, False), (21600, False),
                   (25920, True), (30240, True), (34560, True), (38880, False), (43200, True), (47520, True),
                   (51840, True), (56160, False), (61080, False), (64800, True), (69120, True), (73440, True),
                   (77760, False), (82080, True)]


def test_sun_direction_and_elevation_match_skyfield_over_2026():
    from skymath import sun_elevation, sun_teme
    for unix, x, y, z, el in SKYFIELD_SUN:
        jd = unix_to_jd(unix)
        got, want = sun_teme(jd), np.array([x, y, z])
        cos = got @ want / (np.linalg.norm(got) * np.linalg.norm(want))
        assert np.rad2deg(np.arccos(min(1.0, cos))) < 0.02
        assert abs(float(sun_elevation(jd, 42.0, -71.3)) - el) < 0.05
    jds = unix_to_jd(np.array([u for u, *_ in SKYFIELD_SUN], dtype=float))
    assert sun_teme(jds).shape == (12, 3) and sun_elevation(jds, 42.0, -71.3).shape == (12,)


def test_sunlit_matches_skyfield_for_the_iss_across_a_day():
    from skymath import sun_teme, sunlit
    for dt, want in SKYFIELD_SUNLIT:
        assert bool(sunlit(_iss_r(T0 + dt), sun_teme(unix_to_jd(T0 + dt)))) is want, dt
