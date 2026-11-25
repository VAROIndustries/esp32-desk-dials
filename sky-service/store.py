"""Download, validate and keep orbital elements.

Rules this module enforces (CelesTrak usage policy, celestrak.org/usage-policy.php):
  - one group per request, each group at most once per hour;
  - on ANY non-200 reply, stop asking for that source and back off (1 h doubling to 24 h);
  - never retry in a loop.

Two files per source, in data_dir:
  <name>.dat         the last good download: one JSON header line {"fetched_at": <unix>, "bytes": <n>},
                     a newline, then the body exactly as received. Written only after the body passed
                     validation, through a temp file that is fsynced and then renamed over the old one,
                     so it is always the complete old copy or the complete new one. A header that is
                     missing or malformed, or a body of the wrong length, makes the file corrupt.
  <name>.state.json  the schedule: fetched_at, next_try, backoff, last, last_attempt (written the same way).

The attempt record: BEFORE every request, refresh() sets last_attempt = now and pushes next_try to at
least now + BACKOFF_MIN_S, and saves that; the outcome then updates and saves it again. If the record
cannot be saved, no request is made. On start, load_disk() schedules the next request at the latest of
(saved next_try, fetched_at + interval, last_attempt + BACKOFF_MIN_S), so neither a restart nor a crash
in the middle of a download leads to an early request.

Logs and health() carry only fixed strings, HTTP status codes, exception TYPE names and the fixed
messages of elements.ElementError: never a URL, a proxy string or foreign exception text.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time

import elements

CELESTRAK_INTERVAL_S = 2 * 3600      # GP data changes a few times a day; 2 h is polite
HOBBY_INTERVAL_S = 24 * 3600
BACKOFF_MIN_S = 3600
BACKOFF_MAX_S = 24 * 3600
TIMEOUT_S = 60                       # connect timeout, and the longest wait for any single read
TOTAL_TIMEOUT_S = 120                # the whole download: connect + every read
CLOCK_SKEW_S = 300                   # saved times later than now + this are not believed
_CHUNK = 64 * 1024
_STATE_MAX = 4096                    # a state file larger than this is corrupt
_HEADER_MAX = 1024
_NAME = re.compile(r"[a-z0-9_-]{1,32}")
_clock = time.monotonic              # replaced in tests


class DownloadError(Exception):
    """requests_get gave up on a download."""


class DownloadTooLarge(DownloadError):
    pass


class DownloadTimeout(DownloadError):
    pass


class Source:
    """One element file kept fresh.

    elems: the current elements ([] until the first good copy).
    fetched_at: unix time the current elements were downloaded, or None when never fetched or unknown.
    next_try: no request is made before this unix time. backoff: current back-off in seconds (0 = none).
    last: short status for health(), fixed text only. last_attempt: unix time the last request started,
    or None.
    """

    def __init__(self, name, url, kind, interval_s, proxy=None):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("source name must match [a-z0-9_-]{1,32}")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("source URL must start with https://")
        self.name, self.url, self.kind, self.interval_s, self.proxy = name, url, kind, interval_s, proxy
        self.elems = []
        self.fetched_at = None
        self.next_try = 0.0
        self.backoff = 0
        self.last = "never fetched"
        self.last_attempt = None


def _finite(v):
    """A JSON number as float, or None (bools, strings, NaN and infinities are not numbers here)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = float(v)
        if math.isfinite(v):
            return v
    return None


def _past(v, now):
    """A believable time in the past (small clock skew allowed), or None."""
    v = _finite(v)
    return v if v is not None and v <= now + CLOCK_SKEW_S else None


