import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import app as A
import elements as E
import screens as S
from conftest import T0, omm
from engine import Engine
from store import Source, Store
from test_engine import _subpoint


EDGE = "e" * 24                    # an ORIGIN_SECRET long enough for check_env


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _sources(*names):
    return [Source(n, f"https://example.test/{n}", "tle" if n == S.HOBBY_SOURCE else "omm", 7200) for n in names]


_TEMPLATE = []


def _ticked_engine():
    """A fresh Engine with 80 objects on every screen, ticked at T0.

    One tick costs about 0.1 s and most tests here need one, so the first engine is ticked
    for real and later ones are given its (read-only) tick results. Each test still gets its
    own engine: its own query cache, pass cache and `dropped`."""
    rows = [omm(1000 + i, f"S{i}", ma=85.6398 + (i - 40) * 0.01) for i in range(80)]
    elems = E.parse_omm_json(json.dumps(rows).encode())
    engine = Engine()
    engine.set_screens({n: (elems, ["HOBBY"] * len(elems)) for n in S.SCREENS}, {n: T0 - 3600 for n in S.SCREENS})
    if not _TEMPLATE:
        engine.tick(T0)
        _TEMPLATE.append(engine)
        return engine
    tpl = _TEMPLATE[0]
    for name, sc in engine._staged.items():
        src = tpl._screens[name]
        sc.r, sc.ok, sc.table = src.r, src.ok, src.table
    engine._screens, engine._staged = engine._staged, None       # as a tick swaps the staged set in
    engine._t0, engine._jd, engine._version = tpl._t0, tpl._jd, engine._version + 1
    return engine


def _app(tmp_path, env=None, clock=None, sources=None, engine=None):
    clock = clock or Clock()
    lat, lon = _subpoint(T0)
    if engine is None:
        engine = _ticked_engine()
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
    app, here, _ = _app(tmp_path, {"ORIGIN_SECRET": EDGE, "RATE_PER_MIN": "1"})
    url = f"/v1/sky?{here}&screens=nasa"
    assert app.handle(url, {}, "10.0.0.1")[0] == 403
    assert app.handle("/health", {"x-origin-auth": "nope"}, "10.0.0.1")[0] == 403
    ok = {"x-origin-auth": EDGE, "cf-connecting-ip": "5.5.5.5"}
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
    assert A.check_env({"SKY_CONTACT": "a@b.test", "ORIGIN_SECRET": EDGE}) == []


def test_container_health_check_from_loopback_needs_no_origin_secret(tmp_path):
    app, here, _ = _app(tmp_path, {"ORIGIN_SECRET": EDGE})
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
    app, here, _ = _app(tmp_path, {"RATE_PER_MIN": "1", "ORIGIN_SECRET": EDGE})
    ok = {"x-origin-auth": EDGE}
    replies = [app.handle("/v1/sky?lat=1", {}, "1.1.1.1"),                    # 403
               app.handle("/v1/sky?lat=x", ok, "1.1.1.1"),                    # 400
               app.handle("/v1/sky?lat=x", ok, "1.1.1.1"),                    # 429
               app.handle("/nope", ok, "1.1.1.2")]                            # 404
    assert [r[0] for r in replies] == [403, 400, 429, 404]
    assert all(r[2]["Cache-Control"] == "private, no-store" for r in replies)


# --- /health must not reveal the private screen (amendment A) --------------------------------

def _private_health_app(tmp_path, env=None):
    app, _, _ = _app(tmp_path, env, sources=_sources("starlink", "military", S.HOBBY_SOURCE))
    app.engine.dropped = {"starlink": 0, "military": 1, "hobby": 2}
    return app


def test_public_health_hides_the_private_sources(tmp_path):
    app = _private_health_app(tmp_path, {"SKY_SPY_KEY": "k" * 24})
    for hdrs, ip in (({}, "1.1.1.1"), ({"authorization": "Bearer wrong"}, "1.1.1.1"), ({"authorization": "k" * 24}, "1.1.1.1"),
                     ({}, "127.0.0.1"), ({}, "::1")):          # loopback is not trusted (a sidecar shares it)
        status, body, _ = app.handle("/health", hdrs, ip)
        assert status == 200 and body["ok"] is True
        assert list(body["sources"]) == ["starlink"] and body["dropped_old"] == {"starlink": 0}
        raw = json.dumps(body)
        assert "military" not in raw and "hobby" not in raw


