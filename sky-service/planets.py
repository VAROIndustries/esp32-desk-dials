"""Moon and planets screen ("planets"): Moon, Mercury, Venus, Mars, Jupiter and Saturn.

Positions come from Skyfield with the JPL DE421 ephemeris, read from a local file baked into
the image (PLANETS_EPHEMERIS, default /app/data/de421.bsp). Nothing is ever downloaded at run
time: the file is opened only if it exists, through a Loader with expire=False, and the
timescale uses Skyfield's built-in tables. Without the file the screen is unavailable.

Planets.query(lat, lon, cut, t0) answers like a satellite screen:
    {"count", "age_h": 0, "objects": [{"n": "MOON"|"VENUS"..., "tag": "MOON"|"PLANET",
     "trk": 13 [az, el] points 5 s apart from t0}], "next": None | {"n", "tag", "in_s",
     "from", "max_el": 0}}
t0 is the engine's current tick, the same start time as the satellite tracks of that reply,
so the reply's lag_ms means the same thing. Elevations are geometric (no refraction), as for
satellites. "next" (only when nothing is above the cutoff) is the earliest of the six to rise
above the cutoff within 24 h (picked on a 10-minute table, then timed with Skyfield's
almanac.find_risings), "from" the compass point of its rise azimuth; max_el is always 0 (these bodies have no "pass" in the satellite sense).

Caches: results per (lat, lon, cut) for the current t0 only (cleared when t0 moves on), at
most CACHE_MAX; the rise search per (lat, lon, cut) for NEXT_CACHE_S, at most CACHE_MAX.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

import numpy as np

from engine import TRACK_POINTS, TRACK_STEP_S, _az
from skymath import compass

DEFAULT_PATH = "/app/data/de421.bsp"
CACHE_MAX = 256
NEXT_CACHE_S = 600
RISE_WINDOW_S = 24 * 3600
RISE_GRID_S = 600                 # coarse step that picks which body to search for its rise
# (name sent to the dial, DE421 segment, tag)
BODIES = (
    ("MOON", "moon", "MOON"),
    ("MERCURY", "mercury", "PLANET"),
    ("VENUS", "venus", "PLANET"),
    ("MARS", "mars", "PLANET"),
    ("JUPITER", "jupiter barycenter", "PLANET"),
    ("SATURN", "saturn barycenter", "PLANET"),
)


class Planets:
    def __init__(self, path: str = DEFAULT_PATH, log=print):
        self.path = path
        self.log = log
        self._lock = threading.Lock()          # guards the caches and the one-time load
        self._calc = threading.Lock()          # one Skyfield calculation at a time
        self._loaded = False
        self._eph = self._ts = self._earth = self._bodies = None
        self._t0 = None                         # t0 of the entries in _cache
        self._cache = OrderedDict()             # (lat, lon, cut) -> result, for _t0 only
        self._next = OrderedDict()              # (lat, lon, cut) -> (computed at t0, val)

    # -- loading -------------------------------------------------------------------

    def _load(self) -> bool:
        with self._lock:
            if not self._loaded:
                self._loaded = True
                try:
                    if not os.path.isfile(self.path):        # never let the Loader download
                        raise FileNotFoundError(self.path)
                    from skyfield.api import Loader
                    loader = Loader(os.path.dirname(os.path.abspath(self.path)), verbose=False, expire=False)
                    self._eph = loader(os.path.basename(self.path))
                    self._ts = loader.timescale(builtin=True)
                    self._earth = self._eph["earth"]
                    self._bodies = [(n, self._eph[seg], tag) for n, seg, tag in BODIES]
                except Exception as e:
                    self._eph = None
                    self.log(f"[planets] ephemeris not loaded ({type(e).__name__}); screen unavailable")
            return self._eph is not None

    def available(self) -> bool:
        return self._load()

    def warm(self, now: float):
        """One uncached calculation at start-up, so the first dial does not pay for the file's
        first reads (at 0, 0; its rise search may stay in the rise cache, which is correct)."""
        if self._load():
            self._compute(0.0, 0.0, 0, now, None)
            self._search(0.0, 0.0, 0, now)

    def close(self):
        """Close the ephemeris file (the service keeps it open for its lifetime)."""
        with self._lock:
            if self._eph is not None:
                self._eph.close()
            self._eph, self._loaded = None, True

    # -- queries -------------------------------------------------------------------

    def is_cached(self, lat: float, lon: float, cut: int, t0) -> bool:
        """True when query() would answer from the cache without calculating."""
        with self._lock:
            return t0 is not None and self._t0 == t0 and (lat, lon, cut) in self._cache

    def query(self, lat: float, lon: float, cut: int, t0: float) -> dict:
        if not t0 or not self._load():
            return {"unavailable": True}
        key = (lat, lon, cut)
        with self._lock:
            if self._t0 == t0 and key in self._cache:
                return self._cache[key]
            nxt = self._next.get(key)
        out = self._compute(lat, lon, cut, t0, nxt)
        with self._lock:
            if self._t0 is None or t0 > self._t0:           # a newer tick: older results go
                self._t0 = t0
                self._cache.clear()
            if t0 == self._t0:
                self._cache[key] = out
                while len(self._cache) > CACHE_MAX:
                    self._cache.popitem(last=False)
        return out

    def _unix(self, unix_s):
        """Unix time (no leap seconds) -> Skyfield Time. Passing seconds since 1970 straight to
        ts.utc would count the leap seconds since 1972 and land 27 s late; whole days plus the
        seconds of the day do not."""
        days, sec = np.divmod(np.asarray(unix_s, dtype=float), 86400.0)
        return self._ts.utc(1970, 1, 1 + days.astype(int), 0, 0, sec)

    def _compute(self, lat, lon, cut, t0, nxt):
        from skyfield.api import wgs84
        with self._calc:
            here = self._earth + wgs84.latlon(lat, lon)
            at = here.at(self._unix(t0 + np.arange(TRACK_POINTS) * float(TRACK_STEP_S)))
            rows = []
            for name, body, tag in self._bodies:
                alt, az, _ = at.observe(body).apparent().altaz()
                rows.append((name, tag, az.degrees, alt.degrees))
        up = sorted((r for r in rows if r[3][0] >= cut), key=lambda r: -r[3][0])
        objects = [{"n": n, "tag": tag, "trk": [[_az(a), round(float(e), 1)] for a, e in zip(az, el)]}
                   for n, tag, az, el in up]
        out = {"count": len(objects), "age_h": 0, "objects": objects, "next": None}
        if not objects:
            out["next"] = self._next_rise(lat, lon, cut, t0, nxt)
        return out

    def _next_rise(self, lat, lon, cut, t0, nxt):
        key = (lat, lon, cut)
        if nxt is not None:
            at0, val = nxt
            if 0 <= t0 - at0 < NEXT_CACHE_S and (val is None or val["at"] > t0):
                return _next_out(val, t0)
        val = self._search(lat, lon, cut, t0)
        with self._lock:
            self._next[key] = (t0, val)
            self._next.move_to_end(key)
            while len(self._next) > CACHE_MAX:
                self._next.popitem(last=False)
        return _next_out(val, t0)

    def _search(self, lat, lon, cut, t0):
        """Earliest rise above the cutoff within RISE_WINDOW_S after t0 among the six, or None.

        A coarse table (every RISE_GRID_S; astrometric altitude, within 0.01 degree of the
        apparent one) picks the body that is first above the cutoff; only the bodies first above
        it at that sample or the next one are then searched with almanac.find_risings, over the
        window up to that next sample. A body that rises and sets again between two samples of
        the coarse table (a graze of under RISE_GRID_S above the cutoff) is not seen."""
        from skyfield import almanac
        from skyfield.api import wgs84
        with self._calc:
            topos = wgs84.latlon(lat, lon)
            here = self._earth + topos
            grid = t0 + np.arange(0, RISE_WINDOW_S + 1, RISE_GRID_S, dtype=float)
            at = here.at(self._unix(grid))
            first = []
            for name, body, tag in self._bodies:
                up = at.observe(body).frame_latlon(topos)[0].degrees >= cut
                up[0] = False                          # nothing is up at t0 (else no search)
                first.append(int(up.argmax()) if up.any() else None)
            known = [j for j in first if j is not None]
            if not known:
                return None
            j = min(known)
            start, end = self._unix(t0), self._unix(grid[min(j + 1, grid.size - 1)] + 1.0)
            best = None
            for (name, body, tag), jb in zip(self._bodies, first):
                if jb is None or jb > j + 1:
                    continue
                times, rises = almanac.find_risings(here, body, start, end, horizon_degrees=float(cut))
                times = times[rises]               # False = only touches the cutoff
                if len(times) and (best is None or times[0].tt < best[0].tt):
                    best = (times[0], name, body, tag)
            if best is None:
                return None
            t, name, body, tag = best
            _, az, _ = here.at(t).observe(body).apparent().altaz()
            return {"n": name, "tag": tag, "at": t.utc_datetime().timestamp(), "from": compass(az.degrees)}


def _next_out(val, t0):
    if val is None:
        return None
    return {"n": val["n"], "tag": val["tag"], "in_s": max(0, int(val["at"] - t0)),
            "from": val["from"], "max_el": 0}
