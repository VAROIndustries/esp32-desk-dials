import json
import math

import pytest

import elements
import store as store_mod
from conftest import omm
from store import BACKOFF_MAX_S, BACKOFF_MIN_S, Source, Store

GOOD = json.dumps([omm(25544, "ISS")]).encode()


class FakeHttp:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, proxy, timeout):
        self.calls.append((url, headers, proxy))
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def _store(tmp_path, http, log=None, **kw):
    src = Source("stations", "https://example.test/stations", "omm", 7200, **kw)
    return Store(str(tmp_path), [src], http, "UA/1 (me@example.test)", log=log or (lambda m: None)), src


def _dat(fetched_at, body, nbytes=None):
    head = json.dumps({"fetched_at": fetched_at, "bytes": len(body) if nbytes is None else nbytes})
    return head.encode() + b"\n" + body


def test_good_fetch_is_accepted_persisted_and_scheduled(tmp_path):
    http = FakeHttp([(200, GOOD)])
    store, src = _store(tmp_path, http)
    assert store.due(1000.0) == [src]
    assert store.refresh(src, 1000.0) is True
    assert len(src.elems) == 1 and src.next_try == 1000.0 + 7200 and store.due(1000.0 + 7199) == []
    assert http.calls[0][1] == {"User-Agent": "UA/1 (me@example.test)"} and http.calls[0][2] is None
    fresh, src2 = _store(tmp_path, FakeHttp([]))
    fresh.load_disk()
    assert len(src2.elems) == 1 and src2.fetched_at == 1000.0 and fresh.due(1000.0 + 7199) == []


def test_non_200_keeps_last_good_copy_and_backs_off_doubling(tmp_path):
    http = FakeHttp([(200, GOOD), (403, b"blocked"), (503, b""), RuntimeError("dns")] + [(500, b"")] * 6)
    store, src = _store(tmp_path, http)
    store.refresh(src, 0.0)
    assert store.refresh(src, 8000.0) is False
    assert len(src.elems) == 1 and src.last == "HTTP 403" and src.next_try == 8000.0 + BACKOFF_MIN_S
    assert store.due(8000.0 + BACKOFF_MIN_S - 1) == []           # no early retry
    store.refresh(src, 20000.0)
    assert src.next_try == 20000.0 + 2 * BACKOFF_MIN_S
    store.refresh(src, 40000.0)
    assert src.last.startswith("network error") and src.next_try == 40000.0 + 4 * BACKOFF_MIN_S
    for _ in range(6):
        store.refresh(src, 50000.0)
    assert src.backoff == BACKOFF_MAX_S and len(src.elems) == 1


def test_rejected_file_does_not_replace_good_copy_on_disk(tmp_path):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD), (200, b"<html>maintenance</html>")]))
    store.refresh(src, 0.0)
    assert store.refresh(src, 8000.0) is False and src.last.startswith("rejected")
    fresh, src2 = _store(tmp_path, FakeHttp([]))
    fresh.load_disk()
    assert len(src2.elems) == 1 and src2.fetched_at == 0.0


def test_success_after_failures_resets_backoff(tmp_path):
    store, src = _store(tmp_path, FakeHttp([(500, b""), (200, GOOD)]))
    store.refresh(src, 0.0)
    store.refresh(src, 4000.0)
    assert src.backoff == 0 and src.next_try == 4000.0 + 7200


def test_proxy_is_passed_only_for_the_source_that_has_one(tmp_path):
    http = FakeHttp([(200, GOOD)])
    store, src = _store(tmp_path, http, proxy="http://user:pw@proxy.test:1")
    store.refresh(src, 0.0)
    assert http.calls[0][2] == "http://user:pw@proxy.test:1"


def test_proxy_is_not_passed_to_the_other_source(tmp_path):
    http = FakeHttp([(200, GOOD), (200, GOOD)])
    a = Source("plain", "https://a.test/x", "omm", 7200)
    b = Source("proxied", "https://b.test/y", "omm", 7200, proxy="http://user:pw@proxy.test:1")
    store = Store(str(tmp_path), [a, b], http, "UA", log=lambda m: None)
    store.refresh(b, 0.0)
    store.refresh(a, 0.0)
    assert http.calls[0][0] == "https://b.test/y" and http.calls[0][2] == "http://user:pw@proxy.test:1"
    assert http.calls[1][0] == "https://a.test/x" and http.calls[1][2] is None