def test_trusted_health_shows_everything(tmp_path):
    app = _private_health_app(tmp_path, {"SKY_SPY_KEY": "k" * 24})
    for hdrs, ip in (({"authorization": "Bearer " + "k" * 24}, "1.1.1.1"), ({"authorization": "Bearer " + "k" * 24}, "127.0.0.1")):
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
    assert app.handle("/health", {}, "9.9.9.9")[0] == 429       # the plain health check is charged too


def test_plain_health_is_charged_to_the_address_limit_but_loopback_is_exempt(tmp_path):
    app, here, _ = _app(tmp_path, {"RATE_PER_MIN": "2"})
    status = [app.handle("/health", {}, "9.9.9.9") for _ in range(3)]
    assert [s[0] for s in status] == [200, 200, 429] and status[2][1] == {"error": "slow down", "poll": 300}
    assert status[2][2]["Retry-After"] == "60" and "sources" not in status[2][1]
    sky = f"/v1/sky?{here}&screens=nasa"
    assert [app.handle(p, {}, "8.8.8.8")[0] for p in (sky, "/health", sky)] == [200, 200, 429]   # one budget
    for ip in ("127.0.0.1", "::1"):                              # the container health check
        assert [app.handle("/health", {}, ip)[0] for _ in range(10)] == [200] * 10
    assert [app.handle(sky, {}, "127.0.0.1")[0] for _ in range(3)] == [200, 200, 429]   # loopback /health cost nothing
    edge, _, _ = _app(tmp_path, {"RATE_PER_MIN": "1", "ORIGIN_SECRET": EDGE})
    assert [edge.handle("/health", {}, "127.0.0.1")[0] for _ in range(3)] == [200] * 3
    ok = {"x-origin-auth": EDGE, "cf-connecting-ip": "7.7.7.7"}
    assert [edge.handle("/health", ok, "10.0.0.1")[0] for _ in range(2)] == [200, 429]


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
    out = capsys.readouterr().out
    assert "[sky] background error: RuntimeError\n" in out and "boom" not in out      # type only


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
    out = capsys.readouterr().out
    assert "[sky] background error: OSError\n" in out and "disk gone" not in out      # type only


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
    srv = A.SkyServer(("127.0.0.1", 0), A.make_handler(app, slots))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def test_port_in_use_raises_the_bind_error_not_an_attribute_error(tmp_path):
    app, _, _ = _app(tmp_path)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        with pytest.raises(OSError) as e:
            A.SkyServer(("127.0.0.1", port), A.make_handler(app, None))
        exc = e.value
        assert not isinstance(exc, AttributeError)
        assert not isinstance(exc.__context__, AttributeError)
        assert not isinstance(exc.__cause__, AttributeError)
    finally:
        blocker.close()


def test_server_close_is_idempotent(tmp_path):
    app, _, _ = _app(tmp_path)
    srv, _ = _serve(app)
    srv.shutdown()
    srv.server_close()
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


# --- review round 1 ------------------------------------------------------------------------------

KEY = "k" * 24
LONG_CUT = "/v1/sky?lat=1&lon=1&screens=nasa&cut=nasa:" + "1" * 5001     # int() refuses > 4300 digits


def _port(srv):
    return srv.server_address[1]


def _raw(srv, data: bytes, timeout=5) -> bytes:
    """Send raw bytes on one connection and read until the server closes it."""
    with socket.create_connection(("127.0.0.1", _port(srv)), timeout=timeout) as c:
        c.sendall(data)
        out = b""
        while True:
            try:
                chunk = c.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            out += chunk
    return out


def _parse(reply: bytes):
    head, _, body = reply.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return status, hdrs, body


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# 1. hostile targets ------------------------------------------------------------------------------

@pytest.mark.parametrize("target", [LONG_CUT, "/v1/sky?lat=1&lon=1&screens=nasa&cut=nasa:007",
                                    "http://[x/v1/sky", "//[x/v1/sky?lat=1&lon=1&screens=nasa"],
                         ids=["5001-digit-cut", "3-char-cut", "absolute-bad-ipv6", "netloc-bad-ipv6"])
def test_hostile_targets_are_a_400_not_a_crash(tmp_path, target):
    app, _, _ = _app(tmp_path)
    status, body, hdr = app.handle(target, {}, "1.1.1.1")
    assert status == 400 and hdr["Cache-Control"] == "private, no-store" and "error" in body


