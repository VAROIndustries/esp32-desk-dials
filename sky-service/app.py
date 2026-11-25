"""Sky service (esp32-desk-dials): what is overhead, for the VARO Desk Dial "Sky" source.

    GET /v1/sky?lat=42.0&lon=-71.3&screens=starlink,nasa[&cut=starlink:60,nasa:20]
        The spy screen also needs  Authorization: Bearer <SKY_SPY_KEY>
        Reply: {"t": <unix>, "poll": 50, "lag_ms": <ms since the tracks start>,
                "screens": {<name>: {...} | {"locked": true} | {"unavailable": true}},
                "notice": <optional text>}
        lat/lon are plain decimals (-?ddd[.dddddddd]); cut values are at most 2 digits.
    GET /health     per-source data age; 503 when the calculation has stalled. The private
                    sources are listed only with the spy key (loopback is not trusted for that).

Every reply is Cache-Control: private, no-store, including the error replies http.server makes
itself (JSON, never echoing the request). Every data reply (200 from /v1/sky) carries lag_ms;
error replies do not. Query strings, paths and caller addresses are never logged: a request log
line is the status and a route label (/v1/sky, /health or other).

Limits (none relies on Cloudflare): per-address requests per minute (IPv6 counted per /64);
a server-wide budget of wrong spy keys per minute; a global ceiling on fresh calculations;
32 requests in progress; 64 open connections (more are closed at accept time); a 10 s socket
timeout and a 15 s deadline per connection; one request per connection.

Environment
  PORT              default 8080 (1..65535)
  DATA_DIR          where last good element files are kept (mount a volume), default ./data
  SKY_CONTACT       contact address put in the User-Agent sent to CelesTrak (required)
  SKY_SPY_KEY       enables the private spy screen for dials that send this key (>= 24 chars)
  SPY_HOBBY         "0" turns the extra source off (default on when SPY_HOBBY_URL is set)
  SPY_HOBBY_URL     https:// address of an extra TLE file for the private screen
  SPY_PROXY_URL     optional proxy used ONLY for SPY_HOBBY_URL
  ORIGIN_SECRET     (>= 24 chars) when set, every request must carry X-Origin-Auth with this
                    value (added by a Cloudflare rule) or it is refused; the caller address is
                    then taken from CF-Connecting-IP. /health from loopback is always answered.
  RATE_PER_MIN      per-address request limit, default 30 (1..10000)
  CALC_PER_SEC      global ceiling on fresh (uncached) calculations per second, default 10 (1..1000)
  SKY_NOTICE        optional display-only text sent to every dial

Two background threads: a ticker (engine.tick every TRACK_STEP_S) and a fetcher (downloads and
screen rebuilds), so a slow download can never stall the dials.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import math
import os
import re
import socket
import sys
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
MAX_CONNECTIONS = 64              # open connections; more are closed at accept time
SOCKET_TIMEOUT_S = 10
REQUEST_DEADLINE_S = 15           # a connection still open this long after accept is shut down
MIN_KEY_LEN = 24
WRONG_KEY_EXTRA_COST = 5          # a wrong key costs 6 units of the per-address allowance
WRONG_KEY_PER_MIN = 30            # server-wide: wrong keys compared per minute, all callers
LIMITER_MAX_KEYS = 50000          # per-address table size per minute; newcomers then share one bucket
CUT_MAX_CHARS = 2
FETCH_EVERY_S = 60                # at most one download per minute (staggers the CelesTrak groups)
REBUILD_EVERY_S = 3600            # rebuild hourly anyway, so ageing orbits drop out
PRIVATE_SOURCES = ("military", S.HOBBY_SOURCE)
ROUTES = ("/v1/sky", "/health")
_DECIMAL = re.compile(r"-?[0-9]{1,3}(\.[0-9]{1,8})?")
_WHOLE = re.compile(r"[0-9]{1,6}")
NUMERIC_SETTINGS = (("RATE_PER_MIN", 1, 10000), ("CALC_PER_SEC", 1, 1000), ("PORT", 1, 65535))

NO_STORE = {"Cache-Control": "private, no-store"}     # replies are time-critical: never cached


def limiter_key(addr: str) -> str:
    """The rate-limit bucket of a caller address: IPv4 as is, IPv6 by its /64 (one subscriber
    usually holds a whole /64), anything unparsable in one shared bucket."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "invalid"
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip.exploded}/64", strict=False))
    return str(ip)