def test_health_reports_age_and_never_leaks_urls_or_proxy(tmp_path):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD)]), proxy="http://user:pw@proxy.test:1")
    store.refresh(src, 100.0)
    h = store.health(3700.0)
    assert h == {"stations": {"objects": 1, "age_h": 1.0, "last": "ok, 1 objects"}}
    assert "proxy" not in json.dumps(h) and "example.test" not in json.dumps(h)


def test_corrupt_disk_copy_is_ignored_and_does_not_end_backoff(tmp_path):
    # A data file whose header claims a different length is corrupt: no elements, no time.
    (tmp_path / "stations.dat").write_bytes(_dat(5.0, GOOD, nbytes=len(GOOD) + 1))
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=10.0)
    assert src.elems == [] and src.fetched_at is None and store.due(10.0) == [src]
    # The same corrupt copy beside a schedule that says "in back-off until 5000" must not cause a fetch.
    (tmp_path / "stations.state.json").write_text(json.dumps(
        {"fetched_at": None, "next_try": 5000.0, "backoff": 3600, "last": "HTTP 403", "last_attempt": 1400.0}))
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=1500.0)
    assert src.elems == [] and store.due(4999.0) == [] and store.due(5000.0) == [src]
    assert src.backoff == 3600 and src.last == "HTTP 403"


@pytest.mark.parametrize("raw", [b"", b"no header line", b'{"fetched_at": 5}\n' + GOOD,
                                 b'{"fetched_at": "x", "bytes": 3}\nabc', b"[1]\n" + GOOD, GOOD])
def test_data_file_without_a_valid_header_is_corrupt(tmp_path, raw):
    (tmp_path / "stations.dat").write_bytes(raw)
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=10.0)
    assert src.elems == [] and src.fetched_at is None


def test_logs_and_health_never_contain_exception_text_or_urls(tmp_path):
    logs = []
    src = Source("stations", "https://example.test/stations", "omm", 7200, proxy="http://user:pw@proxy.test:1")
    store = Store(str(tmp_path), [src], FakeHttp([RuntimeError("SECRET user:pw@proxy.test"), (200, b"{SECRET")]), "UA", log=logs.append)
    store.refresh(src, 0.0)
    store.refresh(src, 4000.0)
    (tmp_path / "stations.dat").write_bytes(_dat(5.0, b"{SECRET"))
    (tmp_path / "stations.state.json").write_text('{SECRET')
    store.load_disk(now=5000.0)
    text = json.dumps(store.health(5000.0)) + " ".join(logs)
    assert logs and "SECRET" not in text and "proxy" not in text and "example.test" not in text


def test_element_error_text_is_kept_other_exceptions_are_type_only(tmp_path, monkeypatch):
    store, src = _store(tmp_path, FakeHttp([(200, b"[]"), (200, GOOD)]))
    assert store.refresh(src, 0.0) is False and src.last == "rejected: no usable objects"
    monkeypatch.setattr(store, "_parse", lambda s, raw: (_ for _ in ()).throw(KeyError("SECRET")))
    assert store.refresh(src, 5000.0) is False and src.last == "rejected: KeyError"


def test_unexpected_parser_error_still_backs_off(tmp_path, monkeypatch):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD)]))
    monkeypatch.setattr(store, "_parse", lambda s, raw: (_ for _ in ()).throw(KeyError("x")))
    assert store.refresh(src, 0.0) is False and src.next_try == BACKOFF_MIN_S


# --- Finding 1: back-off and the attempt record survive restarts and crashes ---

def test_backoff_survives_restart(tmp_path):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD), (403, b"")]))
    store.refresh(src, 0.0)
    store.refresh(src, 7200.0)
    assert src.next_try == 7200.0 + BACKOFF_MIN_S
    fresh, src2 = _store(tmp_path, FakeHttp([(403, b"")]))
    fresh.load_disk(now=7201.0)
    assert fresh.due(7201.0) == [] and fresh.due(7200.0 + BACKOFF_MIN_S - 1) == []
    assert src2.next_try == 7200.0 + BACKOFF_MIN_S and src2.backoff == BACKOFF_MIN_S
    assert src2.last == "HTTP 403" and len(src2.elems) == 1 and src2.fetched_at == 0.0
    fresh.refresh(src2, 7200.0 + BACKOFF_MIN_S)
    assert src2.backoff == 2 * BACKOFF_MIN_S                     # doubling continues after restart