@pytest.mark.parametrize("line", [b"GET " + LONG_CUT.encode() + b" HTTP/1.0", b"GET http://[x/v1/sky HTTP/1.0"],
                         ids=["5001-digit-cut", "absolute-bad-ipv6"])
def test_real_server_answers_hostile_targets_with_400_and_no_traceback(tmp_path, capsys, line):
    app, _, _ = _app(tmp_path)
    srv, _ = _serve(app)
    try:
        status, hdrs, body = _parse(_raw(srv, line + b"\r\n\r\n"))
    finally:
        _stop(srv)
    assert status == 400 and hdrs["cache-control"] == "private, no-store" and "error" in json.loads(body)
    cap = capsys.readouterr()
    assert "Traceback" not in cap.err and "127.0.0.1" not in cap.err + cap.out


class RaisingEngine(StubEngine):
    def tick_age(self, now):
        raise RuntimeError("secret detail lat=1")


def test_real_server_turns_an_internal_error_into_a_plain_500(tmp_path, capsys):
    app, _, _ = _app(tmp_path, engine=RaisingEngine(T0, 0.0))
    srv, _ = _serve(app)
    try:
        status, hdrs, body = _parse(_raw(srv, b"GET /v1/sky?lat=1&lon=2&screens=nasa HTTP/1.0\r\n\r\n"))
    finally:
        _stop(srv)
    assert status == 500 and json.loads(body) == {"error": "internal error"}
    assert hdrs["cache-control"] == "private, no-store" and hdrs["content-type"] == "application/json"
    cap = capsys.readouterr()
    assert "[http] handler error: RuntimeError" in cap.out
    logs = cap.out + cap.err
    assert "secret" not in logs and "lat=" not in logs and "127.0.0.1" not in logs and "Traceback" not in logs


# 2. key guessing and the limiter ---------------------------------------------------------------

