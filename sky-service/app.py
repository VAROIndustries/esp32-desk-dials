"""Sky service (esp32-desk-dials): what is overhead, for the VARO Desk Dial "Sky" source.

    GET /v1/sky?lat=42.0&lon=-71.3&screens=starlink,nasa[&cut=starlink:60,nasa:20]
        The spy screen also needs  Authorization: Bearer <SKY_SPY_KEY>
        Reply: {"t": <unix>, "poll": 50, "lag_ms": <ms since the tracks start>,
                "screens": {<name>: {...} | {"locked": true} | {"unavailable": true}},
                "notice": <optional text>}
    GET /health     per-source data age; 503 when the calculation has stalled. The private
                    sources are listed only for loopback callers or with the spy key.

Every reply is Cache-Control: private, no-store. Query strings are never logged.

Environment
  PORT              default 8080
  DATA_DIR          where last good element files are kept (mount a volume), default ./data
  SKY_CONTACT       contact address put in the User-Agent sent to CelesTrak (required)
  SKY_SPY_KEY       enables the private spy screen for dials that send this key (>= 24 chars)
  SPY_HOBBY         "0" turns the extra source off (default on when SPY_HOBBY_URL is set)
  SPY_HOBBY_URL     https:// address of an extra TLE file for the private screen
  SPY_PROXY_URL     optional proxy used ONLY for SPY_HOBBY_URL
  ORIGIN_SECRET     when set, every request must carry X-Origin-Auth with this value
                    (added by a Cloudflare rule) or it is refused; the caller address is then
                    taken from CF-Connecting-IP. /health from loopback is always answered.
  RATE_PER_MIN      per-address request limit, default 30
  CALC_PER_SEC      global ceiling on fresh (uncached) calculations per second, default 10
  SKY_NOTICE        optional display-only text sent to every dial

Two background threads: a ticker (engine.tick every TRACK_STEP_S) and a fetcher (downloads and
screen rebuilds), so a slow download can never stall the dials.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import screens as S
from engine import Engine, STALE_TICK_S, TRACK_STEP_S
from store import CELESTRAK_INTERVAL_S, HOBBY_INTERVAL_S, Source, Store, requests_get

POLL_S = 50                       # tracks are 60 s long; ask again before they run out
BUSY_POLL_S = 300
MAX_BODY = 64 * 1024
MAX_IN_PROGRESS = 32
SOCKET_TIMEOUT_S = 10
MIN_KEY_LEN = 24
WRONG_KEY_EXTRA_COST = 5          # a wrong key costs 6 units of the per-address allowance
FETCH_EVERY_S = 60                # at most one download per minute (staggers the CelesTrak groups)
REBUILD_EVERY_S = 3600            # rebuild hourly anyway, so ageing orbits drop out
PRIVATE_SOURCES = ("military", S.HOBBY_SOURCE)
LOOPBACK = ("127.0.0.1", "::1")

NO_STORE = {"Cache-Control": "private, no-store"}     # replies are time-critical: never cached


class Limiter:
    """Fixed-window counters: per-key per minute, and one global per second."""

    def __init__(self, per_min, per_sec, clock=time.time):
        self.per_min, self.per_sec, self.clock = per_min, per_sec, clock
        self._lock = threading.Lock()
        self._min, self._min_start = {}, 0
        self._sec, self._sec_start = 0, 0

    def allow_key(self, key, cost=1) -> bool:
        with self._lock:
            w = int(self.clock() // 60)
            if w != self._min_start:
                self._min, self._min_start = {}, w
            self._min[key] = self._min.get(key, 0) + cost
            return self._min[key] <= self.per_min

    def allow_global(self) -> bool:
        with self._lock:
            w = int(self.clock())
            if w != self._sec_start:
                self._sec, self._sec_start = 0, w
            self._sec += 1
            return self._sec <= self.per_sec


class BadRequest(Exception):
    pass


def parse_request(query: str):
    """-> (lat, lon, [screen names], {screen: cut}). Raises BadRequest."""
    try:
        q = parse_qs(query, keep_blank_values=True, max_num_fields=8)
    except ValueError:
        raise BadRequest("too many fields") from None
    try:
        lat, lon = float(q["lat"][0]), float(q["lon"][0])
    except (KeyError, ValueError, IndexError):
        raise BadRequest("lat and lon are required numbers") from None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise BadRequest("lat or lon out of range")
    names = [n for n in q.get("screens", [""])[0].split(",") if n]
    if not names or len(names) > S.MAX_SCREENS_PER_REQUEST or len(set(names)) != len(names):
        raise BadRequest(f"screens: 1 to {S.MAX_SCREENS_PER_REQUEST} distinct names")
    if any(n not in S.SCREENS for n in names):
        raise BadRequest("unknown screen")
    cuts = {n: S.SCREENS[n]["cut"] for n in names}
    for part in [p for p in q.get("cut", [""])[0].split(",") if p]:
        name, _, val = part.partition(":")
        if name not in cuts or not (val.isascii() and val.isdigit()) or not S.CUT_MIN <= int(val) <= S.CUT_MAX:
            raise BadRequest("bad cut")
        cuts[name] = int(val)
    return round(lat, 1) + 0.0, round(lon, 1) + 0.0, names, cuts


class App:
    def __init__(self, engine, store, env, clock=time.time):
        self.engine, self.store, self.clock = engine, store, clock
        self.spy_key = env.get("SKY_SPY_KEY", "")
        self.origin_secret = env.get("ORIGIN_SECRET", "")
        self.notice = env.get("SKY_NOTICE", "")[:120]
        self.limiter = Limiter(int(env.get("RATE_PER_MIN", "30")), int(env.get("CALC_PER_SEC", "10")), clock)

    def key_ok(self, auth_header: str) -> bool:
        given = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        return bool(self.spy_key) and bool(given) and hmac.compare_digest(given.encode(), self.spy_key.encode())

    def handle(self, path_qs: str, headers: dict, peer_ip: str):
        """-> (status, body dict, extra headers). headers: lower-cased names."""
        status, body, extra = self._handle(path_qs, headers, peer_ip)
        return status, body, dict(extra, **NO_STORE)

    def _check_key(self, headers, ip) -> bool:
        """True for the right spy key. A wrong key uses the caller's allowance up 6x faster."""
        auth = headers.get("authorization", "")
        if not auth:
            return False
        keyed = self.key_ok(auth)
        if not keyed:
            self.limiter.allow_key(ip, cost=WRONG_KEY_EXTRA_COST)
        return keyed

    def _handle(self, path_qs, headers, peer_ip):
        url = urlsplit(path_qs)
        local = peer_ip in LOOPBACK
        local_health = url.path == "/health" and local              # container health check
        if self.origin_secret and not local_health:
            if not hmac.compare_digest(headers.get("x-origin-auth", "").encode(), self.origin_secret.encode()):
                return 403, {"error": "forbidden"}, {}
            ip = headers.get("cf-connecting-ip", peer_ip)
        else:
            ip = peer_ip
        now = self.clock()
        age = self.engine.tick_age(now)
        stalled = age > STALE_TICK_S
        if url.path == "/health":
            trusted = local
            if headers.get("authorization"):           # a key check here is charged like on /v1/sky
                if not self.limiter.allow_key(ip):
                    return 429, {"error": "slow down", "poll": BUSY_POLL_S}, {"Retry-After": "60"}
                trusted = self._check_key(headers, ip) or trusted
            sources = self.store.health(now)
            dropped = dict(self.engine.dropped)
            if not trusted:                            # the public must not learn the spy screen is on
                sources = {k: v for k, v in sources.items() if k not in PRIVATE_SOURCES}
                dropped = {k: v for k, v in dropped.items() if k not in PRIVATE_SOURCES}
            body = {"ok": not stalled, "tick_age_s": round(age, 1) if age != float("inf") else -1,
                    "edge_locked": bool(self.origin_secret), "dropped_old": dropped, "sources": sources}
            return (503 if stalled else 200), body, {}
        if url.path != "/v1/sky":
            return 404, {"error": "not found"}, {}
        if not self.limiter.allow_key(ip):
            return 429, {"error": "slow down", "poll": BUSY_POLL_S}, {"Retry-After": "60"}
        try:
            lat, lon, names, cuts = parse_request(url.query)
        except BadRequest as e:
            return 400, {"error": str(e)}, {}
        keyed = self._check_key(headers, ip)
        visible = {n: cuts[n] for n in names if keyed or not S.SCREENS[n]["private"]}
        fresh = not stalled and any(not self.engine.is_cached(n, lat, lon, c) for n, c in visible.items())
        if fresh and not self.limiter.allow_global():
            return 503, {"error": "busy", "poll": BUSY_POLL_S}, {"Retry-After": str(BUSY_POLL_S)}
        if stalled:
            t0, results = None, {n: {"unavailable": True} for n in visible}
        else:
            t0, results = self.engine.query_many(lat, lon, visible)   # one snapshot: every screen, one tick
        out = {n: results[n] if n in visible else {"locked": True} for n in names}
        lag_ms = 0 if stalled else max(0, int((now - t0) * 1000))
        body = {"t": int(now), "poll": POLL_S, "lag_ms": lag_ms, "screens": out}
        if self.notice:
            body["notice"] = self.notice
        return 200, body, {}