class Crash(BaseException):
    """Stands in for the process dying in the middle of a download."""


def test_attempt_is_recorded_before_the_request_so_a_crash_cannot_cause_an_early_retry(tmp_path):
    seen = {}

    def http(url, headers, proxy, timeout):
        seen.update(json.loads((tmp_path / "stations.state.json").read_text()))
        raise Crash()

    store, src = _store(tmp_path, http)
    with pytest.raises(Crash):
        store.refresh(src, 1000.0)
    assert seen["last_attempt"] == 1000.0 and seen["next_try"] >= 1000.0 + BACKOFF_MIN_S
    fresh, src2 = _store(tmp_path, FakeHttp([]))
    fresh.load_disk(now=1001.0)
    assert fresh.due(1000.0 + BACKOFF_MIN_S - 1) == [] and fresh.due(1000.0 + BACKOFF_MIN_S) == [src2]


def test_no_state_and_no_data_allows_an_immediate_first_fetch(tmp_path):
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=1000.0)
    assert store.due(1000.0) == [src]


def test_unwritable_state_means_no_request(tmp_path, monkeypatch):
    logs = []
    http = FakeHttp([(200, GOOD)])
    store, src = _store(tmp_path, http, log=logs.append)
    monkeypatch.setattr(store, "_atomic_write", lambda path, data: (_ for _ in ()).throw(OSError("full")))
    assert store.refresh(src, 1000.0) is False
    assert http.calls == [] and src.next_try == 1000.0 + BACKOFF_MIN_S and src.last == "disk error: OSError"
    assert logs and "full" not in " ".join(logs)


def test_restart_uses_latest_of_next_try_fetch_interval_and_last_attempt(tmp_path):
    (tmp_path / "stations.dat").write_bytes(_dat(1000.0, GOOD))
    (tmp_path / "stations.state.json").write_text(json.dumps(
        {"fetched_at": 1000.0, "next_try": 2000.0, "backoff": 0, "last": "ok, 1 objects", "last_attempt": 7000.0}))
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=7100.0)
    assert src.next_try == 7000.0 + BACKOFF_MIN_S and len(src.elems) == 1 and src.fetched_at == 1000.0


# --- Finding 2: one data file, written atomically ---

def test_data_and_fetch_time_live_in_one_file(tmp_path):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD)]))
    store.refresh(src, 1000.0)
    raw = (tmp_path / "stations.dat").read_bytes()
    head, _, body = raw.partition(b"\n")
    assert json.loads(head) == {"fetched_at": 1000.0, "bytes": len(GOOD)} and body == GOOD
    assert not (tmp_path / "stations.meta.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stations.dat", "stations.state.json"]


def test_success_after_a_crash_between_data_and_state_write_is_not_refetched(tmp_path):
    # The data file was replaced (new fetch at 9000) but the state file still holds the attempt record.
    (tmp_path / "stations.dat").write_bytes(_dat(9000.0, GOOD))
    (tmp_path / "stations.state.json").write_text(json.dumps(
        {"fetched_at": 0.0, "next_try": 9000.0 + BACKOFF_MIN_S, "backoff": 7200, "last": "HTTP 500",
         "last_attempt": 9000.0}))
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=9010.0)
    assert src.fetched_at == 9000.0 and src.next_try == 9000.0 + 7200 and src.backoff == 0


def test_atomic_write_fsyncs_and_removes_temp_file_on_failure(tmp_path, monkeypatch):
    store, src = _store(tmp_path, FakeHttp([]))
    target = tmp_path / "stations.dat"
    target.write_bytes(b"old")
    synced = []
    real_fsync = store_mod.os.fsync
    monkeypatch.setattr(store_mod.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    store._atomic_write(str(target), b"new")
    assert target.read_bytes() == b"new" and synced
    monkeypatch.setattr(store_mod.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        store._atomic_write(str(target), b"newer")
    assert target.read_bytes() == b"new" and [p.name for p in tmp_path.iterdir()] == ["stations.dat"]


def test_save_failure_after_a_valid_download_keeps_the_new_elements(tmp_path, monkeypatch):
    logs = []
    store, src = _store(tmp_path, FakeHttp([(200, GOOD)]), log=logs.append)
    real = store._atomic_write

    def flaky(path, data):
        if path.endswith(".dat"):
            raise OSError("disk full SECRET")
        return real(path, data)

    monkeypatch.setattr(store, "_atomic_write", flaky)
    assert store.refresh(src, 1000.0) is True
    assert len(src.elems) == 1 and src.fetched_at == 1000.0 and src.backoff == 0
    assert src.next_try == 1000.0 + 7200 and src.last == "ok, 1 objects (not saved: OSError)"
    assert any("not saved" in m for m in logs) and "SECRET" not in " ".join(logs)


# --- Minor: range checks, None for "never", names ---

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 10_000.0])
def test_unbelievable_fetch_time_keeps_elements_but_time_is_unknown(tmp_path, bad):
    (tmp_path / "stations.dat").write_bytes(_dat(bad, GOOD))
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=1000.0)
    assert len(src.elems) == 1 and src.fetched_at is None and store.age_h("stations", 1000.0) == -1.0
    assert math.isfinite(src.next_try) and src.next_try <= 1000.0 + BACKOFF_MAX_S