def test_limiter_key_is_the_ipv4_address_or_the_ipv6_64():
    k = A.limiter_key
    assert k("5.5.5.5") == "5.5.5.5" and k("::ffff:5.5.5.5") == "5.5.5.5"
    assert k("2001:db8:1:2::1") == k("2001:db8:1:2:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
    assert k("2001:db8:1:3::1") != k("2001:db8:1:2::1")
    assert k("nonsense") == k("") == k("1.2.3.4.5") == k("2001:db8::1/64") == "invalid"


def test_an_ipv6_client_cannot_dodge_the_limit_by_rotating_its_64(tmp_path):
    app, here, _ = _app(tmp_path, {"ORIGIN_SECRET": EDGE, "RATE_PER_MIN": "2"})
    url = f"/v1/sky?{here}&screens=nasa"
    via = lambda ip: app.handle(url, {"x-origin-auth": EDGE, "cf-connecting-ip": ip}, "10.0.0.1")[0]
    assert [via(f"2001:db8:1:2::{i:x}") for i in range(1, 4)] == [200, 200, 429]
    assert via("2001:db8:1:3::1") == 200                               # another /64 is another caller
    assert [via(bad) for bad in ("junk", "more junk", "")] == [200, 200, 429]    # unparsable: one bucket
    app2, _, _ = _app(tmp_path, {"RATE_PER_MIN": "1"})
    assert [app2.handle(url, {}, ip)[0] for ip in ("2001:db8::1", "2001:db8::2")] == [200, 429]


def test_wrong_keys_share_one_server_wide_budget(tmp_path, monkeypatch):
    assert A.WRONG_KEY_PER_MIN == 30
    monkeypatch.setattr(A, "WRONG_KEY_PER_MIN", 3)
    app, here, clock = _app(tmp_path, {"SKY_SPY_KEY": KEY, "RATE_PER_MIN": "1000"})
    compared = []
    real = app.key_ok
    app.key_ok = lambda h: (compared.append(h), real(h))[1]
    url = f"/v1/sky?{here}&screens=spy,nasa"
    wrong = {"authorization": "Bearer guess"}
    assert app.handle(url, wrong, "1.1.1.1")[1]["screens"]["spy"] == {"locked": True}
    assert app.handle("/health", wrong, "1.1.1.2")[0] == 200          # /health guesses count too
    assert app.handle(url, wrong, "1.1.1.3")[0] == 200                 # third wrong key: budget now used up
    assert len(compared) == 3
    assert app.handle(url, wrong, "1.1.1.4")[0] == 429
    assert app.handle(url, {"authorization": "Bearer " + KEY}, "1.1.1.5")[0] == 429   # even the right key ...
    assert app.handle("/health", {"authorization": "Bearer " + KEY}, "1.1.1.6")[0] == 429
    assert len(compared) == 3                                         # ... is not compared
    assert app.handle(url, {}, "1.1.1.7")[1]["screens"]["nasa"]["count"] == 80        # public screens unaffected
    clock.t += 60
    status, body, _ = app.handle(url, {"authorization": "Bearer " + KEY}, "1.1.1.8")
    assert status == 200 and body["screens"]["spy"] == {"unavailable": True}     # unlocked (engine now stale)


def test_limiter_table_is_capped_and_newcomers_share_an_overflow_bucket():
    clock = Clock()
    lim = A.Limiter(2, 10, clock, max_keys=3)
    assert all(lim.allow_key(k) for k in "abc")
    assert lim.allow_key("d") and lim.allow_key("e") and not lim.allow_key("f")      # d, e, f share "overflow"
    assert lim.allow_key("a") and lim.tracked() == 4
    clock.t += 60
    assert lim.allow_key("f") and lim.tracked() == 1


def test_app_limiter_table_cap(tmp_path, monkeypatch):
    assert A.LIMITER_MAX_KEYS == 50000
    monkeypatch.setattr(A, "LIMITER_MAX_KEYS", 2)
    app, here, _ = _app(tmp_path, {"RATE_PER_MIN": "1"})
    url = f"/v1/sky?{here}&screens=nasa"
    assert [app.handle(url, {}, f"7.7.7.{i}")[0] for i in range(4)] == [200, 200, 200, 429]
    assert app.limiter.tracked() == 3


# 3. replies made by http.server itself ---------------------------------------------------------

@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
def test_other_methods_get_a_json_405(tmp_path, method):
    app, _, _ = _app(tmp_path)
    srv, _ = _serve(app)
    try:
        reply = _raw(srv, f"{method} /v1/sky?lat=1&lon=1&screens=nasa&ECHO HTTP/1.0\r\nContent-Length: 0\r\n\r\n".encode())
    finally:
        _stop(srv)
    status, hdrs, body = _parse(reply)
    assert status == 405 and hdrs["allow"] == "GET" and hdrs["cache-control"] == "private, no-store"
    assert hdrs["content-type"] == "application/json" and b"ECHO" not in reply
    if method == "HEAD":
        assert body == b""
    else:
        assert json.loads(body) == {"error": "Method Not Allowed"}


@pytest.mark.parametrize("data, code", [
    (b"GARBAGE /ECHO<script>\r\n\r\n", 400),                              # malformed request line
    (b"GET /ECHO HTTP/9z\r\n\r\n", 400),                                   # bad version
    (b"GET /v1/sky?lat=1&lon=1&screens=nasa&ECHO\r\n\r\n", 400),           # HTTP/0.9: would get no headers at all
    (b"GET /" + b"E" * 65532, 414),                                        # request line over 64 KB
    (b"GET /ECHO HTTP/1.0\r\n" + b"".join(b"X-Echo-%d: ECHO\r\n" % i for i in range(101)) + b"\r\n", 431),
], ids=["garbage-line", "bad-version", "http-0.9", "uri-too-long", "too-many-headers"])
def test_errors_from_http_server_itself_are_json_no_store_and_echo_nothing(tmp_path, data, code):
    app, _, _ = _app(tmp_path)
    srv, _ = _serve(app)
    try:
        reply = _raw(srv, data)
    finally:
        _stop(srv)
    status, hdrs, body = _parse(reply)
    assert status == code and hdrs["cache-control"] == "private, no-store" and hdrs["content-type"] == "application/json"
    assert set(json.loads(body)) == {"error"}
    assert b"ECHO" not in reply and b"<script>" not in reply and b"EEEE" not in reply and len(reply) < 600


def test_each_connection_serves_one_request(tmp_path):
    app, _, _ = _app(tmp_path)
    assert A.make_handler(app).protocol_version == "HTTP/1.0"
    srv, _ = _serve(app)
    req = b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n"
    try:
        reply = _raw(srv, req * 2)
    finally:
        _stop(srv)
    assert reply.count(b"HTTP/1.0 ") == 1


# 4. connections and threads are bounded --------------------------------------------------------

def test_connection_limits_defaults():
    assert A.MAX_CONNECTIONS == 64 and A.REQUEST_DEADLINE_S == 15 and A.MAX_IN_PROGRESS == 32


def test_connections_over_the_cap_are_closed_without_a_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "MAX_CONNECTIONS", 4)
    app, here, _ = _app(tmp_path)
    before = threading.active_count()
    srv, base = _serve(app)
    idle = []
    try:
        idle = [socket.create_connection(("127.0.0.1", _port(srv)), timeout=5) for _ in range(4)]
        for c in idle:
            c.sendall(b"GET /health HTTP/1.0\r\n")                     # headers never finished
        assert _wait(lambda: srv.open_connections() == 4)
        for _ in range(10):
            with socket.create_connection(("127.0.0.1", _port(srv)), timeout=5) as extra:
                extra.sendall(b"GET /health HTTP/1.0\r\n\r\n")
                try:
                    assert extra.recv(1024) == b""                     # closed, no reply
                except ConnectionResetError:
                    pass
        # serve_forever thread + watchdog + 4 handlers
        assert threading.active_count() <= before + 2 + 4
        assert srv.open_connections() == 4
        for c in idle:
            c.close()
        idle = []
        assert _wait(lambda: srv.open_connections() == 0)
        with urllib.request.urlopen(f"{base}/v1/sky?{here}&screens=nasa", timeout=5) as r:
            assert r.status == 200
    finally:
        for c in idle:
            c.close()
        _stop(srv)
    assert _wait(lambda: threading.active_count() <= before)


