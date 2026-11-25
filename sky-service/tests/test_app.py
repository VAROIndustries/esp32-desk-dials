import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import app as A
import elements as E
import screens as S
from conftest import T0, omm
from engine import Engine
from store import Source, Store
from test_engine import _subpoint


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _sources(*names):
    return [Source(n, f"https://example.test/{n}", "tle" if n == S.HOBBY_SOURCE else "omm", 7200) for n in names]


def _app(tmp_path, env=None, clock=None, sources=None, engine=None):
    clock = clock or Clock()
    lat, lon = _subpoint(T0)
    if engine is None:
        rows = [omm(1000 + i, f"S{i}", ma=85.6398 + (i - 40) * 0.01) for i in range(80)]
        elems = E.parse_omm_json(json.dumps(rows).encode())
        engine = Engine()
        engine.set_screens({n: (elems, ["HOBBY"] * len(elems)) for n in S.SCREENS}, {n: T0 - 3600 for n in S.SCREENS})
        engine.tick(T0)
    store = Store(str(tmp_path), sources or _sources("starlink"), None, "UA", log=lambda m: None)
    here = f"lat={round(lat, 1)}&lon={round(lon, 1)}"
    return A.App(engine, store, env or {}, clock), here, clock


class StubEngine:
    """Records query_many calls; t0 and tick_age are chosen independently by the test."""

    def __init__(self, t0, age):
        self.t0, self.age, self.calls, self.dropped = t0, age, [], {}

    def tick_age(self, now):
        return self.age

    def is_cached(self, screen, lat, lon, cut):
        return False

    def query_many(self, lat, lon, wanted):
        self.calls.append((lat, lon, dict(wanted)))
        return self.t0, {n: {"count": 0, "age_h": 1.0, "objects": [], "next": None, "next_busy": True} for n in wanted}


def test_public_screens_answer_without_a_key_and_are_never_cached(tmp_path):
    app, here, _ = _app(tmp_path)
    status, body, hdr = app.handle(f"/v1/sky?{here}&screens=starlink,nasa&cut=starlink:30,nasa:30", {}, "1.1.1.1")
    assert status == 200 and body["poll"] == 50 and body["t"] == int(T0) and body["lag_ms"] == 0
    assert body["screens"]["starlink"]["count"] == 80 and len(body["screens"]["nasa"]["objects"]) == 60
    assert hdr["Cache-Control"] == "private, no-store" and "notice" not in body


@pytest.mark.parametrize("auth", [{}, {"authorization": "Bearer wrong"}, {"authorization": "Bearer "}, {"authorization": "secret-key"}])
def test_spy_is_locked_without_the_right_key_and_never_cached(tmp_path, auth):
    app, here, _ = _app(tmp_path, {"SKY_SPY_KEY": "secret-key"})
    status, body, hdr = app.handle(f"/v1/sky?{here}&screens=spy,nasa&cut=nasa:30", auth, "1.1.1.1")
    assert status == 200 and body["screens"]["spy"] == {"locked": True}
    assert body["screens"]["nasa"]["count"] == 80            # other screens still answer
    assert hdr["Cache-Control"] == "private, no-store"


def test_spy_opens_with_the_key_and_is_never_cached(tmp_path):
    app, here, _ = _app(tmp_path, {"SKY_SPY_KEY": "secret-key"})
    status, body, hdr = app.handle(f"/v1/sky?{here}&screens=spy&cut=spy:30", {"authorization": "Bearer secret-key"}, "1.1.1.1")
    assert status == 200 and body["screens"]["spy"]["objects"][0]["tag"] == "HOBBY"
    assert hdr["Cache-Control"] == "private, no-store"


def test_spy_stays_locked_when_the_server_has_no_key_set(tmp_path):
    app, here, _ = _app(tmp_path, {})
    _, body, _ = app.handle(f"/v1/sky?{here}&screens=spy", {"authorization": "Bearer "}, "1.1.1.1")
    assert body["screens"]["spy"] == {"locked": True}


