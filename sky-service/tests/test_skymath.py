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
