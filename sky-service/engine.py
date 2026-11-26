"""Propagation and per-observer queries.

tick() propagates every tracked object once for all callers: the 60 s track that every
answer animates, and (every LOOKAHEAD_REFRESH_S) a look-ahead table of positions over
the screen's pass-search horizon. query()/query_many() do only the observer transform
and filtering, never propagation; their results are cached per (screen, 0.1-degree
square, cutoff) for the current tick only, at most QCACHE_MAX of them.

Memory: each screen's look-ahead table holds objects x steps x 12 bytes (float32 TEME
positions) plus objects x steps x 1 byte of error flags, where steps is
(horizon_s + LOOKAHEAD_REFRESH_S) / step. Example: 12,000 Starlink objects over
20 + 5 minutes at 30 s = 50 steps is about 7.8 MB.

Rebuilds: set_screens() only stages a new screen set; queries keep using the current one
until the next tick() has tracked the staged set (tracks and tables), then that tick swaps it in.

Per-screen extras (in _tracks, and the mask passed to _search):
  gnss   -> "by_tag": count per constellation tag over everything above the cutoff;
  new    -> "d" on each object: whole days since launch, when the launch date is known;
  bright -> "dark": whether the observer's Sun is below DARK_SUN_EL; objects (and "count")
            only when dark, and then only those in sunlight; the next pass is the first one
            that is above the cutoff, sunlit and in the observer's dark at the same time.

Concurrency: one thread calls tick() and set_screens(); any number of threads query.
A query takes one consistent snapshot under the lock (every screen from the same tick
and element set), computes outside it, and caches only if that tick and element set
are still current.
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict

import numpy as np
from sgp4.api import SatrecArray

from screens import SCREENS
from skymath import azel, compass, sun_elevation, sun_teme, sunlit, unix_to_jd

TRACK_STEP_S = 5
TRACK_POINTS = 13                 # 60 s of track
MAX_OBJECTS = 60
PASS_STEP_S = 30                  # look-ahead step for horizons up to one hour
PASS_STEP_LONG_S = 60             # look-ahead step for longer horizons
PASS_CACHE_S = 300                # a found pass is reused for at most this long
PASS_SEARCH_SLOTS = 2             # request-time pass searches allowed at once, all threads
PASS_CHUNK_POINTS = 200_000       # objects x steps transformed at a time (bounds memory)
LOOKAHEAD_REFRESH_S = 300         # rebuild a look-ahead table older than this
CACHE_MAX = 5000                  # pass-search cache entries (small: one pass each)
QCACHE_MAX = 256                  # per-tick results (up to ~116 KB each at 60 objects: ~30 MB)
OLD_EPOCH_S = 14 * 86400          # objects measured longer ago than this are marked "old"
STALE_TICK_S = 20                 # no tick for this long = the background loop has stalled
DARK_SUN_EL = -6.0                # bright screen: the observer is "dark" below this Sun elevation
GNSS_TAGS = ("GPS", "GAL", "GLO", "BDS")


def _az(a) -> float:
    """Azimuth rounded to 0.1 degree, always in [0, 360)."""
    v = round(float(a) % 360.0, 1)
    return 0.0 if v >= 360.0 else v


def _pass_step(horizon_s: int) -> int:
    return PASS_STEP_S if horizon_s <= 3600 else PASS_STEP_LONG_S


def _propagate(arr, unix_times):
    jd_full = unix_to_jd(np.asarray(unix_times, dtype=float))
    jd_i = np.floor(jd_full)
    err, r, _ = arr.sgp4(jd_i, jd_full - jd_i)
    return jd_full, err, r


class _Table:
    """Positions of every object of one screen at fixed steps after `built`."""

    def __init__(self, arr, built: float, horizon_s: int):
        self.built = float(built)
        self.horizon_s = horizon_s
        self.step = _pass_step(horizon_s)
        k = math.ceil((horizon_s + LOOKAHEAD_REFRESH_S) / self.step)
        self.t = self.built + np.arange(1, k + 1) * self.step
        self.jd, err, r = _propagate(arr, self.t)
        self.bad = (err != 0) | ~np.isfinite(r).all(axis=2)          # (N, K)
        self.r = np.where(self.bad[..., None], 0.0, r).astype(np.float32)


class _Screen:
    def __init__(self, name, elems, tags, fetched_at, launch=None):
        self.name = name
        self.names = [e.name for e in elems]
        self.epochs = [e.epoch for e in elems]
        self.tags = list(tags)
        self.arr = SatrecArray([e.sat for e in elems])
        self.fetched_at = fetched_at   # unix time the elements were downloaded (0 = unknown)
        self.launch = None if launch is None else list(launch)   # new screen: unix launch time or None
        self.r = None             # (N, TRACK_POINTS, 3) km TEME, filled by tick()
        self.ok = None            # (N,) bool: track propagated without error
        self.table = None         # _Table, filled by tick()


class Engine:
    def __init__(self):
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(PASS_SEARCH_SLOTS)
        self._screens = {}
        self._staged = None       # screen set from set_screens(), swapped in by the next tick()
        self._t0 = None           # unix time of the last tick (None = never ticked)
        self._jd = None
        self._version = 0         # bumped by every tick() (which also swaps in staged screens)
        self._qcache = OrderedDict()   # key -> result, for the current version only
        self._pcache = OrderedDict()   # key -> (screen, table.built or None, t0, val)
        self.dropped = {}             # source -> objects left out as too old (for /health)

    def tick_age(self, now_unix: float) -> float:
        """Seconds since the last tick (infinite before the first)."""
        with self._lock:
            return float("inf") if self._t0 is None else now_unix - self._t0

    def tick_time(self):
        """Unix time of the last tick (the start of every track), or None before the first."""
        with self._lock:
            return self._t0

    def set_screens(self, built: dict, fetched: dict):
        """built: screen -> (elems, tags[, launch]) from screens.build_screens; fetched: screen -> unix time."""
        new = {name: _Screen(name, entry[0], entry[1], fetched.get(name, 0.0), *entry[2:3])
               for name, entry in built.items()}
        with self._lock:
            self._staged = new          # queries keep the current screens until tick() swaps it in

    def has(self, screen: str) -> bool:
        with self._lock:
            return screen in self._screens and self._screens[screen].r is not None

    def is_cached(self, screen: str, lat: float, lon: float, cut: int) -> bool:
        """True when query() would answer from the current tick's cache without calculating."""
        with self._lock:
            return (screen, lat, lon, cut) in self._qcache

    def tick(self, now_unix: float):
        """Propagate everything for now .. now+60 s, and rebuild look-ahead tables that are
        missing or older than LOOKAHEAD_REFRESH_S. Call every TRACK_STEP_S seconds."""
        now = float(now_unix)
        with self._lock:
            staged = self._staged
            screens = dict(self._screens if staged is None else staged)
        t = now + np.arange(TRACK_POINTS) * TRACK_STEP_S
        jd_full = unix_to_jd(t)
        results = {}
        for name, sc in screens.items():
            _, err, r = _propagate(sc.arr, t)
            ok = (err == 0).all(axis=1) & np.isfinite(r).all(axis=(1, 2))
            tab = sc.table
            if tab is None or now - tab.built > LOOKAHEAD_REFRESH_S or now < tab.built:
                tab = _Table(sc.arr, now, SCREENS[name]["horizon_s"])
            results[name] = (r, ok, tab)
        with self._lock:
            for name, (r, ok, tab) in results.items():
                if staged is not None or self._screens.get(name) is screens[name]:
                    screens[name].r, screens[name].ok, screens[name].table = r, ok, tab
            if staged is not None:      # the staged set is fully tracked: it takes over now
                self._screens = staged
                if self._staged is staged:
                    self._staged = None
                self._pcache.clear()
            self._t0, self._jd = now, jd_full
            self._version += 1
            self._qcache.clear()

    def query(self, screen: str, lat: float, lon: float, cut: int) -> dict:
        """One screen; see query_many for the result."""
        return self.query_many(lat, lon, {screen: cut})[1][screen]

    def query_many(self, lat: float, lon: float, wanted: dict) -> tuple:
        """wanted: screen -> cutoff. Returns (t0, results): t0 is the unix time of the tick
        every returned track starts at (0.0 before the first tick), results maps each screen
        to {"count", "age_h", "objects", "next"} or {"unavailable": True}. All screens come
        from the same tick.

        "next" (only when nothing is above the cutoff) is the first pass rising within the
        screen's horizon, or None. Its "from" and "max_el" are sampled at the look-ahead step
        (30 s, or 60 s for horizons over an hour), and "max_el" is the peak inside the
        horizon (a pass still climbing at the horizon's end reports its elevation there).
        When no pass search can run right now, "next" is None and "next_busy" is True.
        """
        return self._finish(self._begin(lat, lon, wanted))

    # -- internals: _begin snapshots under the lock, _finish computes and stores --------

    def _begin(self, lat, lon, wanted):
        plan = {"lat": lat, "lon": lon, "items": {}}
        with self._lock:
            plan["version"], plan["t0"], plan["jd"] = self._version, self._t0, self._jd
            for screen, cut in wanted.items():
                key = (screen, lat, lon, cut)
                hit = self._qcache.get(key)
                if hit is not None:
                    plan["items"][screen] = ("hit", hit)
                    continue
                sc = self._screens.get(screen)
                if sc is None or sc.r is None:
                    plan["items"][screen] = ("hit", {"unavailable": True})
                    continue
                plan["items"][screen] = ("calc", (cut, sc, sc.r, sc.ok, sc.table,
                                                  self._pcache.get(key)))
        return plan

    def _finish(self, plan):
        lat, lon, t0, jd = plan["lat"], plan["lon"], plan["t0"], plan["jd"]
        results, store_q, store_p = {}, {}, {}
        for screen, (kind, data) in plan["items"].items():
            if kind == "hit":
                results[screen] = data
                continue
            cut, sc, r, ok, table, phit = data
            key = (screen, lat, lon, cut)
            out = self._tracks(sc, r, ok, jd, t0, lat, lon, cut)
            if out["count"] == 0:
                val, cached = self._cached_pass(phit, sc, table, t0)
                if not cached:
                    if table is None or not self._slots.acquire(blocking=False):
                        out["next_busy"] = True
                        val = None
                    else:
                        try:
                            val = _search(sc, ok, table, t0, lat, lon, cut, _mask(sc.name, lat, lon))
                        finally:
                            self._slots.release()
                        store_p[key] = (sc, None if table is None else table.built, t0, val)
                out["next"] = _pass_out(val, t0)
            results[screen] = out
            if "next_busy" not in out:
                store_q[key] = (sc, out)
        with self._lock:
            for key, entry in store_p.items():
                if self._screens.get(key[0]) is entry[0]:          # element set still current
                    self._pcache[key] = entry
                    self._pcache.move_to_end(key)
            while len(self._pcache) > CACHE_MAX:
                self._pcache.popitem(last=False)
            if self._version == plan["version"]:            # tick and element set still current
                for key, (_, out) in store_q.items():
                    self._qcache[key] = out
                while len(self._qcache) > QCACHE_MAX:
                    self._qcache.popitem(last=False)
        return (0.0 if t0 is None else t0), results

    @staticmethod
    def _cached_pass(phit, sc, table, t0):
        """(val, True) if a cached pass search is still the right answer at tick t0."""
        if phit is None:
            return None, False
        psc, pbuilt, pt0, val = phit
        if psc is not sc or t0 < pt0 or t0 - pt0 >= PASS_CACHE_S:
            return None, False
        if val is not None:
            return (val, True) if val["at"] > t0 else (None, False)
        # "nothing rises": still true only while the window ends at the same table row
        if table is None or pbuilt != table.built:
            return None, False
        same_end = (np.searchsorted(table.t, pt0 + table.horizon_s, side="right")
                    == np.searchsorted(table.t, t0 + table.horizon_s, side="right"))
        return (None, True) if same_end else (None, False)

    @staticmethod
    def _tracks(sc, r, ok, jd, t0, lat, lon, cut):
        az, el, _ = azel(r, jd, lat, lon)
        keep = ok & (el[:, 0] >= cut)
        extra = {}
        if sc.name == "bright":
            dark = bool(sun_elevation(jd[0], lat, lon) < DARK_SUN_EL)
            keep &= dark & sunlit(r[:, 0], sun_teme(jd[0]))
            extra["dark"] = dark
        up = np.flatnonzero(keep)
        if sc.name == "gnss":
            counts = {tag: 0 for tag in GNSS_TAGS}
            for i in up:
                if sc.tags[i] in counts:
                    counts[sc.tags[i]] += 1
            extra["by_tag"] = counts
        order = up[np.argsort(-el[up, 0])][:MAX_OBJECTS]
        objects = []
        for i in order:
            obj = {"n": sc.names[i], "tag": sc.tags[i],
                   "trk": [[_az(a), round(float(e), 1)] for a, e in zip(az[i], el[i])]}
            if t0 - sc.epochs[i] > OLD_EPOCH_S:
                obj["old"] = True
            if sc.launch is not None and sc.launch[i] is not None:
                obj["d"] = max(0, int((t0 - sc.launch[i]) // 86400))
            objects.append(obj)
        age_h = round((t0 - sc.fetched_at) / 3600.0, 1) if sc.fetched_at else -1.0
        return dict({"count": int(up.size), "age_h": age_h, "objects": objects, "next": None}, **extra)


def _mask(screen, lat, lon):
    """The extra condition a pass must meet on this screen, or None. Called as
    mask(r[objects, steps, 3], jd[steps]) -> bool[objects, steps]."""
    if screen != "bright":
        return None

    def visible(r, jd):
        sun = sun_teme(jd)                                     # (steps, 3)
        return sunlit(r, sun[None]) & (sun_elevation(jd, lat, lon) < DARK_SUN_EL)[None]
    return visible


def _search(sc, ok, table, t0, lat, lon, cut, mask=None):
    """First pass rising after t0 and within the horizon, from the look-ahead table only.
    mask, if given, is ANDed with "above the cutoff" (see _mask)."""
    lo = int(np.searchsorted(table.t, t0, side="right"))
    hi = int(np.searchsorted(table.t, t0 + table.horizon_s, side="right"))
    if hi <= lo:
        return None
    # skip any object the track leaves out, or that fails at any step of the window
    eligible = np.flatnonzero(ok & ~table.bad[:, lo:hi].any(axis=1))
    if eligible.size == 0:
        return None
    rows = max(1, PASS_CHUNK_POINTS // max(1, eligible.size))
    for a in range(lo, hi, rows):
        b = min(hi, a + rows)
        _, el, _ = azel(table.r[eligible, a:b], table.jd[a:b], lat, lon)
        above = el >= cut
        if mask is not None:
            above &= mask(table.r[eligible, a:b], table.jd[a:b])
        if not above.any():
            continue
        first = np.where(above.any(axis=1), above.argmax(axis=1), b - a)
        idx = int(first.argmin())
        i, j = int(eligible[idx]), a + int(first[idx])
        az_i, el_i, _ = azel(table.r[i:i + 1, j:hi], table.jd[j:hi], lat, lon)
        up = el_i[0] >= cut
        if mask is not None:
            up &= mask(table.r[i:i + 1, j:hi], table.jd[j:hi])[0]
        k = int(up.argmin()) if not up.all() else up.size   # end of this pass in the window
        return {"n": sc.names[i], "tag": sc.tags[i], "at": float(table.t[j]),
                "from": compass(az_i[0, 0]), "max_el": int(round(float(el_i[0, :k].max())))}
    return None


def _pass_out(val, t0):
    if val is None:
        return None
    return {"n": val["n"], "tag": val["tag"], "in_s": max(0, int(val["at"] - t0)),
            "from": val["from"], "max_el": val["max_el"]}
