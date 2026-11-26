import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ISS element set from the sgp4 library documentation (epoch 2019-12-09).
ISS_L1 = "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991"
ISS_L2 = "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482"
T0 = 1575910800.0        # 2019-12-09 17:00:00 UTC

ISS_OMM = {
    "OBJECT_NAME": "ISS (ZARYA)", "OBJECT_ID": "1998-067A", "EPOCH": "2019-12-09T16:38:29.363424",
    "MEAN_MOTION": 15.50103472, "ECCENTRICITY": 0.0007417, "INCLINATION": 51.6439,
    "RA_OF_ASC_NODE": 211.2001, "ARG_OF_PERICENTER": 17.6667, "MEAN_ANOMALY": 85.6398,
    "EPHEMERIS_TYPE": 0, "CLASSIFICATION_TYPE": "U", "NORAD_CAT_ID": 25544, "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 20248, "BSTAR": 0.38792e-4, "MEAN_MOTION_DOT": 0.00001764, "MEAN_MOTION_DDOT": 0,
}


def omm(satnum, name="SAT", raan=211.2, ma=85.6, **over):
    row = dict(ISS_OMM, NORAD_CAT_ID=satnum, OBJECT_NAME=name, RA_OF_ASC_NODE=raan, MEAN_ANOMALY=ma)
    row.update(over)
    return row


# JPL DE421 for the planets tests: $PLANETS_EPHEMERIS if that file exists, else a copy in the
# user's cache folder, downloaded once (same URL and SHA-256 as the Dockerfile).
DE421_URL = "https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp"
DE421_SHA256 = "a20a7139da04cbc462454634918e9a9ca69127044e2cc9d4f9c16e238d2deedc"


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def de421_path():
    """-> (path, None) or (None, reason the file could not be obtained)."""
    given = os.environ.get("PLANETS_EPHEMERIS")
    if given and os.path.isfile(given):
        return given, None
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "esp32-desk-dials")
    path = os.path.join(cache, "de421.bsp")
    if os.path.isfile(path) and _sha256(path) == DE421_SHA256:
        return path, None
    try:
        import requests
        os.makedirs(cache, exist_ok=True)
        tmp = path + ".part"
        with requests.get(DE421_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if _sha256(tmp) != DE421_SHA256:
            os.remove(tmp)
            return None, "downloaded de421.bsp does not match the pinned SHA-256"
        os.replace(tmp, path)
        return path, None
    except Exception as e:
        return None, f"de421.bsp not available (set PLANETS_EPHEMERIS or allow the download): {type(e).__name__}"