def test_unbelievable_state_values_are_ignored_or_clamped(tmp_path):
    (tmp_path / "stations.state.json").write_text(
        '{"fetched_at": NaN, "next_try": 1e12, "backoff": "x", "last": 5, "last_attempt": 99999999}')
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=1000.0)
    assert src.next_try == 1000.0 + BACKOFF_MAX_S and src.backoff == 0 and isinstance(src.last, str)
    (tmp_path / "stations.state.json").write_text('{"next_try": NaN, "last_attempt": 1e300}')
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=1000.0)
    assert store.due(1000.0) == [src]


def test_never_fetched_is_none_and_time_zero_is_a_real_time(tmp_path):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD)]))
    assert src.fetched_at is None and store.age_h("stations", 0.0) == -1.0
    store.refresh(src, 0.0)
    assert store.age_h("stations", 3600.0) == 1.0


@pytest.mark.parametrize("name", ["", "Stations", "../x", "a/b", "a" * 33, "st.ns", "x\n", None])
def test_source_name_must_be_safe_for_a_path(name):
    with pytest.raises(ValueError):
        Source(name, "https://example.test/x", "omm", 7200)


@pytest.mark.parametrize("url", ["http://example.test/x", "ftp://x", "HTTPS://x", "", None])
def test_source_url_must_be_https(url):
    with pytest.raises(ValueError):
        Source("ok", url, "omm", 7200)


def test_store_refuses_duplicate_names(tmp_path):
    a, b = Source("dup", "https://a.test/", "omm", 7200), Source("dup", "https://b.test/", "omm", 7200)
    with pytest.raises(ValueError):
        Store(str(tmp_path), [a, b], FakeHttp([]), "UA", log=lambda m: None)


# --- Findings 3-5 and round 2: requests_get ---

class FakeRaw:
    """A response body. read() honours amt, like a real stream; every request is recorded."""

    def __init__(self, chunks):
        self.chunks, self.reads = list(chunks), []

    def read(self, amt=None, decode_content=None):
        self.reads.append((amt, decode_content))
        if not self.chunks:
            return b""
        c = self.chunks.pop(0)
        if amt is not None and len(c) > amt:
            c, rest = c[:amt], c[amt:]
            self.chunks.insert(0, rest)
        return c


class FakeResponse:
    def __init__(self, status, raw, headers=None):
        self.status_code, self.raw, self.closed = status, raw, False
        self.headers = dict(headers or {})

    def close(self):
        self.closed = True


class FakeSession:
    instances = []

    def __init__(self, response=None, block=None):
        self.trust_env, self.response, self.calls, self.closed = True, response, [], False
        self.block = block
        FakeSession.instances.append(self)

    def get(self, url, **kw):
        self.calls.append((url, dict(kw), self.trust_env))
        if self.block is not None:          # connect / TLS / headers that never finish in time
            self.block.wait(5)
        return self.response

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def fake_session(monkeypatch):
    import requests
    holder = {"block": None}
    FakeSession.instances = []

    def make():
        return FakeSession(holder["response"], holder["block"])

    def no_network(*a, **k):
        raise AssertionError("a test tried to use the network")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", no_network)
    monkeypatch.setattr(requests, "Session", make)
    return holder