def test_a_client_trickling_header_bytes_is_cut_off_at_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "REQUEST_DEADLINE_S", 0.3)
    app, here, _ = _app(tmp_path)
    before = threading.active_count()
    srv, base = _serve(app)
    try:
        with socket.create_connection(("127.0.0.1", _port(srv)), timeout=5) as c:
            c.sendall(f"GET /v1/sky?{here}&screens=nasa HTTP/1.0\r\nX-Slow: ".encode())
            c.settimeout(0.05)
            start, got, cut = time.monotonic(), b"", None
            while time.monotonic() - start < 3:
                try:
                    c.send(b"a")
                    chunk = c.recv(1024)
                except socket.timeout:
                    continue
                except OSError:                                       # reset / broken pipe: cut off
                    chunk = b""
                got += chunk
                if not chunk:
                    cut = time.monotonic() - start
                    break
        assert cut is not None and 0.25 <= cut <= 1.2 and got == b""
        assert _wait(lambda: srv.open_connections() == 0)
        with urllib.request.urlopen(f"{base}/v1/sky?{here}&screens=nasa", timeout=5) as r:
            assert r.status == 200
    finally:
        _stop(srv)
    assert _wait(lambda: threading.active_count() <= before)


# 5. unknown paths --------------------------------------------------------------------------------

def test_unknown_paths_are_charged_to_the_rate_limit(tmp_path):
    app, here, _ = _app(tmp_path, {"RATE_PER_MIN": "2"})
    assert [app.handle(p, {}, "4.4.4.4")[0] for p in ("/admin", "/x", "/admin")] == [404, 404, 429]
    assert app.handle(f"/v1/sky?{here}&screens=nasa", {}, "4.4.4.4")[0] == 429


def test_log_line_names_a_route_never_the_path(tmp_path, capsys):
    app, _, _ = _app(tmp_path)
    srv, _ = _serve(app)
    try:
        for target in (b"/SECRETPATH", b"/health", b"http://[x/SECRETPATH"):
            _raw(srv, b"GET " + target + b" HTTP/1.0\r\n\r\n")
    finally:
        _stop(srv)
    out = capsys.readouterr().out
    assert "[http] 404 other" in out and "[http] 200 /health" in out and "[http] 400 other" in out
    assert "SECRET" not in out


# 6, 7. start-up settings --------------------------------------------------------------------------

def test_check_env_refuses_a_short_origin_secret():
    with pytest.raises(SystemExit, match="ORIGIN_SECRET must be at least 24"):
        A.check_env({"SKY_CONTACT": "a@b.test", "ORIGIN_SECRET": "edge"})