class Store:
    def __init__(self, data_dir, sources, http_get, user_agent, log=print):
        """http_get(url, headers, proxy, timeout) -> (status_code, body_bytes)."""
        names = [s.name for s in sources]
        if len(set(names)) != len(names):
            raise ValueError("duplicate source name")
        self.dir, self.http_get, self.ua, self.log = data_dir, http_get, user_agent, log
        self.sources = {s.name: s for s in sources}
        os.makedirs(data_dir, exist_ok=True)

    def _path(self, name, ext):
        return os.path.join(self.dir, name + ext)

    def _parse(self, src, raw):
        return elements.parse_omm_json(raw) if src.kind == "omm" else elements.parse_tle_text(raw)

    # --- disk ---------------------------------------------------------------------------------

    def _atomic_write(self, path, data: bytes):
        """Replace path with data: unique temp file in the same directory, fsync, rename."""
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:                                     # make the rename itself durable (best effort)
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _save_state(self, src):
        state = {"fetched_at": src.fetched_at, "next_try": src.next_try, "backoff": src.backoff,
                 "last": src.last, "last_attempt": src.last_attempt}
        self._atomic_write(self._path(src.name, ".state.json"), json.dumps(state).encode())

    def _persist(self, src):
        try:
            self._save_state(src)
        except Exception as e:
            self.log(f"[store] {src.name}: schedule not saved ({type(e).__name__})")

    def _read_state(self, src):
        try:
            with open(self._path(src.name, ".state.json"), "rb") as f:
                raw = f.read(_STATE_MAX + 1)
            if len(raw) > _STATE_MAX:
                raise ValueError("too large")
            st = json.loads(raw)
            if not isinstance(st, dict):
                raise ValueError("not an object")
            return st
        except FileNotFoundError:
            return None
        except Exception as e:
            self.log(f"[store] {src.name}: schedule file unusable ({type(e).__name__})")
            return None

    def _read_data(self, src):
        """(elements, raw header fetched_at) from <name>.dat, or None if missing or corrupt."""
        try:
            with open(self._path(src.name, ".dat"), "rb") as f:
                raw = f.read(elements.MAX_BYTES + _HEADER_MAX + 1)
            head, sep, body = raw.partition(b"\n")
            if not sep or len(head) > _HEADER_MAX:
                raise ValueError("no header")
            h = json.loads(head)
            if not isinstance(h, dict):
                raise ValueError("bad header")
            fetched_at, nbytes = h["fetched_at"], h["bytes"]
            if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
                raise ValueError("bad header")
            if not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes != len(body):
                raise ValueError("length mismatch")
            return self._parse(src, body), fetched_at
        except FileNotFoundError:
            return None
        except Exception as e:      # corrupt copy on disk: ignore it
            self.log(f"[store] {src.name}: disk copy unusable ({type(e).__name__})")
            return None

    def load_disk(self, now=None):
        """Restore elements and schedule after a restart. Nothing is fetched earlier than allowed."""
        now = time.time() if now is None else now
        for src in self.sources.values():
            st = self._read_state(src)
            data = self._read_data(src)
            st_fetched = st_attempt = st_next = None
            if st is not None:
                st_fetched = _past(st.get("fetched_at"), now)
                st_attempt = _past(st.get("last_attempt"), now)
                st_next = _finite(st.get("next_try"))
                if st_next is not None:
                    st_next = min(st_next, now + BACKOFF_MAX_S)
                b = _finite(st.get("backoff"))
                src.backoff = int(min(max(b, 0.0), BACKOFF_MAX_S)) if b is not None else 0
                last = st.get("last")
                if isinstance(last, str) and 0 < len(last) <= 200 and last.isprintable():
                    src.last = last
            data_fetched = None
            if data is not None:
                src.elems, raw_t = data
                data_fetched = _past(raw_t, now)
                src.fetched_at = data_fetched
                if st is None or src.last.startswith("ok") or src.last == "never fetched":
                    src.last = "loaded from disk"
                # The data file is newer than the last attempt record: that attempt succeeded.
                if data_fetched is not None and st_attempt is not None and data_fetched >= st_attempt:
                    src.backoff = 0
            elif st is not None and src.last.startswith("ok"):
                src.last = "no usable copy on disk"
            src.last_attempt = st_attempt
            times = [src.next_try]
            if st_next is not None:
                times.append(st_next)
            times += [t + src.interval_s for t in (data_fetched, st_fetched) if t is not None]
            if st_attempt is not None:
                times.append(st_attempt + BACKOFF_MIN_S)
            src.next_try = max(times)

    # --- schedule -----------------------------------------------------------------------------

    def due(self, now):
        """Sources ready to fetch, most overdue first."""
        return sorted((s for s in self.sources.values() if now >= s.next_try), key=lambda s: s.next_try)

    def refresh(self, src, now) -> bool:
        """Fetch one source once. Returns True when new elements were accepted."""
        src.last_attempt = now
        src.next_try = max(src.next_try, now + BACKOFF_MIN_S)
        try:
            self._save_state(src)          # the attempt record, before any request
        except Exception as e:
            return self._fail(src, now, f"disk error: {type(e).__name__}")
        try:
            status, body = self.http_get(src.url, {"User-Agent": self.ua}, src.proxy, TIMEOUT_S)
        except Exception as e:
            return self._fail(src, now, f"network error: {type(e).__name__}")
        if status != 200:
            return self._fail(src, now, f"HTTP {status}")
        try:
            elems = self._parse(src, body)
        except elements.ElementError as e:      # fixed messages from elements.py
            return self._fail(src, now, f"rejected: {str(e)[:120]}")
        except Exception as e:      # any other parser failure is a rejected file, never an escape from back-off
            return self._fail(src, now, f"rejected: {type(e).__name__}")
        src.elems, src.fetched_at, src.backoff = elems, now, 0
        src.next_try = now + src.interval_s
        src.last = f"ok, {len(elems)} objects"
        try:
            header = json.dumps({"fetched_at": now, "bytes": len(body)}).encode()
            self._atomic_write(self._path(src.name, ".dat"), header + b"\n" + body)
        except Exception as e:      # keep the validated download in memory; the old file stays intact
            src.last += f" (not saved: {type(e).__name__})"
            self.log(f"[store] {src.name}: {src.last}")
        self._persist(src)
        return True

    def _fail(self, src, now, why) -> bool:
        src.backoff = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, src.backoff * 2))
        src.next_try = now + src.backoff
        src.last = why
        self.log(f"[store] {src.name}: {why}; keeping last good copy, next try in {src.backoff // 60} min")
        self._persist(src)
        return False

    def age_h(self, name, now) -> float:
        s = self.sources.get(name)
        if s is None or s.fetched_at is None:
            return -1.0
        return max(0.0, (now - s.fetched_at) / 3600.0)

    def health(self, now) -> dict:
        return {n: {"objects": len(s.elems), "age_h": round(self.age_h(n, now), 1), "last": s.last}
                for n, s in self.sources.items()}