@pytest.fixture
def jobs(monkeypatch):
    """Every download job started, so a test can see what an abandoned worker did. Releases them at the end."""
    import threading
    started, releases = [], []

    class Recorded(store_mod._Download):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self)

    monkeypatch.setattr(store_mod, "_Download", Recorded)
    monkeypatch.setattr(store_mod, "TOTAL_TIMEOUT_S", 0.2)
    yield started, releases
    for r in releases:
        r()
    for t in threading.enumerate():
        if t.name.startswith("store-download"):          # workers and their reapers
            t.join(2)
            assert not t.is_alive(), "a download worker was left running"


def test_requests_get_settings_without_proxy(fake_session):
    fake_session["response"] = FakeResponse(200, FakeRaw([b"ab", b"c"]))
    given = {"User-Agent": "UA"}
    assert store_mod.requests_get("https://x.test/a", given, None, 60) == (200, b"abc")
    s = FakeSession.instances[0]
    url, kw, trust_env = s.calls[0]
    assert url == "https://x.test/a" and trust_env is False
    assert kw["allow_redirects"] is False and kw["verify"] is True and kw["stream"] is True
    assert kw["proxies"] == {} and kw["headers"] == {"User-Agent": "UA", "Accept-Encoding": "identity"}
    assert given == {"User-Agent": "UA"}                       # the caller's dict is not changed
    assert fake_session["response"].closed and s.closed
    assert all(dc is False for _, dc in fake_session["response"].raw.reads)   # never asks urllib3 to decode


def test_requests_get_always_asks_for_an_uncompressed_body(fake_session):
    fake_session["response"] = FakeResponse(200, FakeRaw([b"x"]))
    store_mod.requests_get("https://x.test/a", {"accept-encoding": "gzip", "User-Agent": "UA"}, None, 60)
    _, kw, _ = FakeSession.instances[0].calls[0]
    assert kw["headers"] == {"User-Agent": "UA", "Accept-Encoding": "identity"}


def test_requests_get_settings_with_proxy(fake_session):
    fake_session["response"] = FakeResponse(200, FakeRaw([b"x"]))
    store_mod.requests_get("https://x.test/a", {}, "http://user:pw@proxy.test:1", 60)
    _, kw, trust_env = FakeSession.instances[0].calls[0]
    assert kw["proxies"] == {"https": "http://user:pw@proxy.test:1"} and trust_env is False


def test_requests_get_non_200_returns_status_and_closes(fake_session):
    fake_session["response"] = FakeResponse(301, FakeRaw([b"moved"]))
    assert store_mod.requests_get("https://x.test/a", {}, None, 60) == (301, b"")
    assert fake_session["response"].closed


def test_requests_get_refuses_plain_http(fake_session):
    fake_session["response"] = FakeResponse(200, FakeRaw([b"x"]))
    with pytest.raises(ValueError):
        store_mod.requests_get("http://x.test/a", {}, None, 60)
    assert FakeSession.instances == [] or FakeSession.instances[0].calls == []


@pytest.mark.parametrize("enc", ["gzip", "deflate", "br", "GZIP", "identity, gzip"])
def test_requests_get_refuses_an_encoded_body(fake_session, enc):
    fake_session["response"] = FakeResponse(200, FakeRaw([b"\x1f\x8b..."]), {"Content-Encoding": enc})
    with pytest.raises(store_mod.DownloadError):
        store_mod.requests_get("https://x.test/a", {}, None, 60)
    assert fake_session["response"].closed and fake_session["response"].raw.reads == []


@pytest.mark.parametrize("enc", ["identity", "Identity", ""])
def test_requests_get_accepts_identity_encoding(fake_session, enc):
    fake_session["response"] = FakeResponse(200, FakeRaw([b"ok"]), {"Content-Encoding": enc})
    assert store_mod.requests_get("https://x.test/a", {}, None, 60) == (200, b"ok")


def test_requests_get_size_cap(fake_session, monkeypatch):
    monkeypatch.setattr(elements, "MAX_BYTES", 10)
    fake_session["response"] = FakeResponse(200, FakeRaw([b"123456", b"78901234", b"never read"]))
    with pytest.raises(store_mod.DownloadTooLarge):
        store_mod.requests_get("https://x.test/a", {}, None, 60)
    raw = fake_session["response"].raw
    assert fake_session["response"].closed and raw.chunks == [b"234", b"never read"]
    assert [amt for amt, _ in raw.reads] == [11, 5]           # never asks for more than cap + 1 - held


