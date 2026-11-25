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

Downloads (requests_get): the caller is never delayed past TOTAL_TIMEOUT_S. Known limit: an abandoned
worker stuck in DNS resolution, TCP connect, the TLS handshake or a trickled header read may not be
wakeable and can linger; its result is discarded, its memory is bounded by the size cap, and the schedule
(at most one attempt per source per hour) bounds how many can accumulate.

Logs and health() carry only fixed strings, HTTP status codes, exception TYPE names and the fixed
messages of elements.ElementError: never a URL, a proxy string or foreign exception text.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import tempfile
import threading
import time

import elements

CELESTRAK_INTERVAL_S = 2 * 3600      # GP data changes a few times a day; 2 h is polite
HOBBY_INTERVAL_S = 24 * 3600
BACKOFF_MIN_S = 3600
BACKOFF_MAX_S = 24 * 3600
TIMEOUT_S = 60                       # connect timeout, and the longest wait for any single read
TOTAL_TIMEOUT_S = 120                # the whole download (DNS, connect, TLS, headers, body), enforced by
                                     # waiting for a worker thread at most this long
CLOCK_SKEW_S = 300                   # saved times later than now + this are not believed
_CHUNK = 64 * 1024
_STATE_MAX = 4096                    # a state file larger than this is corrupt
_HEADER_MAX = 1024
_NAME = re.compile(r"[a-z0-9_-]{1,32}")


class DownloadError(Exception):
    """requests_get gave up on a download."""


class DownloadTooLarge(DownloadError):
    pass


class DownloadTimeout(DownloadError):
    pass


class DownloadEncoded(DownloadError):
    """The server sent a compressed body although identity was asked for."""


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
        iv = _finite(interval_s)
        if iv is None or iv < BACKOFF_MIN_S:      # never more than one request per hour per source
            raise ValueError("source interval must be a finite number of seconds >= BACKOFF_MIN_S")
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
                # The data file is newer than the schedule (a crash between the two writes): the saved
                # status describes an older attempt, so it no longer applies.
                newer = data_fetched is not None and st is not None and (st_fetched is None or data_fetched > st_fetched)
                if st is None or newer or src.last.startswith("ok") or src.last == "never fetched":
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

    No redirects, no proxy or credentials from the environment, TLS verified, body requested
    uncompressed (a Content-Encoding other than identity is refused). The download runs in a daemon
    worker thread; the caller gets control back within TOTAL_TIMEOUT_S whatever the network does (DNS,
    connect, TLS, headers, chunk framing, body), with DownloadTimeout. At the deadline the caller only marks
    the download abandoned and raises; a separate reaper thread then tries to wake the worker (shutting the
    response's socket) and close its response and session, and the worker's result is thrown away. Raises DownloadTooLarge
    past elements.MAX_BYTES (never more than MAX_BYTES + 1 bytes are held); the response is always closed.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("only https URLs are fetched")
    job = _Download(url, headers, proxy, min(timeout, TOTAL_TIMEOUT_S))
    job.thread.start()
    if not job.finished.wait(TOTAL_TIMEOUT_S) and job.abandon():
        raise DownloadTimeout()
    if job.error is not None:
        raise job.error
    return job.result


class _Download:
    """One download in a daemon thread. Hands its outcome over only if the caller is still waiting."""

    def __init__(self, url, headers, proxy, read_timeout):
        self.url, self.proxy, self.t = url, proxy, read_timeout
        self.headers = {k: v for k, v in (headers or {}).items() if k.lower() != "accept-encoding"}
        self.headers["Accept-Encoding"] = "identity"
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.abandoned = False
        self.session = self.response = None
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, name="store-download", daemon=True)

    def _run(self):
        try:
            res, err = self._fetch(), None
        except BaseException as e:      # handed to the caller; never escapes the thread
            res, err = None, e
        with self.lock:
            if self.abandoned:
                return                  # too late: nobody gets this result
            self.result, self.error = res, err
            self.finished.set()

    def abandon(self) -> bool:
        """Give up on the download. False if it finished first (its outcome then stands).

        Runs on the caller's thread, so it only marks the job abandoned (under the job's own lock, never held
        across I/O) and hands the wake-up to a reaper thread: nothing here touches the response, the session,
        a socket or any lock the worker may hold while it is blocked in a read.
        """
        with self.lock:
            if self.finished.is_set():
                return False
            self.abandoned = True
            r, s = self.response, self.session
        try:
            threading.Thread(target=_reap, args=(r, s), name="store-download-reaper", daemon=True).start()
        except BaseException:           # no reaper (thread limit): the worker still stops at its next check
            pass
        return True

    def _keep(self, attr, obj):
        """Record the session / response so abandon() can close it; stop at once if already abandoned."""
        with self.lock:
            setattr(self, attr, obj)
            if self.abandoned:
                raise DownloadTimeout()

    def _fetch(self):
        import requests
        with requests.Session() as s:
            s.trust_env = False
            self._keep("session", s)
            r = s.get(self.url, headers=self.headers, timeout=(self.t, self.t),
                      proxies={"https": self.proxy} if self.proxy else {},
                      allow_redirects=False, verify=True, stream=True)
            try:
                self._keep("response", r)
                if r.status_code != 200:
                    return r.status_code, b""
                enc = (r.headers.get("Content-Encoding") or "identity").strip().lower()
                if enc != "identity":
                    raise DownloadEncoded()
                buf = bytearray()
                while True:
                    if self.abandoned:
                        raise DownloadTimeout()
                    chunk = r.raw.read(min(_CHUNK, elements.MAX_BYTES + 1 - len(buf)), decode_content=False)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > elements.MAX_BYTES:
                        raise DownloadTooLarge()
                if self.abandoned:
                    raise DownloadTimeout()
                return 200, bytes(buf)
            finally:
                r.close()


def _reap(r, s):
    """Best effort, on its own daemon thread: wake the abandoned worker's blocked read, then close its
    response and session. If any step blocks, only this thread waits; nothing it raises escapes."""
    if r is not None:
        _quietly(_wake, r)
        _quietly(r.close)
    if s is not None:
        _quietly(s.close)


def _wake(r):
    """Shut the response's socket for reading so a blocked recv returns. urllib3's own hook first: it keeps
    the socket even when http.client has set connection.sock to None (Connection: close responses)."""
    raw = getattr(r, "raw", None)
    shutdown = getattr(raw, "shutdown", None)
    if callable(shutdown) and _quietly(shutdown):
        return
    sock = getattr(getattr(raw, "connection", None), "sock", None)
    shutdown = getattr(sock, "shutdown", None)       # an SSLTransport (TLS in TLS) has none
    if callable(shutdown):
        _quietly(shutdown, socket.SHUT_RDWR)


def _quietly(fn, *args) -> bool:
    """Call fn; True if it returned, False if it raised anything (which is dropped, never logged)."""
    try:
        fn(*args)
        return True
    except BaseException:
        return False