@pytest.mark.parametrize("qs", [
    "lon=1&screens=nasa", "lat=x&lon=1&screens=nasa", "lat=91&lon=1&screens=nasa", "lat=1&lon=181&screens=nasa",
    "lat=nan&lon=1&screens=nasa", "lat=inf&lon=1&screens=nasa", "lat=1&lon=1", "lat=1&lon=1&screens=",
    "lat=1&lon=1&screens=nasa,nasa", "lat=1&lon=1&screens=nasa,starlink,spy,nasa", "lat=1&lon=1&screens=http://evil",
    "lat=1&lon=1&screens=nasa&cut=nasa:86", "lat=1&lon=1&screens=nasa&cut=nasa:-1", "lat=1&lon=1&screens=nasa&cut=starlink:10",
    "lat=1&lon=1&screens=nasa&cut=nasa:1e1",
    "lat=1&lon=1&screens=nasa&cut=nasa:%C2%B2",                    # superscript two: isdigit() but not a number
    "lat=1&lon=1&screens=nasa&cut=nasa:%D9%A3",                    # Arabic-Indic digit: ASCII digits only
    "lat=1&lon=1&screens=nasa&" + "&".join(f"x{i}=1" for i in range(10)),   # too many fields
])
def test_bad_requests_are_refused(tmp_path, qs):
    app, _, _ = _app(tmp_path)
    assert app.handle(f"/v1/sky?{qs}", {}, "1.1.1.1")[0] == 400


def test_location_is_rounded_to_a_tenth_of_a_degree():
    assert A.parse_request("lat=42.04999&lon=-71.25001&screens=nasa")[:2] == (42.0, -71.3)
    assert A.parse_request("lat=-0.04&lon=0.04&screens=nasa")[:2] == (0.0, 0.0)


def test_per_address_rate_limit_and_window_reset(tmp_path):
    app, here, clock = _app(tmp_path, {"RATE_PER_MIN": "3"})
    url = f"/v1/sky?{here}&screens=nasa"
    assert [app.handle(url, {}, "9.9.9.9")[0] for _ in range(4)] == [200, 200, 200, 429]
    assert app.handle(url, {}, "8.8.8.8")[0] == 200            # another address is unaffected
    clock.t += 61
    assert app.handle(url, {}, "9.9.9.9")[0] == 200