def test_requests_get_exactly_at_cap_is_accepted(fake_session, monkeypatch):
    monkeypatch.setattr(elements, "MAX_BYTES", 10)
    fake_session["response"] = FakeResponse(200, FakeRaw([b"12345", b"67890"]))
    assert store_mod.requests_get("https://x.test/a", {}, None, 60) == (200, b"1234567890")


def test_requests_get_worker_errors_reach_the_caller(fake_session):
    class Broken(FakeRaw):
        def read(self, amt=None, decode_content=None):
            raise ConnectionResetError("peer at https://x.test/a went away")

    fake_session["response"] = FakeResponse(200, Broken([]))
    with pytest.raises(ConnectionResetError):
        store_mod.requests_get("https://x.test/a", {}, None, 60)
    assert fake_session["response"].closed


def test_requests_get_connect_or_headers_that_never_finish_time_out(fake_session, jobs):
    import threading
    import time
    started, releases = jobs
    block = threading.Event()
    releases.append(block.set)
    fake_session["block"] = block
    late = FakeResponse(200, FakeRaw([b"late data"]))
    fake_session["response"] = late
    t0 = time.monotonic()
    with pytest.raises(store_mod.DownloadTimeout):
        store_mod.requests_get("https://x.test/a", {}, None, 60)
    assert time.monotonic() - t0 < 0.2 + 0.3
    assert FakeSession.instances[0].closed                    # the session was closed to unblock the worker
    block.set()                                               # now the headers arrive, too late
    started[0].thread.join(2)
    assert not started[0].thread.is_alive()
    assert late.closed and late.raw.reads == []               # the late response is closed, nothing read
    assert started[0].result is None and started[0].error is None and not started[0].finished.is_set()


def test_requests_get_body_that_stops_arriving_times_out(fake_session, jobs):
    import socket
    import time
    started, releases = jobs
    ours, theirs = socket.socketpair()
    releases.append(ours.close)
    releases.append(theirs.close)
    theirs.sendall(b"first part ")

    class SocketBody:
        """A body read from a real (local) socket whose sender goes quiet."""

        def __init__(self, sock):
            self.connection = type("Conn", (), {"sock": sock})()
            self.reads = []

        def read(self, amt=None, decode_content=None):
            self.reads.append(amt)
            return self.connection.sock.recv(amt)

    resp = FakeResponse(200, SocketBody(ours))
    fake_session["response"] = resp
    t0 = time.monotonic()
    with pytest.raises(store_mod.DownloadTimeout):
        store_mod.requests_get("https://x.test/a", {}, None, 60)
    assert time.monotonic() - t0 < 0.2 + 0.3
    started[0].thread.join(2)                                 # shutting the socket down released the read
    assert not started[0].thread.is_alive()
    assert resp.closed and len(resp.raw.reads) == 2
    assert started[0].result is None and started[0].error is None and not started[0].finished.is_set()


class LoopbackServer:
    """A plain-HTTP listener on 127.0.0.1: sends `Connection: close` headers (so http.client hands the socket
    to the response and leaves connection.sock None), then stalls or trickles one byte per `trickle_s`."""

    def __init__(self, trickle_s=None, run_for_s=3.0):
        import socket
        import threading
        self.trickle_s, self.run_for_s = trickle_s, run_for_s
        self.stop = threading.Event()
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(1)
        self.port = self.lsock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, name="test-loopback-server", daemon=True)
        self.thread.start()

    def _serve(self):
        import time
        try:
            self.lsock.settimeout(5)
            conn, _ = self.lsock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n")
                end = time.monotonic() + self.run_for_s
                while not self.stop.is_set() and time.monotonic() < end:
                    if self.trickle_s is None:
                        self.stop.wait(0.05)
                    else:
                        conn.sendall(b"x")
                        self.stop.wait(self.trickle_s)
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.lsock.close()
        self.thread.join(5)
        assert not self.thread.is_alive()


