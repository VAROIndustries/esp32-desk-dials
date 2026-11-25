"""Download, validate and keep orbital elements.

Rules this module enforces (CelesTrak usage policy, celestrak.org/usage-policy.php):
  - one group per request, each group at most once per hour;
  - on ANY non-200 reply, stop asking for that source and back off (1 h doubling to 24 h);
  - never retry in a loop.
The last good copy of every source lives on disk, so a restart fetches nothing early.
"""
from __future__ import annotations

import json
import os
import time

import elements

CELESTRAK_INTERVAL_S = 2 * 3600      # GP data changes a few times a day; 2 h is polite
HOBBY_INTERVAL_S = 24 * 3600
BACKOFF_MIN_S = 3600
BACKOFF_MAX_S = 24 * 3600
TIMEOUT_S = 60


class Source:
    def __init__(self, name, url, kind, interval_s, proxy=None):
        self.name, self.url, self.kind, self.interval_s, self.proxy = name, url, kind, interval_s, proxy
        self.elems = []
        self.fetched_at = 0.0        # unix time of the last good copy
        self.next_try = 0.0
        self.backoff = 0
        self.last = "never fetched"


class Store:
    def __init__(self, data_dir, sources, http_get, user_agent, log=print):
        """http_get(url, headers, proxy, timeout) -> (status_code, body_bytes)."""
        self.dir, self.http_get, self.ua, self.log = data_dir, http_get, user_agent, log
        self.sources = {s.name: s for s in sources}
        os.makedirs(data_dir, exist_ok=True)

    def _paths(self, name):
        return os.path.join(self.dir, name + ".dat"), os.path.join(self.dir, name + ".meta.json")

    def _parse(self, src, raw):
        return elements.parse_omm_json(raw) if src.kind == "omm" else elements.parse_tle_text(raw)

    def load_disk(self):
        for src in self.sources.values():
            dat, meta = self._paths(src.name)
            try:
                with open(meta) as f:
                    fetched_at = float(json.load(f)["fetched_at"])
                with open(dat, "rb") as f:
                    src.elems = self._parse(src, f.read())
                src.fetched_at = fetched_at
                src.next_try = fetched_at + src.interval_s
                src.last = "loaded from disk"
            except FileNotFoundError:
                pass
            except Exception as e:      # corrupt copy on disk: ignore it, fetch when due
                self.log(f"[store] {src.name}: disk copy unusable ({type(e).__name__})")

    def due(self, now):
        """Sources ready to fetch, most overdue first."""
        return sorted((s for s in self.sources.values() if now >= s.next_try), key=lambda s: s.next_try)

    def refresh(self, src, now) -> bool:
        """Fetch one source once. Returns True when new elements were accepted."""
        try:
            status, body = self.http_get(src.url, {"User-Agent": self.ua}, src.proxy, TIMEOUT_S)
        except Exception as e:
            return self._fail(src, now, f"network error: {type(e).__name__}")
        if status != 200:
            return self._fail(src, now, f"HTTP {status}")
        try:
            elems = self._parse(src, body)
        except Exception as e:      # any parser failure is a rejected file, never an escape from back-off
            return self._fail(src, now, f"rejected: {type(e).__name__}")
        dat, meta = self._paths(src.name)
        try:
            for path, data in ((dat, body), (meta, json.dumps({"fetched_at": now}).encode())):
                with open(path + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(path + ".tmp", path)
        except OSError as e:
            return self._fail(src, now, f"disk error: {type(e).__name__}")
        src.elems, src.fetched_at, src.backoff = elems, now, 0
        src.next_try = now + src.interval_s
        src.last = f"ok, {len(elems)} objects"
        return True

    def _fail(self, src, now, why) -> bool:
        src.backoff = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, src.backoff * 2))
        src.next_try = now + src.backoff
        src.last = why
        self.log(f"[store] {src.name}: {why}; keeping last good copy, next try in {src.backoff // 60} min")
        return False

    def age_h(self, name, now) -> float:
        s = self.sources.get(name)
        return (now - s.fetched_at) / 3600.0 if s and s.fetched_at else -1.0

    def health(self, now) -> dict:
        return {n: {"objects": len(s.elems), "age_h": round(self.age_h(n, now), 1), "last": s.last}
                for n, s in self.sources.items()}


def requests_get(url, headers, proxy, timeout):
    import requests
    r = requests.get(url, headers=headers, timeout=timeout,
                     proxies={"https": proxy, "http": proxy} if proxy else None)
    return r.status_code, r.content
