"""Server status service (esp32-desk-dials).

Watches Hetzner Cloud servers, every resource on one or more Coolify instances, and
each public domain, and serves one small JSON summary to the desk dials:

    GET /status   Authorization: Bearer <SERVICE_KEY>
    GET /health   no auth; is the monitor itself alive

What counts as a problem
  down   Hetzner server not running; Coolify server unreachable; a resource that is
         degraded, restarting, or unhealthy; a resource whose domain fails its HTTPS
         check (connection error or HTTP 5xx). A stopped ("exited") resource WITH a
         domain is down, because its site is.
  warn   starting / health unknown after start; a server's CPU above CPU_WARN %;
         a Coolify API or the Hetzner API this service could not read.
  stopped  exited, no domain: something you turned off. Grey on the dial, not an alert.
  up     running (healthy, or no health check configured) and its domain answers.

Environment
  SERVICE_KEY            bearer key the dials send
  HETZNER_TOKEN          Hetzner Cloud API token (Read only is enough); optional
  COOLIFY_INSTANCES      "NAME=URL,NAME=URL", e.g.
                         "VAROAPPS-1=http://89.167.57.129:8000,VAROAPPS-2=http://coolify:8080"
  COOLIFY_TOKEN_<NAME>   one API token per instance, NAME upper-cased with - -> _,
                         e.g. COOLIFY_TOKEN_VAROAPPS_1 (permission "read" is enough)
  EXTRA_URLS             optional "Label=https://...,Label=https://..." to check too
  IGNORE                 optional comma list of resource names to leave out
  PORT_CHECKS            optional "Label=host:port,..." TCP services to check (mail etc.).
                         465/993/995 are checked over TLS; 25/587/465 must greet with 220,
                         993/143 with "* OK". A TLS certificate within CERT_WARN_DAYS
                         (default 14) of expiry is a warning.
  POLL_SECONDS           default 60
  CPU_WARN               default 90 (percent, 5-minute average)
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import socket
import ssl

import requests

HETZNER = os.environ.get("HETZNER_API", "https://api.hetzner.cloud/v1")   # overridable for tests
UA = {"User-Agent": "esp32-desk-dials-status/1.0"}


# ------------------------------------------------------------------ classification

def classify_coolify(status: str, has_domain: bool) -> str:
    """Coolify writes '<state>:<health>' (running:healthy, running:unknown,
    degraded:unhealthy, restarting:unknown, starting:unknown, paused:unknown...) or
    plain 'exited'; a ':excluded' suffix means health checks are excluded."""
    s = (status or "").lower().replace(":excluded", "")
    state, _, health = s.partition(":")
    if state in ("exited", "stopped", "dead", ""):
        return "down" if has_domain else "stopped"
    if state in ("degraded", "restarting") or health == "unhealthy":
        return "down"
    if state in ("starting", "paused", "created") or health == "starting":
        return "warn"
    if state == "running":
        return "up"
    return "warn"


SEVERITY = {"down": 3, "warn": 2, "stopped": 1, "up": 0}


def worse(a: str, b: str) -> str:
    return a if SEVERITY[a] >= SEVERITY[b] else b


def first_domain(fqdn) -> str | None:
    if not fqdn:
        return None
    for part in str(fqdn).split(","):
        part = part.strip()
        if part.startswith("http://") or part.startswith("https://"):
            host = urlparse(part).hostname or ""
            if host and not host.endswith("sslip.io"):
                return "https://" + host + "/" if part.startswith("https://") else part
    return None


# ------------------------------------------------------------------ collectors

def http_check(url: str) -> tuple[bool, str]:
    try:
        r = requests.get(url, timeout=10, allow_redirects=True, headers=UA)
        if r.status_code >= 500:
            return False, f"HTTP {r.status_code}"
        return True, f"HTTP {r.status_code}"
    except requests.exceptions.SSLError:
        return False, "certificate error"
    except requests.exceptions.ConnectTimeout:
        return False, "timed out"
    except requests.exceptions.ConnectionError:
        return False, "no connection"
    except requests.exceptions.Timeout:
        return False, "timed out"


def hetzner_servers(token: str, cpu_warn: float) -> list[dict]:
    h = {"Authorization": f"Bearer {token}", **UA}
    servers, page = [], 1
    while True:
        r = requests.get(f"{HETZNER}/servers", params={"page": page, "per_page": 50}, headers=h, timeout=15)
        r.raise_for_status()
        d = r.json()
        servers += d.get("servers", [])
        nxt = (((d.get("meta") or {}).get("pagination") or {}).get("next_page"))
        if not nxt:
            break
        page = nxt
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)
    out = []
    for s in servers:
        cpu = None
        if s.get("status") == "running":
            try:
                m = requests.get(f"{HETZNER}/servers/{s['id']}/metrics", headers=h, timeout=15, params={
                    "type": "cpu", "start": start.isoformat(), "end": end.isoformat(), "step": 60})
                m.raise_for_status()
                series = (((m.json().get("metrics") or {}).get("time_series") or {}).get("cpu") or {}).get("values") or []
                vals = [float(v[1]) for v in series if len(v) > 1 and v[1] not in (None, "")]
                if vals:
                    # Hetzner sums CPU over all cores (a 4-core box tops out at 400%);
                    # divide by the core count so 100% means the whole server is busy.
                    cores = ((s.get("server_type") or {}).get("cores")) or 1
                    cpu = round(sum(vals) / len(vals) / cores, 1)
            except Exception:
                cpu = None
        state = "up" if s.get("status") == "running" else "down"
        if state == "up" and cpu is not None and cpu >= cpu_warn:
            state = "warn"
        ipv4 = (((s.get("public_net") or {}).get("ipv4") or {}).get("ip"))
        out.append({"name": s.get("name"), "status": s.get("status"), "cpu": cpu, "state": state, "ip": ipv4})
    return out


def coolify_get(base: str, token: str, path: str):
    r = requests.get(base.rstrip("/") + "/api/v1" + path, headers={"Authorization": f"Bearer {token}", **UA}, timeout=15)
    if r.status_code >= 400:
        # Coolify says why (IP not allowed, missing ability, bad token) in the body; keep it.
        try:
            msg = (r.json() or {}).get("message") or r.text
        except ValueError:
            msg = r.text
        raise CoolifyError(r.status_code, str(msg)[:160])
    return r.json()


class CoolifyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message = status, message


def coolify_instance(name: str, base: str, token: str) -> dict:
    """Servers (reachable/usable), every resource with the server it runs on, and the
    domains to check."""
    servers = coolify_get(base, token, "/servers")
    resources = coolify_get(base, token, "/resources")
    domains: dict[str, str] = {}
    for r in resources:
        d = first_domain(r.get("fqdn"))
        if d:
            domains[r.get("uuid")] = d
    # Services keep their domains on their sub-applications.
    for r in resources:
        if r.get("type") == "service" and r.get("uuid") not in domains:
            try:
                for app in coolify_get(base, token, f"/services/{r['uuid']}/applications") or []:
                    d = first_domain(app.get("fqdn"))
                    if d:
                        domains[r["uuid"]] = d
                        break
            except Exception:
                pass
    placed = []
    for s in servers:
        srv = {"name": s.get("name"), "reachable": bool(s.get("is_reachable")), "usable": bool(s.get("is_usable"))}
        try:
            items = coolify_get(base, token, f"/servers/{s['uuid']}/resources") or []
        except Exception:
            items = []
        for it in items:
            # Coolify calls the server it runs on "localhost"; the instance name means more.
            where = name if (srv["name"] or "").lower() in ("localhost", "") else srv["name"]
            placed.append({"uuid": it.get("uuid"), "name": it.get("name"), "type": it.get("type"),
                           "status": it.get("status"), "server": where, "coolify": name,
                           "domain": domains.get(it.get("uuid"))})
        srv["resources"] = len(items)
        s["_srv"] = srv
    return {"servers": [s["_srv"] for s in servers], "resources": placed}


# ------------------------------------------------------------------ port checks

IMPLICIT_TLS = {465, 993, 995}
GREETING = {25: b"220", 465: b"220", 587: b"220", 143: b"* OK", 993: b"* OK", 110: b"+OK", 995: b"+OK"}


def port_check(host: str, port: int, warn_days: int) -> tuple[str, str]:
    """('up'|'warn'|'down', why). Connects, does TLS on implicit-TLS ports (verifying the
    certificate against host), and reads the greeting where the protocol has one."""
    try:
        raw = socket.create_connection((host, port), timeout=10)
    except OSError as e:
        return "down", "no connection" if not isinstance(e, socket.timeout) else "timed out"
    sock = raw
    try:
        cert_days = None
        if port in IMPLICIT_TLS:
            ctx = ssl.create_default_context()
            try:
                sock = ctx.wrap_socket(raw, server_hostname=host)
            except ssl.SSLCertVerificationError as e:
                return "down", "certificate " + ("expired" if "expired" in str(e) else "invalid")
            except (ssl.SSLError, OSError):
                return "down", "TLS failed"
            exp = sock.getpeercert().get("notAfter")
            if exp:
                cert_days = int((ssl.cert_time_to_seconds(exp) - time.time()) // 86400)
        want = GREETING.get(port)
        if want:
            sock.settimeout(10)
            try:
                hello = sock.recv(256)
            except OSError:
                return "down", "no greeting"
            if not hello.startswith(want):
                return "down", "bad greeting"
        if cert_days is not None and cert_days < warn_days:
            return "warn", f"certificate expires in {cert_days}d"
        return "up", "ok"
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ monitor

class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.summary = None
        self.last_ok = 0.0
        self.hetzner_token = os.environ.get("HETZNER_TOKEN", "").strip()
        self.instances = []
        for part in os.environ.get("COOLIFY_INSTANCES", "").split(","):
            if "=" in part:
                n, u = part.split("=", 1)
                n, u = n.strip(), u.strip()
                tok = os.environ.get("COOLIFY_TOKEN_" + n.upper().replace("-", "_").replace(" ", "_"), "").strip()
                self.instances.append((n, u, tok))
        self.extra = []
        for part in os.environ.get("EXTRA_URLS", "").split(","):
            if "=" in part:
                n, u = part.split("=", 1)
                self.extra.append((n.strip(), u.strip()))
        self.ports = []
        for part in os.environ.get("PORT_CHECKS", "").split(","):
            if "=" in part and ":" in part.split("=", 1)[1]:
                n, hp = part.split("=", 1)
                h, pt = hp.strip().rsplit(":", 1)
                self.ports.append((n.strip(), h.strip(), int(pt)))
        self.cert_warn = int(os.environ.get("CERT_WARN_DAYS", "14"))
        self.ignore = {x.strip().lower() for x in os.environ.get("IGNORE", "").split(",") if x.strip()}
        self.cpu_warn = float(os.environ.get("CPU_WARN", "90"))
        self.poll = max(30, int(os.environ.get("POLL_SECONDS", "60")))

    def collect(self) -> dict:
        problems, servers, items, monitor_errors = [], [], [], []

        if self.hetzner_token:
            try:
                for s in hetzner_servers(self.hetzner_token, self.cpu_warn):
                    servers.append({"name": s["name"], "state": s["state"], "cpu": s["cpu"], "status": s["status"]})
                    if s["state"] == "down":
                        problems.append({"name": s["name"], "where": "Hetzner", "state": "down", "why": f"server {s['status']}"})
                    elif s["state"] == "warn":
                        problems.append({"name": s["name"], "where": "Hetzner", "state": "warn", "why": f"CPU {s['cpu']:.0f}%"})
            except Exception as e:
                monitor_errors.append(f"Hetzner API: {type(e).__name__}")

        for name, base, tok in self.instances:
            if not tok:
                monitor_errors.append(f"{name}: no Coolify token")
                continue
            try:
                inst = coolify_instance(name, base, tok)
            except CoolifyError as e:
                why = "API not allowed from this IP" if "not allowed" in e.message.lower() else \
                      "token rejected" if e.status == 401 else f"API error {e.status}"
                monitor_errors.append(f"Coolify {name}: {why}")
                print(f"[coolify] {name} {base}: {e}", flush=True)
                continue
            except Exception as e:
                monitor_errors.append(f"Coolify {name}: unreachable")
                print(f"[coolify] {name} {base}: {e!r}", flush=True)
                continue
            for srv in inst["servers"]:
                if not srv["reachable"]:
                    label = name if (srv["name"] or "").lower() == "localhost" else srv["name"]
                    problems.append({"name": label, "where": f"Coolify {name}", "state": "down", "why": "server unreachable"})
            for r in inst["resources"]:
                if (r["name"] or "").lower() in self.ignore:
                    continue
                items.append(r)

        # HTTPS checks, in parallel.
        checks = {r["uuid"]: r["domain"] for r in items if r.get("domain")}
        extra = {f"extra:{n}": u for n, u in self.extra}
        urls = {**checks, **extra}
        results = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {k: ex.submit(http_check, u) for k, u in urls.items()}
            pfuts = {(n, h, pt): ex.submit(port_check, h, pt, self.cert_warn) for n, h, pt in self.ports}
            for k, f in futs.items():
                results[k] = f.result()
            port_results = {k: f.result() for k, f in pfuts.items()}

        dots = []
        counts = {"up": 0, "warn": 0, "down": 0, "stopped": 0}
        for r in sorted(items, key=lambda x: ((x["server"] or ""), (x["name"] or "").lower())):
            state = classify_coolify(r["status"], bool(r.get("domain")))
            why = r["status"] or "unknown"
            res = results.get(r["uuid"])
            if res is not None and not res[0]:
                state = worse(state, "down")
                why = f"{r['domain'].split('//')[1].rstrip('/')}: {res[1]}"
            elif state == "down" and res is not None and res[0]:
                # Coolify says not running but the site answers — trust the site, flag softly.
                state, why = "warn", f"Coolify says {r['status']}, site answers"
            counts[state] += 1
            dots.append(state[0].upper())
            if state in ("down", "warn"):
                problems.append({"name": r["name"], "where": r["server"], "state": state, "why": why})
        for sv in servers:                 # Hetzner servers get a dot each too
            counts[sv["state"]] += 1
            dots.append(sv["state"][0].upper())
        for (n, h, pt), (state, why) in port_results.items():
            counts[state] += 1
            dots.append(state[0].upper())
            if state != "up":
                problems.append({"name": n, "where": f"{h}:{pt}", "state": state, "why": why})
        for (n, u) in self.extra:
            ok, why = results[f"extra:{n}"]
            state = "up" if ok else "down"
            counts[state] += 1
            dots.append(state[0].upper())
            if not ok:
                problems.append({"name": n, "where": u.split("//")[-1].rstrip("/"), "state": "down", "why": why})
        for e in monitor_errors:
            problems.append({"name": e.split(":")[0], "where": "monitor", "state": "warn", "why": e.split(":", 1)[-1].strip()})

        problems.sort(key=lambda p: -SEVERITY[p["state"]])
        overall = "down" if any(p["state"] == "down" for p in problems) else ("warn" if problems else "up")
        return {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "overall": overall,
                "counts": {**counts, "total": sum(counts.values())},
                "servers": servers, "problems": problems[:20], "dots": "".join(dots)[:160]}

    def loop(self):
        while True:
            try:
                s = self.collect()
                with self.lock:
                    self.summary = s
                    self.last_ok = time.time()
                print(f"[status] {s['overall']} {s['counts']} problems={len(s['problems'])}"
                      + "".join(f"\n  {p['state']:5} {p['name']} ({p['where']}): {p['why']}" for p in s['problems']), flush=True)
            except Exception:
                traceback.print_exc()
            time.sleep(self.poll)


MON: Monitor | None = None
KEY = ""


class Handler(BaseHTTPRequestHandler):
    server_version = "DialStatus/1.0"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            with MON.lock:
                age = time.time() - MON.last_ok if MON.last_ok else None
            return self.send(200, {"ok": MON.last_ok > 0, "ageSeconds": round(age) if age is not None else None})
        if path == "/status":
            h = self.headers.get("Authorization", "")
            given = h[7:].strip() if h.startswith("Bearer ") else ""
            if not given or not hmac.compare_digest(given.encode(), KEY.encode()):
                return self.send(401, {"error": "Missing or wrong key."})
            with MON.lock:
                s = MON.summary
                age = time.time() - MON.last_ok if MON.last_ok else None
            if not s:
                return self.send(503, {"error": "First check still running."})
            return self.send(200, {**s, "ageSeconds": round(age)})
        return self.send(404, {"error": "not found"})


def main():
    global MON, KEY
    KEY = os.environ["SERVICE_KEY"]
    MON = Monitor()
    threading.Thread(target=MON.loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"status service on :{port}; coolify={[n for n, _, _ in MON.instances]} hetzner={'yes' if MON.hetzner_token else 'no'}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