@pytest.fixture
def loopback(fake_session, jobs, monkeypatch):
    """requests.Session replaced by one whose get() makes a REAL urllib3 request to a loopback listener and
    wraps it in a real requests.Response (real http.client response, real buffered reader). The https-only
    check and the HTTPAdapter.send block of the production path stay in force."""
    import requests
    import urllib3
    servers, sessions = [], []

    class LoopbackSession:
        def __init__(self, server, read_timeout):
            self.server, self.read_timeout = server, read_timeout
            self.trust_env, self.closed, self.pool = True, False, None
            sessions.append(self)

        def get(self, url, **kw):
            self.pool = urllib3.HTTPConnectionPool("127.0.0.1", self.server.port, maxsize=1, retries=False,
                                                   timeout=urllib3.Timeout(connect=2, read=self.read_timeout))
            u = self.pool.urlopen("GET", "/", headers=kw["headers"], preload_content=False, retries=False)
            adapter = requests.adapters.HTTPAdapter()
            try:
                return adapter.build_response(requests.Request("GET", url).prepare(), u)
            finally:
                adapter.close()

        def close(self):
            self.closed = True
            if self.pool is not None:
                self.pool.close()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    def start(read_timeout, **server_kw):
        server = LoopbackServer(**server_kw)
        servers.append(server)
        monkeypatch.setattr(requests, "Session", lambda: LoopbackSession(server, read_timeout))
        return server

    yield start, jobs[0], sessions
    for s in servers:
        s.close()


@pytest.mark.parametrize("case", ["stall", "trickle"])
def test_requests_get_returns_on_time_when_a_connection_close_body_stalls_or_trickles(loopback, case):
    """Review round 3: a real response whose connection.sock is None, read through a real BufferedReader.
    stall: headers then nothing, per-read timeout 2 s. trickle: headers then 1 byte every 0.1 s for 3 s,
    per-read timeout 1 s (never fires). The caller must be back within the total (0.2 s) plus a margin."""
    import time
    start, started, sessions = loopback
    if case == "stall":
        server = start(2.0, trickle_s=None, run_for_s=3.0)
    else:
        server = start(1.0, trickle_s=0.1, run_for_s=3.0)
    t0 = time.monotonic()
    with pytest.raises(store_mod.DownloadTimeout):
        store_mod.requests_get("https://x.test/a", {}, None, 60)
    took = time.monotonic() - t0
    print(f"{case}: caller regained control after {took:.2f}s (total limit 0.2s)")
    assert took < 0.2 + 0.3
    started[0].thread.join(1.5)                             # the reaper woke the blocked read
    assert not started[0].thread.is_alive()
    assert started[0].result is None and started[0].error is None and not started[0].finished.is_set()
    assert sessions[0].closed
    server.close()


@pytest.mark.parametrize("variant", ["no_shutdown_methods", "shutdown_raises_attribute_error"])
def test_requests_get_abandon_tolerates_transports_without_a_usable_shutdown(fake_session, jobs, monkeypatch, variant):
    """TLS-in-TLS through an https proxy gives an SSLTransport with no shutdown(); nothing it raises may
    change what the caller sees or escape a thread."""
    import threading
    import time
    started, releases = jobs
    hook_calls = []
    monkeypatch.setattr(threading, "excepthook", lambda args: hook_calls.append(args.exc_type))
    gate = threading.Event()
    releases.append(gate.set)

    class Transport:                                        # like SSLTransport: no shutdown()
        pass

    class BadTransport:
        def shutdown(self, how):
            raise AttributeError("SSLTransport has no attribute shutdown at https://x.test/a")

    class Body:
        def __init__(self):
            self.connection = type("Conn", (), {})()
            self.connection.sock = Transport() if variant == "no_shutdown_methods" else BadTransport()
            if variant == "shutdown_raises_attribute_error":
                def shutdown():
                    raise AttributeError("no shutdown at https://x.test/a")
                self.shutdown = shutdown

        def read(self, amt=None, decode_content=None):
            gate.wait(5)
            return b""

    resp = FakeResponse(200, Body())
    fake_session["response"] = resp
    t0 = time.monotonic()
    with pytest.raises(store_mod.DownloadTimeout):
        store_mod.requests_get("https://x.test/a", {}, None, 60)
    assert time.monotonic() - t0 < 0.2 + 0.3
    deadline = time.monotonic() + 2
    while not (resp.closed and FakeSession.instances[0].closed) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert resp.closed and FakeSession.instances[0].closed    # the failing wake-up step did not stop the rest
    gate.set()
    started[0].thread.join(2)
    for t in threading.enumerate():
        if t.name.startswith("store-download"):
            t.join(2)
    assert hook_calls == []
    assert started[0].result is None and started[0].error is None


