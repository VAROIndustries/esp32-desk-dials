import json

from conftest import omm
from store import BACKOFF_MAX_S, BACKOFF_MIN_S, Source, Store

GOOD = json.dumps([omm(25544, "ISS")]).encode()


class FakeHttp:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, proxy, timeout):
        self.calls.append((url, headers, proxy))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _store(tmp_path, http, **kw):
    src = Source("stations", "https://example.test/stations", "omm", 7200, **kw)
    return Store(str(tmp_path), [src], http, "UA/1 (me@example.test)", log=lambda m: None), src


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


def test_health_reports_age_and_never_leaks_urls_or_proxy(tmp_path):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD)]), proxy="http://user:pw@proxy.test:1")
    store.refresh(src, 100.0)
    h = store.health(3700.0)
    assert h == {"stations": {"objects": 1, "age_h": 1.0, "last": "ok, 1 objects"}}
    assert "proxy" not in json.dumps(h) and "example.test" not in json.dumps(h)


def test_corrupt_disk_copy_is_ignored(tmp_path):
    (tmp_path / "stations.dat").write_bytes(b"garbage")
    (tmp_path / "stations.meta.json").write_text('{"fetched_at": 5}')
    store, src = _store(tmp_path, FakeHttp([]))
    store.load_disk()
    assert src.elems == [] and store.due(0.0) == [src]


def test_logs_and_health_never_contain_exception_text_or_urls(tmp_path):
    logs = []
    src = Source("stations", "https://example.test/stations", "omm", 7200, proxy="http://user:pw@proxy.test:1")
    store = Store(str(tmp_path), [src], FakeHttp([RuntimeError("SECRET user:pw@proxy.test"), (200, b"{SECRET")]), "UA", log=logs.append)
    store.refresh(src, 0.0)
    store.refresh(src, 4000.0)
    (tmp_path / "stations.dat").write_bytes(b"{SECRET")
    (tmp_path / "stations.meta.json").write_text('{"fetched_at": 5}')
    store.load_disk()
    text = json.dumps(store.health(5000.0)) + " ".join(logs)
    assert logs and "SECRET" not in text and "proxy" not in text and "example.test" not in text


def test_unexpected_parser_or_disk_error_still_backs_off(tmp_path, monkeypatch):
    store, src = _store(tmp_path, FakeHttp([(200, GOOD), (200, GOOD)]))
    monkeypatch.setattr(store, "_parse", lambda s, raw: (_ for _ in ()).throw(KeyError("x")))
    assert store.refresh(src, 0.0) is False and src.next_try == BACKOFF_MIN_S
    monkeypatch.undo()
    monkeypatch.setattr("store.os.replace", lambda a, b: (_ for _ in ()).throw(OSError("full")))
    assert store.refresh(src, 5000.0) is False and src.next_try == 5000.0 + 2 * BACKOFF_MIN_S