@pytest.mark.parametrize("name, bad", [
    ("RATE_PER_MIN", "0"), ("RATE_PER_MIN", "10001"), ("RATE_PER_MIN", "abc"), ("RATE_PER_MIN", "1.5"),
    ("RATE_PER_MIN", " 30"), ("RATE_PER_MIN", ""), ("RATE_PER_MIN", "٣"), ("CALC_PER_SEC", "0"),
    ("CALC_PER_SEC", "1001"), ("CALC_PER_SEC", "-1"), ("PORT", "0"), ("PORT", "65536"), ("PORT", "http"),
])
def test_check_env_refuses_numbers_out_of_range(name, bad):
    with pytest.raises(SystemExit, match=name):
        A.check_env({"SKY_CONTACT": "a@b.test", "ORIGIN_SECRET": EDGE, name: bad})


def test_check_env_accepts_numbers_in_range():
    env = {"SKY_CONTACT": "a@b.test", "ORIGIN_SECRET": EDGE}
    for extra in ({"RATE_PER_MIN": "1", "CALC_PER_SEC": "1", "PORT": "1"},
                  {"RATE_PER_MIN": "10000", "CALC_PER_SEC": "1000", "PORT": "65535"}):
        assert A.check_env(dict(env, **extra)) == []


# 8. strict lat/lon --------------------------------------------------------------------------------

@pytest.mark.parametrize("lat", ["%EF%BC%91", "1_0", "%201", "1%20", "1%0A", "+1", "1.", ".5", "1.123456789",
                                 "1e1", "0x1", "1234", "--1", "%D9%A3"])
def test_lat_and_lon_must_be_plain_ascii_decimals(tmp_path, lat):
    app, _, _ = _app(tmp_path)
    assert app.handle(f"/v1/sky?lat={lat}&lon=1&screens=nasa", {}, "1.1.1.1")[0] == 400
    assert app.handle(f"/v1/sky?lat=1&lon={lat}&screens=nasa", {}, "1.1.1.2")[0] == 400


def test_plain_decimals_are_accepted():
    assert A.parse_request("lat=-0.0&lon=-179.12345678&screens=nasa")[:2] == (0.0, -179.1)
    assert A.parse_request("lat=90&lon=180&screens=nasa")[:2] == (90.0, 180.0)


# 10. start-up build ------------------------------------------------------------------------------

def test_start_screens_builds_from_disk_and_records_it(tmp_path):
    store = Store(str(tmp_path), _sources("stations"), None, "UA", log=lambda m: None)
    store.sources["stations"].elems = E.parse_omm_json(_omm_body(25544))
    eng = Engine()
    state = A.start_screens(eng, store, {}, False, T0)
    eng.tick(T0)
    assert eng.has("nasa") and state["last_rebuild"] == T0


def test_start_screens_survives_a_failed_build(tmp_path, capsys, monkeypatch):
    def boom(*a):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(A, "rebuild", boom)
    store = Store(str(tmp_path), _sources("stations"), None, "UA", log=lambda m: None)
    eng = Engine()
    state = A.start_screens(eng, store, {}, False, T0)
    assert state["last_rebuild"] == 0.0                 # the fetcher rebuilds on its first pass
    eng.tick(T0)
    assert not eng.has("nasa")
    out = capsys.readouterr().out
    assert "[sky] start-up build failed: RuntimeError" in out and "secret" not in out


def test_extra_source_uses_decodo_when_its_token_is_set():
    base = {"SKY_SPY_KEY": "k" * 24, "SPY_HOBBY_URL": "https://example.test/x.zip", "SPY_PROXY_URL": "http://p.test:1"}
    plain = {s.name: s for s in A.build_sources(base)[0]}[S.HOBBY_SOURCE]
    assert plain.proxy == "http://p.test:1" and plain.http_get is None
    via = {s.name: s for s in A.build_sources(dict(base, SPY_PROXY_KEY="dG9rZW4="))[0]}[S.HOBBY_SOURCE]
    assert via.proxy is None and callable(via.http_get)
    with pytest.raises(SystemExit, match="SPY_PROXY_KEY") as e:
        A.check_env({"SKY_CONTACT": "a@b.test", "SPY_PROXY_KEY": "bad token\n"})
    assert "bad token" not in str(e.value)
