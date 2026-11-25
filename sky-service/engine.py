"""Propagation and per-observer queries.

tick() propagates every tracked object once for all callers; query() does only the
observer transform and filtering, cached per (screen, 0.1-degree square, cutoff).
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np
from sgp4.api import SatrecArray

from skymath import azel, compass, unix_to_jd

TRACK_STEP_S = 5
TRACK_POINTS = 13                 # 60 s of track
MAX_OBJECTS = 60
PASS_STEP_S = 30
PASS_CACHE_S = 300
CACHE_MAX = 5000
OLD_EPOCH_S = 14 * 86400          # objects measured longer ago than this are marked "old"
STALE_TICK_S = 20                 # no tick for this long = the background loop has stalled


class _Screen:
    def __init__(self, elems, tags, fetched_at):
        self.names = [e.name for e in elems]
        self.epochs = [e.epoch for e in elems]
        self.tags = list(tags)
        self.arr = SatrecArray([e.sat for e in elems])
        self.fetched_at = fetched_at   # unix time the elements were downloaded (0 = unknown)
        self.r = None             # (N, TRACK_POINTS, 3) km TEME, filled by tick()
        self.ok = None            # (N,) bool: propagated without error


class Engine:
    def __init__(self):
        self._lock = threading.Lock()
        self._screens = {}
        self._t0 = 0.0
        self._jd = None
        self._tick_id = 0
        self._qcache = OrderedDict()
        self._pcache = OrderedDict()
        self.dropped = {}             # source -> objects left out as too old (for /health)

    def tick_age(self, now_unix: float) -> float:
        """Seconds since the last tick (infinite before the first)."""
        with self._lock:
            return now_unix - self._t0 if self._t0 else float("inf")

    def set_screens(self, built: dict, fetched: dict):
        """built: screen -> (elems, tags) from screens.build_screens; fetched: screen -> unix time."""
        new = {name: _Screen(elems, tags, fetched.get(name, 0.0)) for name, (elems, tags) in built.items()}
        with self._lock:
            self._screens = new
            self._qcache.clear()
            self._pcache.clear()

    def has(self, screen: str) -> bool:
        with self._lock:
            return screen in self._screens and self._screens[screen].r is not None

    def is_cached(self, screen: str, lat: float, lon: float, cut: int) -> bool:
        """True when query() would answer from the per-tick cache without calculating."""
        with self._lock:
            return (screen, lat, lon, cut) in self._qcache

    def tick(self, now_unix: float):
        """Propagate everything for now .. now+60 s. Call every TRACK_STEP_S seconds."""
        with self._lock:
            screens = dict(self._screens)
        t = now_unix + np.arange(TRACK_POINTS) * TRACK_STEP_S
        jd_full = unix_to_jd(t)
        jd_i = np.floor(jd_full)
        results = {}
        for name, sc in screens.items():
            err, r, _ = sc.arr.sgp4(jd_i, jd_full - jd_i)
            results[name] = (r, (err == 0).all(axis=1))
        with self._lock:
            for name, (r, ok) in results.items():
                if self._screens.get(name) is screens[name]:
                    screens[name].r, screens[name].ok = r, ok
            self._t0, self._jd = float(now_unix), jd_full
            self._tick_id += 1
            self._qcache.clear()

    def query(self, screen: str, lat: float, lon: float, cut: int) -> dict:
        key = (screen, lat, lon, cut)
        with self._lock:
            sc = self._screens.get(screen)
            hit = self._qcache.get(key)
            if hit is not None:
                return hit
            if sc is None or sc.r is None:
                return {"unavailable": True}
            r, ok, jd, t0 = sc.r, sc.ok, self._jd, self._t0
        az, el, _ = azel(r, jd, lat, lon)
        up = np.flatnonzero(ok & (el[:, 0] >= cut))
        order = up[np.argsort(-el[up, 0])][:MAX_OBJECTS]
        objects = []
        for i in order:
            obj = {"n": sc.names[i], "tag": sc.tags[i],
                   "trk": [[round(float(a), 1), round(float(e), 1)] for a, e in zip(az[i], el[i])]}
            if t0 - sc.epochs[i] > OLD_EPOCH_S:
                obj["old"] = True
            objects.append(obj)
        age_h = round((t0 - sc.fetched_at) / 3600.0, 1) if sc.fetched_at else -1.0
        out = {"count": int(up.size), "age_h": age_h, "objects": objects,
               "next": None if up.size else self._next_pass(screen, sc, lat, lon, cut, t0)}
        with self._lock:
            self._qcache[key] = out
            while len(self._qcache) > CACHE_MAX:
                self._qcache.popitem(last=False)
        return out

    def _next_pass(self, screen, sc, lat, lon, cut, t0):
        from screens import SCREENS
        key = (screen, lat, lon, cut)
        with self._lock:
            hit = self._pcache.get(key)
        if hit is not None and hit[0] > t0:
            return _pass_out(hit[1], t0)
        steps = SCREENS[screen]["horizon_s"] // PASS_STEP_S
        t = t0 + np.arange(1, steps + 1) * PASS_STEP_S
        jd_full = unix_to_jd(t)
        jd_i = np.floor(jd_full)
        err, r, _ = sc.arr.sgp4(jd_i, jd_full - jd_i)
        az, el, _ = azel(r, jd_full, lat, lon)
        above = (el >= cut) & (err == 0)
        val = None
        if above.any():
            first = np.where(above.any(axis=1), above.argmax(axis=1), steps + 1)
            i = int(first.argmin())
            j = int(first[i])
            k = j
            while k + 1 < steps and above[i, k + 1]:
                k += 1
            val = {"n": sc.names[i], "tag": sc.tags[i], "at": float(t[j]),
                   "from": compass(az[i, j]), "max_el": int(round(float(el[i, j:k + 1].max())))}
        with self._lock:
            self._pcache[key] = (t0 + PASS_CACHE_S, val)
            while len(self._pcache) > CACHE_MAX:
                self._pcache.popitem(last=False)
        return _pass_out(val, t0)


def _pass_out(val, t0):
    if val is None:
        return None
    return {"n": val["n"], "tag": val["tag"], "in_s": max(0, int(val["at"] - t0)),
            "from": val["from"], "max_el": val["max_el"]}