def test_wrong_keys_use_up_the_allowance_faster(tmp_path):
    app, here, _ = _app(tmp_path, {"SKY_SPY_KEY": "k", "RATE_PER_MIN": "12"})
    url = f"/v1/sky?{here}&screens=spy"
    codes = [app.handle(url, {"authorization": "Bearer guess"}, "9.9.9.9")[0] for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_global_ceiling_counts_only_fresh_calculations(tmp_path):
    app, here, clock = _app(tmp_path, {"CALC_PER_SEC": "2", "RATE_PER_MIN": "100"})
    urls = [f"/v1/sky?lat={10 + i}&lon=20&screens=nasa" for i in range(3)]
    got = [app.handle(u, {}, f"1.1.1.{i}") for i, u in enumerate(urls)]
    assert [g[0] for g in got] == [200, 200, 503]
    assert got[2][1]["poll"] == 300 and got[2][2]["Retry-After"] == "300"
    assert app.handle(urls[0], {}, "1.1.1.7")[0] == 200        # already calculated this tick: not charged
    clock.t += 1
    assert app.handle(urls[2], {}, "1.1.1.9")[0] == 200


def test_origin_secret_gates_everything_and_supplies_the_caller_address(tmp_path):
    app, here, _ = _app(tmp_path, {"ORIGIN_SECRET": "edge", "RATE_PER_MIN": "1"})
    url = f"/v1/sky?{here}&screens=nasa"
    assert app.handle(url, {}, "10.0.0.1")[0] == 403
    assert app.handle("/health", {"x-origin-auth": "nope"}, "10.0.0.1")[0] == 403
    ok = {"x-origin-auth": "edge", "cf-connecting-ip": "5.5.5.5"}
    assert app.handle(url, ok, "10.0.0.1")[0] == 200
    assert app.handle(url, ok, "10.0.0.1")[0] == 429                                   # limited by 5.5.5.5 ...
    assert app.handle(url, dict(ok, **{"cf-connecting-ip": "6.6.6.6"}), "10.0.0.1")[0] == 200   # ... not by the proxy


def test_lag_reports_how_old_the_track_start_is(tmp_path):
    app, here, clock = _app(tmp_path)
    clock.t += 3.25
    _, body, _ = app.handle(f"/v1/sky?{here}&screens=nasa&cut=nasa:30", {}, "1.1.1.1")
    assert body["lag_ms"] == 3250 and body["screens"]["nasa"]["count"] == 80


def test_lag_follows_the_t0_the_engine_returns_not_tick_age(tmp_path):
    eng = StubEngine(t0=T0 + 6.75, age=0.5)          # tick_age would say 500 ms; the snapshot says 3250
    app, _, _ = _app(tmp_path, clock=Clock(T0 + 10), engine=eng)
    status, body, _ = app.handle("/v1/sky?lat=1&lon=2&screens=nasa", {}, "1.1.1.1")
    assert status == 200 and body["lag_ms"] == 3250
    assert body["screens"]["nasa"]["next_busy"] is True and body["screens"]["nasa"]["next"] is None   # passed through


def test_lag_is_never_negative(tmp_path):
    eng = StubEngine(t0=T0 + 1, age=0.0)
    app, _, _ = _app(tmp_path, engine=eng)
    assert app.handle("/v1/sky?lat=1&lon=2&screens=nasa", {}, "1.1.1.1")[1]["lag_ms"] == 0


def test_engine_is_asked_once_and_only_for_screens_the_caller_may_see(tmp_path):
    eng = StubEngine(t0=T0, age=0.0)
    app, _, _ = _app(tmp_path, {"SKY_SPY_KEY": "k" * 24}, engine=eng)
    _, body, _ = app.handle("/v1/sky?lat=1&lon=2&screens=spy,nasa&cut=nasa:30", {}, "1.1.1.1")
    assert eng.calls == [(1.0, 2.0, {"nasa": 30})] and body["screens"]["spy"] == {"locked": True}
    assert list(body["screens"]) == ["spy", "nasa"]
    eng.calls.clear()
    app.handle("/v1/sky?lat=1&lon=2&screens=spy,nasa", {"authorization": "Bearer " + "k" * 24}, "1.1.1.2")
    assert eng.calls == [(1.0, 2.0, {"spy": 20, "nasa": 20})]


def test_a_stalled_engine_is_not_asked(tmp_path):
    eng = StubEngine(t0=T0, age=21.0)
    app, _, _ = _app(tmp_path, engine=eng)
    _, body, _ = app.handle("/v1/sky?lat=1&lon=2&screens=nasa", {}, "1.1.1.1")
    assert eng.calls == [] and body["screens"]["nasa"] == {"unavailable": True} and body["lag_ms"] == 0


def test_stalled_background_loop_is_visible_not_silent(tmp_path):
    app, here, clock = _app(tmp_path, {"SKY_SPY_KEY": "k" * 24})
    clock.t += 21
    status, body, _ = app.handle(f"/v1/sky?{here}&screens=nasa,spy", {"authorization": "Bearer " + "k" * 24}, "1.1.1.1")
    assert status == 200 and body["screens"] == {"nasa": {"unavailable": True}, "spy": {"unavailable": True}}
    assert body["lag_ms"] == 0
    _, body, _ = app.handle(f"/v1/sky?{here}&screens=spy", {}, "1.1.1.1")
    assert body["screens"]["spy"] == {"locked": True}             # still locked without the key
    status, body, _ = app.handle("/health", {}, "1.1.1.1")
    assert status == 503 and body["ok"] is False and body["tick_age_s"] == 21.0


def test_never_ticked_engine_reads_as_stalled(tmp_path):
    app, here, _ = _app(tmp_path, engine=Engine())
    status, body, _ = app.handle("/health", {}, "1.1.1.1")
    assert status == 503 and body["ok"] is False and body["tick_age_s"] == -1
    _, body, _ = app.handle(f"/v1/sky?{here}&screens=nasa", {}, "1.1.1.1")
    assert body["screens"]["nasa"] == {"unavailable": True} and body["lag_ms"] == 0


def test_check_env_refuses_weak_settings_and_warns_about_open_origin():
    with pytest.raises(SystemExit, match="SKY_CONTACT"):
        A.check_env({})
    with pytest.raises(SystemExit, match="at least 24"):
        A.check_env({"SKY_CONTACT": "a@b.test", "SKY_SPY_KEY": "short"})
    assert len(A.check_env({"SKY_CONTACT": "a@b.test", "SKY_SPY_KEY": "k" * 24})) == 1
    assert A.check_env({"SKY_CONTACT": "a@b.test", "ORIGIN_SECRET": "edge"}) == []


def test_container_health_check_from_loopback_needs_no_origin_secret(tmp_path):
    app, here, _ = _app(tmp_path, {"ORIGIN_SECRET": "edge"})
    assert app.handle("/health", {}, "127.0.0.1")[0] == 200
    assert app.handle("/health", {}, "::1")[0] == 200
    assert app.handle(f"/v1/sky?{here}&screens=nasa", {}, "127.0.0.1")[0] == 403


def test_forwarded_address_header_is_ignored_without_origin_secret(tmp_path):
    app, here, _ = _app(tmp_path, {"RATE_PER_MIN": "1"})
    url = f"/v1/sky?{here}&screens=nasa"
    assert app.handle(url, {"cf-connecting-ip": "5.5.5.5"}, "10.0.0.1")[0] == 200
    assert app.handle(url, {"cf-connecting-ip": "6.6.6.6"}, "10.0.0.1")[0] == 429


def test_health_notice_and_unknown_paths(tmp_path):
    app, here, _ = _app(tmp_path, {"SKY_NOTICE": "Plane source changed, see dial.varo.industries " + "x" * 200})
    status, body, _ = app.handle("/health", {}, "1.1.1.1")
    assert status == 200 and body["sources"]["starlink"]["objects"] == 0
    assert body["ok"] is True and body["edge_locked"] is False and body["tick_age_s"] == 0 and body["dropped_old"] == {}
    assert app.handle("/admin", {}, "1.1.1.1")[0] == 404
    _, body, _ = app.handle(f"/v1/sky?{here}&screens=nasa", {}, "1.1.1.1")
    assert len(body["notice"]) == 120


def test_every_reply_is_marked_not_cacheable(tmp_path):
    app, here, _ = _app(tmp_path, {"RATE_PER_MIN": "1", "ORIGIN_SECRET": "edge"})
    ok = {"x-origin-auth": "edge"}
    replies = [app.handle("/v1/sky?lat=1", {}, "1.1.1.1"),                    # 403
               app.handle("/v1/sky?lat=x", ok, "1.1.1.1"),                    # 400
               app.handle("/v1/sky?lat=x", ok, "1.1.1.1"),                    # 429
               app.handle("/nope", ok, "1.1.1.1")]                            # 404
    assert [r[0] for r in replies] == [403, 400, 429, 404]
    assert all(r[2]["Cache-Control"] == "private, no-store" for r in replies)


# --- /health must not reveal the private screen (amendment A) --------------------------------

def _private_health_app(tmp_path, env=None):
    app, _, _ = _app(tmp_path, env, sources=_sources("starlink", "military", S.HOBBY_SOURCE))
    app.engine.dropped = {"starlink": 0, "military": 1, "hobby": 2}
    return app


def test_public_health_hides_the_private_sources(tmp_path):
    app = _private_health_app(tmp_path, {"SKY_SPY_KEY": "k" * 24})
    for hdrs in ({}, {"authorization": "Bearer wrong"}, {"authorization": "k" * 24}):
        status, body, _ = app.handle("/health", hdrs, "1.1.1.1")
        assert status == 200 and body["ok"] is True
        assert list(body["sources"]) == ["starlink"] and body["dropped_old"] == {"starlink": 0}
        raw = json.dumps(body)
        assert "military" not in raw and "hobby" not in raw


def test_trusted_health_shows_everything(tmp_path):
    app = _private_health_app(tmp_path, {"SKY_SPY_KEY": "k" * 24})
    for hdrs, ip in (({"authorization": "Bearer " + "k" * 24}, "1.1.1.1"), ({}, "127.0.0.1"), ({}, "::1")):
        _, body, _ = app.handle("/health", hdrs, ip)
        assert sorted(body["sources"]) == ["hobby", "military", "starlink"]
        assert body["dropped_old"] == {"starlink": 0, "military": 1, "hobby": 2}


def test_public_health_still_reports_a_stall(tmp_path):
    app = _private_health_app(tmp_path)
    app.clock.t += 21
    status, body, _ = app.handle("/health", {}, "1.1.1.1")
    assert status == 503 and body["ok"] is False and list(body["sources"]) == ["starlink"]


def test_key_guesses_on_health_are_rate_limited_like_sky(tmp_path):
    app = _private_health_app(tmp_path, {"SKY_SPY_KEY": "k" * 24, "RATE_PER_MIN": "12"})
    codes = [app.handle("/health", {"authorization": "Bearer guess"}, "9.9.9.9")[0] for _ in range(3)]
    assert codes == [200, 200, 429]
    status, body, _ = app.handle("/health", {"authorization": "Bearer " + "k" * 24}, "9.9.9.9")
    assert status == 429 and "sources" not in body              # over the limit: the key is not even checked
    assert app.handle("/health", {}, "9.9.9.9")[0] == 200       # the plain health check is never limited


def test_worst_case_reply_fits_the_size_cap(tmp_path):
    app, here, _ = _app(tmp_path, {"SKY_SPY_KEY": "k"})
    _, body, _ = app.handle(f"/v1/sky?{here}&screens=starlink,nasa,spy&cut=starlink:0,nasa:0,spy:0", {"authorization": "Bearer k"}, "1.1.1.1")
    assert all(len(s["objects"]) == 60 for s in body["screens"].values())
    assert len(json.dumps(body, separators=(",", ":")).encode()) < A.MAX_BODY


def test_build_sources_fetches_spy_data_only_when_the_key_is_set():
    names = lambda env: sorted(s.name for s in A.build_sources(env)[0])
    assert names({}) == ["resource", "science", "starlink", "stations"]
    assert names({"SKY_SPY_KEY": "k"}) == ["military", "resource", "science", "starlink", "stations"]
    env = {"SKY_SPY_KEY": "k", "SPY_HOBBY_URL": "https://example.test/c.tle", "SPY_PROXY_URL": "http://u:p@proxy.test:1"}
    src, on = A.build_sources(env)
    hobby = [s for s in src if s.name == "hobby"][0]
    assert on and hobby.proxy == "http://u:p@proxy.test:1" and all(s.proxy is None for s in src if s.name != "hobby")
    assert A.build_sources(dict(env, SPY_HOBBY="0"))[1] is False
    assert A.build_sources(dict(env, SPY_HOBBY_URL="http://example.test/c.tle"))[1] is False       # plain HTTP refused
    assert A.build_sources({"SPY_HOBBY_URL": "https://example.test/c.tle"})[1] is False              # no key, no fetch


# --- background work: ticker and fetcher are separate (amendment D) --------------------------

class FakeHttp:
    def __init__(self, body):
        self.body, self.urls = body, []

    def __call__(self, url, headers, proxy, timeout):
        self.urls.append(url)
        return 200, self.body


def _omm_body(*satnums):
    return json.dumps([omm(n, f"S{n}") for n in satnums]).encode()


def test_tick_once_ticks_and_survives_errors(capsys):
    eng = Engine()
    A.tick_once(eng, T0)
    assert eng.tick_age(T0 + 1) == 1.0

    class Broken:
        def tick(self, now):
            raise RuntimeError("boom")
    A.tick_once(Broken(), T0)                                  # does not raise
    assert "[sky] background error: RuntimeError: boom" in capsys.readouterr().out


def test_fetch_once_downloads_at_most_one_source_per_minute_most_overdue_first(tmp_path):
    http = FakeHttp(_omm_body(25544))
    store = Store(str(tmp_path), _sources("starlink", "stations"), http, "UA", log=lambda m: None)
    store.sources["starlink"].next_try, store.sources["stations"].next_try = 5.0, 1.0
    state, eng = A.new_fetch_state(), Engine()
    A.fetch_once(state, eng, store, {}, False, T0)
    assert http.urls == ["https://example.test/stations"]
    A.fetch_once(state, eng, store, {}, False, T0 + 59)
    assert len(http.urls) == 1
    A.fetch_once(state, eng, store, {}, False, T0 + 60)
    assert http.urls[-1] == "https://example.test/starlink" and len(http.urls) == 2


def test_fetch_once_rebuilds_after_a_good_download_or_hourly(tmp_path, monkeypatch):
    http = FakeHttp(_omm_body(25544))
    store = Store(str(tmp_path), _sources("stations"), http, "UA", log=lambda m: None)
    calls = []
    real = A.rebuild
    monkeypatch.setattr(A, "rebuild", lambda *a: (calls.append(a[-1]), real(*a)))
    state, eng = A.new_fetch_state(), Engine()
    A.fetch_once(state, eng, store, {}, False, T0)              # downloads and rebuilds
    assert calls == [T0]
    eng.tick(T0)
    assert eng.has("nasa")
    A.fetch_once(state, eng, store, {}, False, T0 + 600)        # nothing due, under an hour: no rebuild
    assert calls == [T0]
    A.fetch_once(state, eng, store, {}, False, T0 + 3600)       # an hour on: rebuild so old orbits drop out
    assert calls == [T0, T0 + 3600] and len(http.urls) == 1


def test_fetch_once_does_not_rebuild_after_a_failed_download(tmp_path, monkeypatch):
    store = Store(str(tmp_path), _sources("stations"), lambda *a: (503, b""), "UA", log=lambda m: None)
    calls = []
    monkeypatch.setattr(A, "rebuild", lambda *a: calls.append(a[-1]))
    state = A.new_fetch_state()
    state["last_rebuild"] = T0 - 10
    A.fetch_once(state, Engine(), store, {}, False, T0)
    assert calls == [] and store.sources["stations"].backoff > 0


def test_fetch_once_survives_errors(capsys):
    class Broken:
        def due(self, now):
            raise OSError("disk gone")
    A.fetch_once(A.new_fetch_state(), Engine(), Broken(), {}, False, T0)
    assert "[sky] background error: OSError: disk gone" in capsys.readouterr().out


def test_rebuild_uses_the_oldest_fetch_and_treats_never_fetched_as_unknown(tmp_path):
    store = Store(str(tmp_path), _sources("stations", "science", "resource", "starlink"), None, "UA", log=lambda m: None)
    elems = E.parse_omm_json(_omm_body(25544, 20580))
    store.sources["stations"].elems, store.sources["stations"].fetched_at = elems[:1], T0 - 7200
    store.sources["science"].elems, store.sources["science"].fetched_at = elems[1:], T0 - 3600
    store.sources["starlink"].elems = elems                     # fetched_at stays None (never fetched)
    eng = Engine()
    A.rebuild(eng, store, {}, False, T0)
    eng.tick(T0)
    assert eng.query("nasa", 0.0, 0.0, 85)["age_h"] == 2.0      # the older of the two NASA sources
    assert eng.query("starlink", 0.0, 0.0, 85)["age_h"] == -1.0  # unknown, not 1970
    assert not eng.has("spy")


# --- the real server ---------------------------------------------------------------------------

def _serve(app, slots=None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), A.make_handler(app, slots))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def test_real_server_answers_and_logs_no_location(tmp_path, capsys):
    app, here, _ = _app(tmp_path)
    srv, base = _serve(app)
    try:
        with urllib.request.urlopen(f"{base}/v1/sky?{here}&screens=nasa", timeout=5) as r:
            assert r.status == 200 and r.headers["Cache-Control"] == "private, no-store"
            assert json.loads(r.read())["screens"]["nasa"]["count"] >= 0
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/v1/sky?lat=x&lon=9&screens=nasa", timeout=5)
        assert e.value.code == 400 and e.value.headers["Cache-Control"] == "private, no-store"
        e.value.close()
    finally:
        _stop(srv)
    out = capsys.readouterr().out
    assert "[http] 200 /v1/sky" in out and "[http] 400 /v1/sky" in out and "lat=" not in out and "lon=" not in out


def test_real_server_is_busy_when_all_slots_are_taken_and_drops_silent_clients(tmp_path):
    app, here, _ = _app(tmp_path)
    handler = A.make_handler(app, threading.Semaphore(0))
    assert handler.timeout == A.SOCKET_TIMEOUT_S == 10
    srv, base = _serve(app, threading.Semaphore(0))
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/v1/sky?{here}&screens=nasa", timeout=5)
        assert e.value.code == 503 and e.value.headers["Retry-After"] == "30"
        assert e.value.headers["Cache-Control"] == "private, no-store"
        e.value.close()
    finally:
        _stop(srv)