def requests_get(url, headers, proxy, timeout):
    """GET one https URL. Returns (status, body); the body is read only for a 200.

    No redirects, no proxy or credentials from the environment, TLS verified. Raises DownloadTooLarge
    past elements.MAX_BYTES and DownloadTimeout past TOTAL_TIMEOUT_S; the response is always closed.
    """
    import requests
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("only https URLs are fetched")
    deadline = _clock() + TOTAL_TIMEOUT_S
    t = min(timeout, TOTAL_TIMEOUT_S)
    with requests.Session() as s:
        s.trust_env = False
        r = s.get(url, headers=headers, timeout=(t, t), proxies={"https": proxy} if proxy else {},
                  allow_redirects=False, verify=True, stream=True)
        try:
            if r.status_code != 200:
                return r.status_code, b""
            buf = bytearray()
            while True:
                left = deadline - _clock()
                if left <= 0:
                    raise DownloadTimeout()
                _limit_next_read(r, min(t, left))
                chunk = r.raw.read1(_CHUNK, decode_content=True)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > elements.MAX_BYTES:
                    raise DownloadTooLarge()
            if _clock() > deadline:
                raise DownloadTimeout()
            return 200, bytes(buf)
        finally:
            r.close()


def _limit_next_read(r, seconds):
    """Shorten the socket timeout so a single read cannot run past the total deadline."""
    sock = getattr(getattr(r.raw, "connection", None), "sock", None)
    if sock is not None:
        try:
            sock.settimeout(max(0.01, seconds))
        except OSError:
            pass