def make_handler(app, slots=None):
    slots = threading.BoundedSemaphore(MAX_IN_PROGRESS) if slots is None else slots

    class Handler(BaseHTTPRequestHandler):
        server_version = "sky-service"
        sys_version = ""
        timeout = SOCKET_TIMEOUT_S                    # a silent client is dropped

        def log_message(self, fmt, *args):            # never log the query string (location)
            pass

        def do_GET(self):
            if not slots.acquire(blocking=False):
                status, body, extra = 503, {"error": "busy", "poll": BUSY_POLL_S}, dict(NO_STORE, **{"Retry-After": "30"})
            else:
                try:
                    hdrs = {k.lower(): v for k, v in self.headers.items()}
                    status, body, extra = app.handle(self.path, hdrs, self.client_address[0])
                finally:
                    slots.release()
            raw = json.dumps(body, separators=(",", ":")).encode()
            if len(raw) > MAX_BODY:
                status, raw = 500, b'{"error":"reply too large"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)
            print(f"[http] {status} {urlsplit(self.path).path}", flush=True)
    return Handler


def build_sources(env):
    src = [Source(name, S.CELESTRAK.format(group=group), "omm", CELESTRAK_INTERVAL_S)
           for name, group in S.GROUPS.items() if name != "military" or env.get("SKY_SPY_KEY")]
    hobby_url = env.get("SPY_HOBBY_URL", "")
    hobby_on = bool(env.get("SKY_SPY_KEY")) and env.get("SPY_HOBBY", "1") != "0" and hobby_url.startswith("https://")
    if hobby_on:
        src.append(Source(S.HOBBY_SOURCE, hobby_url, "tle", HOBBY_INTERVAL_S, env.get("SPY_PROXY_URL") or None))
    return src, hobby_on