def test_requests_get_timeouts_never_exceed_the_total(fake_session):
    fake_session["response"] = FakeResponse(200, FakeRaw([b"x"]))
    store_mod.requests_get("https://x.test/a", {}, None, 500)
    _, kw, _ = FakeSession.instances[0].calls[0]
    assert max(kw["timeout"]) <= store_mod.TOTAL_TIMEOUT_S


def test_store_user_agent_is_sent_through_requests_get(tmp_path, fake_session):
    fake_session["response"] = FakeResponse(200, FakeRaw([GOOD]))
    store, src = _store(tmp_path, store_mod.requests_get)
    assert store.refresh(src, 0.0) is True
    _, kw, _ = FakeSession.instances[0].calls[0]
    assert kw["headers"]["User-Agent"] == "UA/1 (me@example.test)" and kw["proxies"] == {}


def test_download_errors_back_off_as_network_errors(tmp_path):
    store, src = _store(tmp_path, FakeHttp([store_mod.DownloadTooLarge()]))
    assert store.refresh(src, 0.0) is False and src.last == "network error: DownloadTooLarge"
    assert src.next_try == BACKOFF_MIN_S


# --- Round 2 minor items ---

def test_data_newer_than_the_state_file_reads_loaded_from_disk(tmp_path):
    # Crash after the data write, before the state write: the old state still says "HTTP 403".
    (tmp_path / "stations.dat").write_bytes(_dat(9000.0, GOOD))
    (tmp_path / "stations.state.json").write_text(json.dumps(
        {"fetched_at": 100.0, "next_try": 9000.0 + BACKOFF_MIN_S, "backoff": 3600, "last": "HTTP 403",
         "last_attempt": 9000.0}))
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=9010.0)
    assert src.last == "loaded from disk" and src.backoff == 0


def test_data_not_newer_than_the_state_file_keeps_its_status(tmp_path):
    (tmp_path / "stations.dat").write_bytes(_dat(100.0, GOOD))
    (tmp_path / "stations.state.json").write_text(json.dumps(
        {"fetched_at": 100.0, "next_try": 9000.0 + BACKOFF_MIN_S, "backoff": 3600, "last": "HTTP 403",
         "last_attempt": 9000.0}))
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk(now=9010.0)
    assert src.last == "HTTP 403" and src.backoff == 3600


@pytest.mark.parametrize("interval", [0, 60, BACKOFF_MIN_S - 1, -7200, float("nan"), float("inf"), "7200", None, True])
def test_interval_below_the_hourly_minimum_is_refused(interval):
    with pytest.raises(ValueError):
        Source("ok", "https://example.test/x", "omm", interval)


def test_interval_of_exactly_one_hour_is_allowed():
    assert Source("ok", "https://example.test/x", "omm", BACKOFF_MIN_S).interval_s == BACKOFF_MIN_S


def test_reap_survives_close_attributes_that_raise_on_lookup():
    closed = []

    class Hostile:
        @property
        def close(self):                     # even looking the attribute up raises
            raise RuntimeError("lookup")

    class Session:
        def close(self):
            closed.append("session")

    store_mod._reap(Hostile(), Session())     # does not raise; the session is still closed
    assert closed == ["session"]
    store_mod._reap(None, Hostile())          # a hostile session is tolerated too


# --- the extra TLE file may be published as a zip archive -------------------------------

def _zip_body(members):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _tle_store(tmp_path, http):
    src = Source("extra", "https://example.test/extra.zip", "tle", 24 * 3600)
    return Store(str(tmp_path), [src], http, "UA", log=lambda m: None), src


def test_zipped_tle_is_accepted_persisted_raw_and_reloaded(tmp_path):
    from conftest import ISS_L1, ISS_L2
    body = _zip_body([("feed.tle", f"ISS\n{ISS_L1}\n{ISS_L2}\n".encode())])
    store, src = _tle_store(tmp_path, FakeHttp([(200, body)]))
    assert store.refresh(src, 1000.0) is True
    assert [(e.name, e.satnum) for e in src.elems] == [("ISS", 25544)] and src.last == "ok, 1 objects"
    head, _, saved = (tmp_path / "extra.dat").read_bytes().partition(b"\n")
    assert saved == body and json.loads(head)["bytes"] == len(body)
    fresh, src2 = _tle_store(tmp_path, FakeHttp([]))
    fresh.load_disk(now=1001.0)
    assert [(e.name, e.satnum) for e in src2.elems] == [("ISS", 25544)] and src2.fetched_at == 1000.0

