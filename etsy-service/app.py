"""Etsy dial service (esp32-desk-dials).

Reads one Etsy shop and serves Today / Month / Year totals to the desk dials:

    GET /summary            Authorization: Bearer <SERVICE_KEY>
    GET /health             no auth; sync status only, no money
    GET /etsy/connect       one-time Etsy authorization (asks for SERVICE_KEY)
    GET /etsy/callback      Etsy redirects here after you approve

Money definitions (all in the shop currency, local time zone TZ):
  gross   item prices - discounts + shipping + gift wrap, for receipts that were not
          canceled or fully refunded, minus partial refunds. Sales tax, VAT and the
          small state delivery fees Etsy collects and remits are NOT income, so they
          are left out.
  fees    everything Etsy charged in the period, from the payment-account ledger:
          transaction, payment processing, offsite ads, Etsy Ads, listing/renewal and
          subscription fees, less fee refunds and credits. Positive number.
  labels  postage labels bought on Etsy in the period. Positive number. Kept apart
          from fees: postage is a cost, not an Etsy fee, and the dial lets you choose.

This service has its OWN Etsy authorization (scope transactions_r) stored in
DATA_DIR. Etsy rotates the refresh token on every refresh, so it must not share a
token file with any other script that uses the same Etsy app.

The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API
but is not endorsed or certified by Etsy, Inc.

Environment:
  ETSY_KEYSTRING, ETSY_SHARED_SECRET   the shop owner's Etsy app
  ETSY_SHOP_ID                         numeric shop id
  SHOP_NAME                            shown in /summary (optional)
  SERVICE_KEY                          bearer key for dials + the connect page
  PUBLIC_URL                           e.g. https://etsydial.example.com (for the OAuth redirect)
  TZ_NAME                              default America/New_York
  DATA_DIR                             default /data  (mount a persistent volume here)
  SYNC_SECONDS                         default 300
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests

API = "https://openapi.etsy.com/v3"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
AUTH_URL = "https://www.etsy.com/oauth/connect"
SCOPES = "transactions_r"

# ------------------------------------------------------------------ money rules

EXCLUDED_STATUSES = {"canceled", "fully refunded"}


def money(m) -> float:
    if not m:
        return 0.0
    return m["amount"] / (m.get("divisor") or 100)


def receipt_row(r: dict) -> dict:
    """The few numbers we keep per receipt."""
    status = (r.get("status") or "").lower()
    counted = status not in EXCLUDED_STATUSES and bool(r.get("is_paid", True))
    gross = 0.0
    if counted:
        gross = money(r.get("subtotal")) + money(r.get("total_shipping_cost")) + money(r.get("gift_wrap_price"))
        refunded = sum(money(f.get("amount")) for f in (r.get("refunds") or [])
                       if (f.get("status") or "").upper() == "SUCCESS")
        gross = max(0.0, gross - refunded)
    return {"t": int(r.get("created_timestamp") or r.get("create_timestamp") or 0),
            "g": round(gross, 2), "c": 1 if counted else 0}


# Ledger entry types. Amounts on the ledger are signed cents: charges are negative,
# refunds/credits positive.
NOT_FEES = {
    "payment_gross", "refund_gross", "recoup", "sales_tax", "sales_tax_refund",
    "buyer_fee",           # state retail delivery fee: collected from the buyer and remitted
    "vat_tax", "vat",      # VAT collected from buyers
    "billing_payment",     # you paying Etsy from a card when the balance went negative
}
FEE_MARKERS = ("transaction", "processing_fee", "offsite_ads", "prolist", "listing", "renew",
               "subscription", "etsy_plus", "seller_driven_traffic", "vat_tax_ep",
               "vat_seller_services", "share_save", "marketing", "pattern",
               "sdt_", "seller_credit")


def ledger_kind(e: dict) -> str:
    """'fee', 'label', or 'other' (payments, payouts, taxes, recoupments...)."""
    t = (e.get("ledger_type") or "").lower()
    if t.startswith("disburse") or t in NOT_FEES:
        return "other"
    if t.startswith("shipping_label") or t == "misc_cr_sl":   # misc_cr_sl: credit on a label
        return "label"
    if any(m in t for m in FEE_MARKERS):
        return "fee"
    return "other"


def ledger_row(e: dict) -> dict:
    return {"t": int(e.get("created_timestamp") or e.get("create_date") or 0),
            "a": int(e.get("amount") or 0), "k": ledger_kind(e),
            "y": (e.get("ledger_type") or "")}


# ------------------------------------------------------------------ to ship

def to_ship_summary(receipts: list, now: datetime) -> dict:
    """Paid, unshipped, not-canceled orders that contain something physical.

    Etsy gives each transaction an expected_ship_date (the ship-by date). An order's
    ship-by is the earliest of its physical items. Overdue = ship-by before today,
    due today = ship-by today, both in the service's time zone.
    """
    today = now.date()
    orders = overdue = due_today = 0
    soonest = None
    for r in receipts:
        if (r.get("status") or "").lower() in EXCLUDED_STATUSES or r.get("is_shipped"):
            continue
        physical = [t for t in (r.get("transactions") or []) if not t.get("is_digital")]
        if not physical:
            continue
        orders += 1
        dates = [t["expected_ship_date"] for t in physical if t.get("expected_ship_date")]
        if not dates:
            continue
        ship_by = datetime.fromtimestamp(min(dates), now.tzinfo).date()
        if ship_by < today:
            overdue += 1
        elif ship_by == today:
            due_today += 1
        if soonest is None or ship_by < soonest:
            soonest = ship_by
    return {"orders": orders, "overdue": overdue, "dueToday": due_today,
            "nextShipBy": f"{soonest:%a %b} {soonest.day}" if soonest else None,
            "nextShipByDate": soonest.isoformat() if soonest else None}


# ------------------------------------------------------------------ periods

def period_bounds(now: datetime) -> dict:
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)
    year0 = month0.replace(month=1)
    return {"today": (int(day0.timestamp()), f"{day0:%a %b} {day0.day}"),
            "month": (int(month0.timestamp()), month0.strftime("%B")),
            "year": (int(year0.timestamp()), str(year0.year))}


def summarize(receipts: dict, ledger: dict, now: datetime) -> dict:
    out = {}
    end = int(now.timestamp()) + 1
    for name, (start, label) in period_bounds(now).items():
        orders = 0
        gross = 0.0
        for r in receipts.values():
            if start <= r["t"] < end:
                orders += r["c"]
                gross += r["g"]
        fee_cents = 0
        label_cents = 0
        for e in ledger.values():
            if start <= e["t"] < end:
                if e["k"] == "fee":
                    fee_cents += e["a"]
                elif e["k"] == "label":
                    label_cents += e["a"]
        fees = round(-fee_cents / 100, 2)
        labels = round(-label_cents / 100, 2)
        out[name] = {"label": label, "orders": orders, "gross": round(gross, 2),
                     "fees": fees, "labels": labels, "net": round(gross - fees, 2)}
    return out


# ------------------------------------------------------------------ Etsy client

class EtsyClient:
    """Minimal Etsy v3 client with its own rotating token stored in DATA_DIR."""

    def __init__(self, keystring: str, secret: str, token_path: str):
        self.keystring = keystring
        self.secret = secret
        self.token_path = token_path
        self.lock = threading.Lock()
        self.tokens = self._load()

    def _load(self):
        try:
            with open(self.token_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def _save(self, tok: dict):
        tok["obtained_at"] = int(time.time())
        tmp = self.token_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(tok, f)
        os.replace(tmp, self.token_path)
        self.tokens = tok

    @property
    def connected(self) -> bool:
        return bool(self.tokens and self.tokens.get("refresh_token"))

    def exchange_code(self, code: str, verifier: str, redirect_uri: str):
        r = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "client_id": self.keystring,
            "redirect_uri": redirect_uri, "code": code, "code_verifier": verifier}, timeout=20)
        r.raise_for_status()
        with self.lock:
            self._save(r.json())

    def _refresh(self):
        r = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "client_id": self.keystring,
            "refresh_token": self.tokens["refresh_token"]}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Etsy token refresh failed ({r.status_code}) - reconnect at /etsy/connect")
        self._save(r.json())

    def _headers(self):
        return {"x-api-key": f"{self.keystring}:{self.secret}" if self.secret else self.keystring,
                "Authorization": f"Bearer {self.tokens['access_token']}"}

    def get(self, path: str, params=None) -> dict:
        if not self.connected:
            raise RuntimeError("Etsy is not connected yet - open /etsy/connect")
        for attempt in range(4):
            with self.lock:
                age = time.time() - self.tokens.get("obtained_at", 0)
                if age > (self.tokens.get("expires_in", 3600) - 300):
                    self._refresh()
                headers = self._headers()
            r = requests.get(API + path, params=params, headers=headers, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2") or 2))
                continue
            if r.status_code == 401 and attempt == 0:
                with self.lock:
                    self._refresh()
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return r.json()

    def paged(self, path: str, params: dict):
        off = 0
        while True:
            d = self.get(path, {**params, "limit": 100, "offset": off})
            res = d.get("results", [])
            yield from res
            off += 100
            if not res or off >= d.get("count", 0):
                break
            time.sleep(0.25)   # personal Etsy apps get 5 requests/second, shared with every other tool on the app


# ------------------------------------------------------------------ store + sync

class Store:
    """Receipts and ledger entries for the current and previous year, kept on disk."""

    FULL_EVERY = 6 * 3600   # re-read the whole year this often (catches late edits)
    OVERLAP = 15 * 60

    def __init__(self, path: str, tz: ZoneInfo):
        self.path = path
        self.tz = tz
        self.lock = threading.Lock()
        self.state = {"receipts": {}, "ledger": {}, "receipts_at": 0, "ledger_at": 0,
                      "full_at": 0, "year": 0, "last_ok": 0, "last_error": "", "unknown_types": [],
                      "to_ship": None}
        try:
            with open(path) as f:
                self.state.update(json.load(f))
        except FileNotFoundError:
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    LEDGER_WINDOW = 30 * 86400   # Etsy refuses ledger queries wider than 31 days

    def _ledger(self, client, shop_id: str, start: int, end: int) -> dict:
        out = {}
        lo = start
        while lo < end:
            hi = min(lo + self.LEDGER_WINDOW, end)
            for e in client.paged(f"/application/shops/{shop_id}/payment-account/ledger-entries",
                                  {"min_created": lo, "max_created": hi}):
                out[str(e["entry_id"])] = ledger_row(e)
            lo = hi
        return out

    def sync(self, client, shop_id: str):
        now = datetime.now(self.tz)
        year0 = int(now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        s = self.state
        full = (s["year"] != now.year) or (time.time() - s["full_at"] > self.FULL_EVERY)
        started = int(time.time())

        if full:
            rec = {str(r["receipt_id"]): receipt_row(r)
                   for r in client.paged(f"/application/shops/{shop_id}/receipts", {"min_created": year0})}
            # The ledger only ever gains entries, so it is read in full just once a year
            # (or on first run); after that the incremental pass keeps it current.
            fresh_year = s["year"] != now.year or not s["ledger"]
            led = (self._ledger(client, shop_id, year0, started + 60) if fresh_year
                   else self._ledger(client, shop_id, s["ledger_at"] - self.OVERLAP, started + 60))
            with self.lock:
                s["receipts"] = rec
                if fresh_year:
                    s["ledger"] = led
                else:
                    s["ledger"].update({k: v for k, v in led.items() if v["t"] >= year0})
                s["full_at"] = started
                s["year"] = now.year
        else:
            rec = {str(r["receipt_id"]): receipt_row(r)
                   for r in client.paged(f"/application/shops/{shop_id}/receipts",
                                         {"min_last_modified": s["receipts_at"] - self.OVERLAP})}
            led = self._ledger(client, shop_id, s["ledger_at"] - self.OVERLAP, started + 60)
            with self.lock:
                for k, v in rec.items():
                    if v["t"] >= year0:
                        s["receipts"][k] = v
                s["ledger"].update({k: v for k, v in led.items() if v["t"] >= year0})

        # Orders waiting to ship: one small query every sync (paid, not shipped, not canceled).
        open_orders = list(client.paged(f"/application/shops/{shop_id}/receipts",
                                        {"was_paid": "true", "was_shipped": "false", "was_canceled": "false"}))
        to_ship = to_ship_summary(open_orders, now)

        with self.lock:
            s["to_ship"] = to_ship
            s["receipts_at"] = s["ledger_at"] = started
            s["last_ok"] = started
            s["last_error"] = ""
            s["unknown_types"] = sorted({e["y"] for e in s["ledger"].values() if e["k"] == "other"
                                         and not e["y"].lower().startswith("disburse")
                                         and e["y"].lower() not in NOT_FEES})
            self.save()

    def summary(self) -> dict:
        with self.lock:
            now = datetime.now(self.tz)
            periods = summarize(self.state["receipts"], self.state["ledger"], now)
            return {"shop": os.environ.get("SHOP_NAME", ""), "currency": "USD", "tz": str(self.tz),
                    "asOf": datetime.fromtimestamp(self.state["last_ok"], self.tz).isoformat() if self.state["last_ok"] else None,
                    "stale": bool(self.state["last_error"]),
                    "error": self.state["last_error"] or None,
                    "today": periods["today"], "month": periods["month"], "year": periods["year"],
                    "toShip": self.state.get("to_ship")}


# ------------------------------------------------------------------ app

class App:
    def __init__(self):
        self.keystring = os.environ["ETSY_KEYSTRING"]
        self.secret = os.environ.get("ETSY_SHARED_SECRET", "")
        self.shop_id = os.environ["ETSY_SHOP_ID"]
        self.service_key = os.environ["SERVICE_KEY"]
        self.public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
        self.tz = ZoneInfo(os.environ.get("TZ_NAME", "America/New_York"))
        data = os.environ.get("DATA_DIR", "/data")
        os.makedirs(data, exist_ok=True)
        self.client = EtsyClient(self.keystring, self.secret, os.path.join(data, "etsy_tokens.json"))
        self.store = Store(os.path.join(data, "state.json"), self.tz)
        self.sync_seconds = max(60, int(os.environ.get("SYNC_SECONDS", "300")))
        self.pending = {}   # oauth state -> (verifier, created)
        self.kick = threading.Event()

    @property
    def redirect_uri(self):
        return f"{self.public_url}/etsy/callback"

    def sync_loop(self):
        while True:
            if self.client.connected:
                try:
                    self.store.sync(self.client, self.shop_id)
                except Exception as e:   # keep serving the last good numbers
                    traceback.print_exc()
                    with self.store.lock:
                        self.store.state["last_error"] = str(e)[:200]
            self.kick.wait(self.sync_seconds)
            self.kick.clear()

    def key_ok(self, given: str) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.service_key.encode())


APP: App | None = None

PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Etsy Dial service</title><style>body{{font-family:system-ui,sans-serif;background:#0b1424;color:#f4ebdd;max-width:520px;margin:40px auto;padding:0 20px}}
h1{{color:#f2a93b;font-size:22px}}input{{width:100%;padding:10px;border-radius:8px;border:1px solid #2a3a58;background:#131f35;color:#f4ebdd;box-sizing:border-box}}
button{{margin-top:12px;background:#f2a93b;color:#0b1424;border:0;border-radius:8px;padding:10px 16px;font-weight:600}}.m{{color:#8a93a6}}</style></head>
<body><h1>Etsy Dial service</h1>{body}
<p class=m style='margin-top:40px;font-size:12px'>The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.</p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "EtsyDial/1.0"

    def log_message(self, fmt, *args):   # no query strings (keys) in logs
        print("%s %s %s" % (self.command, self.path.split("?")[0], args[1] if len(args) > 1 else ""))

    def send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def page(self, code: int, body: str):
        self.send(code, PAGE.format(body=body), "text/html")

    def bearer(self) -> str:
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.startswith("Bearer ") else ""

    def do_GET(self):
        a = APP
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/health":
            s = a.store.state
            return self.send(200, {"connected": a.client.connected, "lastSync": s["last_ok"] or None,
                                   "error": s["last_error"] or None})
        if u.path == "/summary":
            if not a.key_ok(self.bearer()):
                return self.send(401, {"error": "Missing or wrong key."})
            if not a.client.connected:
                return self.send(503, {"error": "Etsy is not connected yet."})
            if not a.store.state["last_ok"]:
                return self.send(503, {"error": a.store.state["last_error"] or "First Etsy sync still running."})
            return self.send(200, a.store.summary())
        if u.path == "/etsy/connect":
            return self.page(200, "<p class=m>Connect the Etsy shop (read-only: orders and payment-account fees).</p>"
                                  "<form method=post action=/etsy/connect><label>Service key</label>"
                                  "<input name=key type=password autocomplete=off><button>Continue to Etsy</button></form>")
        if u.path == "/etsy/callback":
            st = (q.get("state") or [""])[0]
            code = (q.get("code") or [""])[0]
            pend = a.pending.pop(st, None)
            if not pend or not code or time.time() - pend[1] > 600:
                return self.page(400, "<p>That link expired or did not match. Start again at <a style='color:#f2a93b' href=/etsy/connect>/etsy/connect</a>.</p>")
            try:
                a.client.exchange_code(code, pend[0], a.redirect_uri)
            except Exception as e:
                return self.page(502, f"<p>Etsy refused the code: {html.escape(str(e))}</p>")
            a.kick.set()
            return self.page(200, "<p>Connected. The first sync of this year's orders takes a minute or two; the dials pick it up automatically.</p>")
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        a = APP
        if self.path != "/etsy/connect":
            return self.send(404, {"error": "not found"})
        n = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if not a.key_ok((form.get("key") or [""])[0]):
            return self.page(401, "<p>Wrong key. <a style='color:#f2a93b' href=/etsy/connect>Try again</a>.</p>")
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        st = secrets.token_urlsafe(24)
        a.pending = {k: v for k, v in a.pending.items() if time.time() - v[1] < 600}
        a.pending[st] = (verifier, time.time())
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": a.keystring, "redirect_uri": a.redirect_uri,
            "scope": SCOPES, "state": st, "code_challenge": challenge, "code_challenge_method": "S256"})
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    global APP
    APP = App()
    threading.Thread(target=APP.sync_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"Etsy Dial service listening on :{port}  (redirect URI {APP.redirect_uri})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
