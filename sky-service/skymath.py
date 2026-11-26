"""Observer geometry for the sky service: TEME positions -> azimuth / elevation / range,
and where the Sun is (for "is the observer in the dark" and "is the satellite sunlit").

Pure numpy, no I/O. Checked against Skyfield in tests/test_skymath.py.
"""
from __future__ import annotations

import numpy as np

_A = 6378.137                    # WGS84 equatorial radius, km
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_AU_KM = 149597870.7


def gmst(jd_ut1):
    """Greenwich mean sidereal time in radians (IAU 1982). Accepts a float or an array."""
    t = (np.asarray(jd_ut1, dtype=float) - 2451545.0) / 36525.0
    sec = 67310.54841 + (876600 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return np.deg2rad((sec % 86400.0) / 240.0)


def azel(r_teme, jd, lat_deg: float, lon_deg: float):
    """Topocentric azimuth (deg, 0 = north, clockwise), elevation (deg) and range (km).

    r_teme: array (..., T, 3) of TEME positions in km, as sgp4 returns them.
    jd:     array (T,) of full Julian dates (UT1 ~ UTC) matching the T axis.
    """
    r = np.asarray(r_teme, dtype=float)
    th = gmst(jd)
    c, s = np.cos(th), np.sin(th)
    x = c * r[..., 0] + s * r[..., 1]
    y = -s * r[..., 0] + c * r[..., 1]
    z = r[..., 2]
    la, lo = np.deg2rad(lat_deg), np.deg2rad(lon_deg)
    n = _A / np.sqrt(1 - _E2 * np.sin(la) ** 2)
    dx = x - n * np.cos(la) * np.cos(lo)
    dy = y - n * np.cos(la) * np.sin(lo)
    dz = z - n * (1 - _E2) * np.sin(la)
    east = -np.sin(lo) * dx + np.cos(lo) * dy
    north = -np.sin(la) * np.cos(lo) * dx - np.sin(la) * np.sin(lo) * dy + np.cos(la) * dz
    up = np.cos(la) * np.cos(lo) * dx + np.cos(la) * np.sin(lo) * dy + np.sin(la) * dz
    rng = np.sqrt(dx * dx + dy * dy + dz * dz)
    return np.rad2deg(np.arctan2(east, north)) % 360.0, np.rad2deg(np.arcsin(up / rng)), rng


def compass(az_deg: float) -> str:
    """Eight-point compass name for an azimuth in degrees."""
    return _COMPASS[int(((float(az_deg) % 360.0) + 22.5) // 45) % 8]


def unix_to_jd(unix_s: float) -> float:
    return unix_s / 86400.0 + 2440587.5


def sun_teme(jd):
    """Geocentric apparent Sun position in km, TEME-like frame (true equator, equinox of date).

    The Astronomical Almanac's low-precision formula (about 0.01 degree from 1950 to 2050).
    The ~0.005 degree between the true and mean equinox (TEME's x axis) is ignored.
    jd: a float or an array (T,) of Julian dates (UT). Returns (3,) or (T, 3).
    """
    n = np.asarray(jd, dtype=float) - 2451545.0
    L = np.deg2rad((280.460 + 0.9856474 * n) % 360.0)
    g = np.deg2rad((357.528 + 0.9856003 * n) % 360.0)
    lam = L + np.deg2rad(1.915) * np.sin(g) + np.deg2rad(0.020) * np.sin(2 * g)
    eps = np.deg2rad(23.439 - 0.0000004 * n)
    dist = (1.00014 - 0.01671 * np.cos(g) - 0.00014 * np.cos(2 * g)) * _AU_KM
    return np.stack([dist * np.cos(lam), dist * np.cos(eps) * np.sin(lam), dist * np.sin(eps) * np.sin(lam)], axis=-1)


def sun_elevation(jd, lat_deg: float, lon_deg: float):
    """Elevation of the Sun's centre in degrees (no refraction) for one or more Julian dates."""
    jd = np.asarray(jd, dtype=float)
    _, el, _ = azel(sun_teme(np.atleast_1d(jd)), np.atleast_1d(jd), lat_deg, lon_deg)
    return el.reshape(jd.shape)


def sunlit(r_teme, sun):
    """True where a satellite at r_teme (..., 3) km is in sunlight, sun (..., 3) km broadcasting.

    Earth's shadow is taken as a cylinder of the equatorial radius behind the Earth: enough to
    say whether a satellite can be seen with the eye. The penumbra (a few seconds of a pass)
    and the Earth's flattening are ignored.
    """
    r = np.asarray(r_teme, dtype=float)
    s = np.asarray(sun, dtype=float)
    s = s / np.linalg.norm(s, axis=-1, keepdims=True)
    along = np.sum(r * s, axis=-1)
    across = np.linalg.norm(r - along[..., None] * s, axis=-1)
    return (along >= 0) | (across > _A)
