"""Observer geometry for the sky service: TEME positions -> azimuth / elevation / range.

Pure numpy, no I/O. Checked against Skyfield in tests/test_skymath.py.
"""
from __future__ import annotations

import numpy as np

_A = 6378.137                    # WGS84 equatorial radius, km
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


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