def rebuild(engine, store, env, hobby_on, now):
    elems = {n: s.elems for n, s in store.sources.items()}
    built, dropped = S.build_screens(elems, bool(env.get("SKY_SPY_KEY")), hobby_on, now)

    def oldest(names):          # 0.0 = unknown (a source never fetched has fetched_at None)
        times = [store.sources[n].fetched_at for n in names
                 if n in store.sources and store.sources[n].fetched_at is not None]
        return min(times) if times else 0.0
    fetched = {"starlink": oldest(["starlink"]), "nasa": oldest(["stations", "science", "resource"]),
               "spy": oldest(["military", S.HOBBY_SOURCE])}
    engine.set_screens(built, fetched)
    engine.dropped = dropped


def _log_error(e):
    print(f"[sky] background error: {type(e).__name__}: {e}", flush=True)


def tick_once(engine, now):
    """One pass of the ticker thread: never touches the network or the disk."""
    try:
        engine.tick(now)
    except Exception as e:                          # keep ticking; /health shows a stall
        _log_error(e)


def new_fetch_state() -> dict:
    return {"last_fetch": 0.0, "last_rebuild": 0.0}


def fetch_once(state, engine, store, env, hobby_on, now):
    """One pass of the fetcher thread: at most one download per FETCH_EVERY_S (the most overdue
    source), then a rebuild when that download succeeded or REBUILD_EVERY_S has passed."""
    try:
        got = False
        if now - state["last_fetch"] >= FETCH_EVERY_S:
            due = store.due(now)
            if due:
                state["last_fetch"] = now
                got = store.refresh(due[0], now)
        if got or now - state["last_rebuild"] >= REBUILD_EVERY_S:
            rebuild(engine, store, env, hobby_on, now)
            state["last_rebuild"] = now
    except Exception as e:
        _log_error(e)


def _ticker(engine, stop):
    while not stop.is_set():
        start = time.time()
        tick_once(engine, start)
        stop.wait(max(0.5, TRACK_STEP_S - (time.time() - start)))


def _fetcher(state, engine, store, env, hobby_on, stop):
    while not stop.is_set():
        fetch_once(state, engine, store, env, hobby_on, time.time())
        stop.wait(TRACK_STEP_S)


def check_env(env) -> list:
    """Refuse unsafe settings (SystemExit); return warnings for the rest."""
    if not env.get("SKY_CONTACT"):
        raise SystemExit("SKY_CONTACT is required (contact address sent to CelesTrak)")
    key = env.get("SKY_SPY_KEY", "")
    if key and len(key) < MIN_KEY_LEN:
        raise SystemExit(f"SKY_SPY_KEY must be at least {MIN_KEY_LEN} characters")
    warn = []
    if not env.get("ORIGIN_SECRET"):
        warn.append("ORIGIN_SECRET is not set: requests are accepted from any address, not only Cloudflare")
    return warn


def main():
    env = os.environ
    for w in check_env(env):
        print(f"[sky] WARNING: {w}", flush=True)
    contact = env["SKY_CONTACT"]
    sources, hobby_on = build_sources(env)
    ua = f"VARO-sky-service/1.0 (+https://github.com/VAROIndustries/esp32-desk-dials; {contact})"
    store = Store(env.get("DATA_DIR", "./data"), sources, requests_get, ua)
    store.load_disk()
    engine = Engine()
    now = time.time()
    state = new_fetch_state()
    rebuild(engine, store, env, hobby_on, now)    # screens from the disk copies before any download
    state["last_rebuild"] = now
    engine.tick(now)
    stop = threading.Event()
    threading.Thread(target=_ticker, args=(engine, stop), name="sky-ticker", daemon=True).start()
    threading.Thread(target=_fetcher, args=(state, engine, store, env, hobby_on, stop),
                     name="sky-fetcher", daemon=True).start()
    port = int(env.get("PORT", "8080"))
    print(f"[sky] listening on :{port}; spy screen {'on' if env.get('SKY_SPY_KEY') else 'off'}, "
          f"hobbyist source {'on' if hobby_on else 'off'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(App(engine, store, env))).serve_forever()


if __name__ == "__main__":
    main()