class Limiter:
    """Fixed-window counters: per-key per minute (at most max_keys keys a minute; newcomers past
    that share the "overflow" bucket), one global per second, and a server-wide budget of wrong
    keys per minute."""

    def __init__(self, per_min, per_sec, clock=time.time, max_keys=LIMITER_MAX_KEYS,
                 wrong_key_per_min=WRONG_KEY_PER_MIN):
        self.per_min, self.per_sec, self.clock = per_min, per_sec, clock
        self.max_keys, self.wrong_key_per_min = max_keys, wrong_key_per_min
        self._lock = threading.Lock()
        self._min, self._min_start = {}, 0
        self._sec, self._sec_start = 0, 0
        self._wrong, self._wrong_start = 0, 0

    def allow_key(self, key, cost=1) -> bool:
        with self._lock:
            w = int(self.clock() // 60)
            if w != self._min_start:
                self._min, self._min_start = {}, w
            if key not in self._min and len(self._min) >= self.max_keys:
                key = "overflow"
            self._min[key] = self._min.get(key, 0) + cost
            return self._min[key] <= self.per_min

    def tracked(self) -> int:
        with self._lock:
            return len(self._min)

    def allow_global(self) -> bool:
        with self._lock:
            w = int(self.clock())
            if w != self._sec_start:
                self._sec, self._sec_start = 0, w
            self._sec += 1
            return self._sec <= self.per_sec

    def reserve_key_check(self) -> bool:
        """Take one unit of this minute's wrong-key budget before comparing a key; False when
        the budget is used up (then the key must not be compared at all)."""
        with self._lock:
            w = int(self.clock() // 60)
            if w != self._wrong_start:
                self._wrong, self._wrong_start = 0, w
            if self._wrong >= self.wrong_key_per_min:
                return False
            self._wrong += 1
            return True

    def refund_key_check(self):
        """The key was right: give the unit back (only wrong keys use the budget)."""
        with self._lock:
            self._wrong = max(0, self._wrong - 1)


class BadRequest(Exception):
    pass


def _coordinate(q, name) -> float:
    try:
        raw = q[name][0]
    except (KeyError, IndexError):
        raise BadRequest("lat and lon are required numbers") from None
    if not _DECIMAL.fullmatch(raw):          # ASCII digits only: no "1_0", " 1", "1e1", "nan"
        raise BadRequest("lat and lon are required numbers")
    return float(raw)


def parse_request(query: str):
    """-> (lat, lon, [screen names], {screen: cut}). Raises BadRequest."""
    try:
        q = parse_qs(query, keep_blank_values=True, max_num_fields=8)
    except ValueError:
        raise BadRequest("too many fields") from None
    lat, lon = _coordinate(q, "lat"), _coordinate(q, "lon")
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
        if (name not in cuts or not 1 <= len(val) <= CUT_MAX_CHARS      # length first: int() of
                or not (val.isascii() and val.isdigit())               # 5000 digits raises
                or not S.CUT_MIN <= int(val) <= S.CUT_MAX):
            raise BadRequest("bad cut")
        cuts[name] = int(val)
    return round(lat, 1) + 0.0, round(lon, 1) + 0.0, names, cuts


def route_label(path_qs: str) -> str:
    """What a log line may say about the target: a fixed label, never the caller's text."""
    try:
        path = urlsplit(path_qs).path
    except ValueError:
        return "other"
    return path if path in ROUTES else "other"


SLOW_DOWN = (429, {"error": "slow down", "poll": BUSY_POLL_S}, {"Retry-After": "60"})


class App:
    def __init__(self, engine, store, env, clock=time.time):
        self.engine, self.store, self.clock = engine, store, clock
        self.spy_key = env.get("SKY_SPY_KEY", "")
        self.origin_secret = env.get("ORIGIN_SECRET", "")
        self.notice = env.get("SKY_NOTICE", "")[:120]
        self.limiter = Limiter(int(env.get("RATE_PER_MIN", "30")), int(env.get("CALC_PER_SEC", "10")), clock,
                               max_keys=LIMITER_MAX_KEYS, wrong_key_per_min=WRONG_KEY_PER_MIN)

    def key_ok(self, auth_header: str) -> bool:
        given = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        return bool(self.spy_key) and bool(given) and hmac.compare_digest(given.encode(), self.spy_key.encode())

    def handle(self, path_qs: str, headers: dict, peer_ip: str):
        """-> (status, body dict, extra headers). headers: lower-cased names."""
        status, body, extra = self._handle(path_qs, headers, peer_ip)
        return status, body, dict(extra, **NO_STORE)

    def _key(self, headers, ip) -> str:
        """"none" (no Authorization header), "ok", "wrong", or "throttled" (this minute's
        server-wide wrong-key budget is used up, so the key was not compared)."""
        auth = headers.get("authorization", "")
        if not auth:
            return "none"
        if not self.limiter.reserve_key_check():
            return "throttled"
        if self.key_ok(auth):
            self.limiter.refund_key_check()
            return "ok"
        self.limiter.allow_key(ip, cost=WRONG_KEY_EXTRA_COST)     # and 6x faster for this caller
        return "wrong"

    def _handle(self, path_qs, headers, peer_ip):
        try:
            url = urlsplit(path_qs)
            path, query = url.path, url.query
        except ValueError:                                     # e.g. "http://[x/..."
            path, query = None, ""
        local_health = path == "/health" and peer_ip in ("127.0.0.1", "::1")    # container health check
        if self.origin_secret and not local_health:
            if not hmac.compare_digest(headers.get("x-origin-auth", "").encode(), self.origin_secret.encode()):
                return 403, {"error": "forbidden"}, {}
            ip = limiter_key(headers.get("cf-connecting-ip", peer_ip))
        else:
            ip = limiter_key(peer_ip)
        now = self.clock()
        age = self.engine.tick_age(now)
        stalled = age > STALE_TICK_S
        if path == "/health":
            # Charged to the per-address limit like every route (one budget, no separate one);
            # only the container health check (loopback, no key) is exempt.
            if (not local_health or headers.get("authorization")) and not self.limiter.allow_key(ip):
                return SLOW_DOWN
            trusted = False                            # only the key unlocks the full body
            if headers.get("authorization"):
                key = self._key(headers, ip)
                if key == "throttled":
                    return SLOW_DOWN
                trusted = key == "ok"
            sources = self.store.health(now)
            dropped = dict(self.engine.dropped)
            if not trusted:                            # the public must not learn the spy screen is on
                sources = {k: v for k, v in sources.items() if k not in PRIVATE_SOURCES}
                dropped = {k: v for k, v in dropped.items() if k not in PRIVATE_SOURCES}
            body = {"ok": not stalled, "tick_age_s": round(age, 1) if age != float("inf") else -1,
                    "edge_locked": bool(self.origin_secret), "dropped_old": dropped, "sources": sources}
            return (503 if stalled else 200), body, {}
        if not self.limiter.allow_key(ip):             # unknown paths are charged too
            return SLOW_DOWN
        if path is None:
            return 400, {"error": "bad request target"}, {}
        if path != "/v1/sky":
            return 404, {"error": "not found"}, {}
        try:
            lat, lon, names, cuts = parse_request(query)
        except BadRequest as e:
            return 400, {"error": str(e)}, {}
        key = self._key(headers, ip)
        if key == "throttled":
            return SLOW_DOWN
        keyed = key == "ok"
        visible = {n: cuts[n] for n in names if keyed or not S.SCREENS[n]["private"]}
        # Approximate: a tick can land between is_cached and query_many, so a request counted as
        # cached may calculate after all. The ceiling bounds load; it is not an exact count.
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


class _Watchdog:
    """One thread for all connections: shuts down any socket still registered deadline_s after
    it was added. Never raises into, or blocks, the handler threads."""

    def __init__(self, deadline_s):
        self.deadline_s = deadline_s
        self._cv = threading.Condition()
        self._due = {}            # socket -> time.monotonic() deadline
        self._cut = set()         # sockets shut down by the watchdog, until removed
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="sky-deadline", daemon=True)
        self._thread.start()

    def add(self, sock):
        with self._cv:
            self._due[sock] = time.monotonic() + self.deadline_s
            self._cv.notify()

    def remove(self, sock):
        with self._cv:
            self._due.pop(sock, None)
            self._cut.discard(sock)

    def was_cut(self, sock) -> bool:
        with self._cv:
            return sock in self._cut

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify()
        self._thread.join(timeout=5)

    def _run(self):
        with self._cv:
            while not self._stopped:
                now = time.monotonic()
                for sock, due in list(self._due.items()):
                    if due <= now:
                        del self._due[sock]
                        self._cut.add(sock)
                        try:
                            sock.shutdown(socket.SHUT_RDWR)       # wakes a blocked read or write
                        except OSError:
                            pass
                nxt = min(self._due.values(), default=None)
                self._cv.wait(None if nxt is None else max(0.0, nxt - now))


class SkyServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that bounds connections and threads: at most max_connections open
    (a further connection is closed at accept time, before any thread starts) and each one shut
    down deadline_s after accept, whatever it is doing (a client trickling header bytes never
    trips the per-read socket timeout)."""

    def __init__(self, address, handler, max_connections=None, deadline_s=None):
        self.max_connections = MAX_CONNECTIONS if max_connections is None else max_connections
        self.deadline_s = REQUEST_DEADLINE_S if deadline_s is None else deadline_s
        self._conn_lock = threading.Lock()
        self._open = 0
        super().__init__(address, handler)
        self._watchdog = _Watchdog(self.deadline_s)

    def open_connections(self) -> int:
        with self._conn_lock:
            return self._open

    def was_cut(self, sock) -> bool:
        return self._watchdog.was_cut(sock)

    def process_request(self, request, client_address):
        with self._conn_lock:
            full = self._open >= self.max_connections
            if not full:
                self._open += 1
        if full:
            self.shutdown_request(request)             # closed without a reply or a thread
            return
        self._watchdog.add(request)
        try:
            super().process_request(request, client_address)      # starts the handler thread
        except BaseException:
            self._done(request)                        # the base server closes the socket
            raise

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self._done(request)                        # before close: the watchdog never sees it closed
            self.shutdown_request(request)

    def _done(self, request):
        self._watchdog.remove(request)
        with self._conn_lock:
            self._open -= 1

    def handle_error(self, request, client_address):
        """No traceback and no caller address in the log: the exception type only."""
        print(f"[http] connection error: {sys.exc_info()[0].__name__}", flush=True)

    def server_close(self):
        super().server_close()
        watchdog = getattr(self, "_watchdog", None)    # absent when the bind in __init__ failed
        if watchdog is not None:
            watchdog.stop()                            # idempotent


def make_handler(app, slots=None):
    slots = threading.BoundedSemaphore(MAX_IN_PROGRESS) if slots is None else slots

    class Handler(BaseHTTPRequestHandler):
        server_version = "sky-service"
        sys_version = ""
        protocol_version = "HTTP/1.0"                 # one request per connection
        timeout = SOCKET_TIMEOUT_S                    # a silent client is dropped

        def log_message(self, fmt, *args):            # never log the query string (location)
            pass

        def _was_cut(self) -> bool:
            was_cut = getattr(self.server, "was_cut", None)
            return bool(was_cut and was_cut(self.request))

        def _send(self, status, raw, extra, body=True):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(raw)

        def send_error(self, code, message=None, explain=None):
            """Replies http.server makes itself (bad request line, 414, 431, 501, our 405s):
            short generic JSON, never cached, never echoing what the caller sent."""
            self.close_connection = True
            if self._was_cut():
                return
            if self.request_version in ("", "HTTP/0.9"):    # so the status line and headers are sent
                self.request_version = "HTTP/1.0"
            raw = json.dumps({"error": self.responses.get(code, ("Error",))[0]}).encode()
            extra = dict(NO_STORE, **({"Allow": "GET"} if code == 405 else {}))
            self._send(code, raw, extra, body=self.command != "HEAD" and code >= 200 and code not in (204, 304))
            print(f"[http] {code} {route_label(getattr(self, 'path', '') or '')}", flush=True)

        def _refuse_method(self):
            self.send_error(405)

        do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = _refuse_method

        def _answer(self):
            if not slots.acquire(blocking=False):
                return 503, {"error": "busy", "poll": BUSY_POLL_S}, dict(NO_STORE, **{"Retry-After": "30"})
            try:
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                return app.handle(self.path, hdrs, self.client_address[0])
            finally:
                slots.release()

        def do_GET(self):
            self.close_connection = True
            if self._was_cut():                       # deadline passed while headers were read
                return
            if self.request_version == "HTTP/0.9":    # a 0.9 reply has no headers: no no-store
                self.send_error(400)
                return
            route = route_label(self.path)
            try:
                status, body, extra = self._answer()
                raw = json.dumps(body, separators=(",", ":")).encode()
                if len(raw) > MAX_BODY:
                    status, raw = 500, b'{"error":"reply too large"}'
            except Exception as e:                    # never a traceback, path, query or address
                print(f"[http] handler error: {type(e).__name__}", flush=True)
                status, raw, extra = 500, b'{"error":"internal error"}', dict(NO_STORE)
            self._send(status, raw, extra)
            print(f"[http] {status} {route}", flush=True)
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
    print(f"[sky] background error: {type(e).__name__}", flush=True)      # type only, never the message


def tick_once(engine, now):
    """One pass of the ticker thread: never touches the network or the disk."""
    try:
        engine.tick(now)
    except Exception as e:                          # keep ticking; /health shows a stall
        _log_error(e)


def new_fetch_state() -> dict:
    return {"last_fetch": 0.0, "last_rebuild": 0.0}


def start_screens(engine, store, env, hobby_on, now) -> dict:
    """Build the screens from the disk copies before any download; -> the fetcher's state.
    If that fails, log the exception type and start with no screens: last_rebuild stays 0, so
    the fetcher rebuilds on its first pass."""
    state = new_fetch_state()
    try:
        rebuild(engine, store, env, hobby_on, now)
        state["last_rebuild"] = now
    except Exception as e:
        print(f"[sky] start-up build failed: {type(e).__name__}; starting with no screens", flush=True)
    return state


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
    origin = env.get("ORIGIN_SECRET", "")
    if origin and len(origin) < MIN_KEY_LEN:
        raise SystemExit(f"ORIGIN_SECRET must be at least {MIN_KEY_LEN} characters")
    for name, lo, hi in NUMERIC_SETTINGS:
        if name in env:
            val = env[name]
            if not (_WHOLE.fullmatch(val) and lo <= int(val) <= hi):
                raise SystemExit(f"{name} must be a whole number from {lo} to {hi}")
    warn = []
    if not origin:
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
    state = start_screens(engine, store, env, hobby_on, now)    # screens from the disk copies first
    engine.tick(now)
    stop = threading.Event()
    threading.Thread(target=_ticker, args=(engine, stop), name="sky-ticker", daemon=True).start()
    threading.Thread(target=_fetcher, args=(state, engine, store, env, hobby_on, stop),
                     name="sky-fetcher", daemon=True).start()
    port = int(env.get("PORT", "8080"))
    print(f"[sky] listening on :{port}; spy screen {'on' if env.get('SKY_SPY_KEY') else 'off'}, "
          f"extra source {'on' if hobby_on else 'off'}", flush=True)
    SkyServer(("0.0.0.0", port), make_handler(App(engine, store, env))).serve_forever()


if __name__ == "__main__":
    main()
