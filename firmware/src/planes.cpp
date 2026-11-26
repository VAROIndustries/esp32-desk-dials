#include "planes.h"
#include <NetworkClientSecure.h>
#include <HTTPClient.h>
#include <esp_heap_caps.h>
#include <math.h>
#include <string.h>
#include <algorithm>
#include "certs.h"
#include "config.h"

// Same structure as sky.cpp. Everything below except the network I/O and the parse of the spare
// buffer is guarded by s_mx.
//
// planes_end vs. a running planes_fetch / planes_route_tick (other core): planes_end never frees
// while one is running. The fetch sets s_busy under the lock when it starts; if planes_end comes
// in meanwhile it drops the current reply at once (readers see nullptr) and sets s_endPending;
// the fetch frees the buffers itself on its way out. s_gen counts planes_end calls so a fetch
// that started before one never swaps its result (or a route) in after it.
//
// The request budget and its clock survive planes_end: adsb.lol counts per IP per UTC day,
// whether or not the Sky source was deselected in between.

namespace {

constexpr uint32_t READ_MS       = 15000;
constexpr uint32_t MIN_GAP_MS    = 30000;     // never two plane requests closer than this
constexpr uint32_t ROUTE_GAP_MS  = 20000;     // adsbdb: at most one lookup per 20 s
constexpr size_t   ROUTE_MAX     = 8 * 1024;
constexpr int      ROUTE_SLOTS   = 24;
constexpr int32_t  RETRY_DEFAULT = 900;       // 429 without a usable Retry-After

struct RouteEntry {
    char     cs[9];        // cleaned callsign, "" = free slot
    char     text[20];     // "SJU - MCO"; "" = no route
    uint32_t used;         // millis() of the last use, for LRU
};

planes::Reply    *s_buf[2] = { nullptr, nullptr };
int               s_cur = -1;                 // index of the current reply, -1 = none
uint32_t          s_ms = 0;
char             *s_body = nullptr;           // MAX_REPLY + 1 bytes, PSRAM (route replies use it too)
RouteEntry       *s_routes = nullptr;         // ROUTE_SLOTS entries, PSRAM
char              s_want[9] = "";             // cleaned callsign the UI asked for, "" = none
uint32_t          s_routeMs = 0;              // millis() of the last route request
bool              s_routeAsked = false;       // a route request was ever made
SemaphoreHandle_t s_mx = nullptr;             // created once, never deleted
bool              s_busy = false;
bool              s_endPending = false;
uint32_t          s_gen = 0;

// Budget and its clock (UTC ms = s_utcBase + millis() - s_utcAt once a reply gave us `now`).
planes::Budget    s_budget;
bool              s_haveUtc = false;
int64_t           s_utcBase = 0;
uint32_t          s_utcAt = 0;
uint16_t          s_unbilled = 0;             // requests made before the clock was known
bool              s_attempted = false;        // a plane request was ever made
uint32_t          s_lastReqMs = 0;            // millis() of the last plane request
uint32_t          s_blockAt = 0, s_blockMs = 0;   // a 429 that came before the clock was known

// Caller holds the lock (or there is no mutex yet, so nothing was allocated).
void freeAll() {
    for (int i = 0; i < 2; ++i) { heap_caps_free(s_buf[i]); s_buf[i] = nullptr; }
    heap_caps_free(s_body); s_body = nullptr;
    heap_caps_free(s_routes); s_routes = nullptr;
    s_endPending = false;
}
void resetState() {
    s_cur = -1; s_ms = 0;
    s_want[0] = 0;
    s_routeAsked = false;
    if (s_routes) memset(s_routes, 0, sizeof(RouteEntry) * ROUTE_SLOTS);
}

// A-Z0-9 upper case, 3..8 characters; anything else is dropped. false = not a callsign.
bool cleanCallsign(const char *in, char out[9]) {
    out[0] = 0;
    if (!in) return false;
    size_t o = 0;
    for (const char *p = in; *p; ++p) {
        char c = *p;
        if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
        if (!((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9'))) continue;
        if (o >= 8) { out[0] = 0; return false; }
        out[o++] = c;
    }
    out[o] = 0;
    return o >= 3;
}

// Caller holds the lock.
RouteEntry *findRoute(const char *cs) {
    if (!s_routes || !cs[0]) return nullptr;
    for (int i = 0; i < ROUTE_SLOTS; ++i)
        if (s_routes[i].cs[0] && !strcmp(s_routes[i].cs, cs)) return &s_routes[i];
    return nullptr;
}
// Caller holds the lock. text "" = no route.
void putRoute(const char *cs, const char *text) {
    if (!s_routes || !cs[0]) return;
    RouteEntry *e = findRoute(cs);
    if (!e) {
        e = &s_routes[0];
        for (int i = 0; i < ROUTE_SLOTS; ++i) {
            if (!s_routes[i].cs[0]) { e = &s_routes[i]; break; }
            if ((int32_t)(s_routes[i].used - e->used) < 0) e = &s_routes[i];   // least recently used
        }
        strlcpy(e->cs, cs, sizeof(e->cs));
    }
    strlcpy(e->text, text, sizeof(e->text));
    e->used = millis();
}

// Caller holds the lock. Current UTC ms, false = not known yet.
bool utcNow(int64_t &now) {
    if (!s_haveUtc) return false;
    now = s_utcBase + (int64_t)(uint32_t)(millis() - s_utcAt);
    return true;
}

// Caller holds the lock. Seconds until the next plane request may go (0 = now, -1 = done today).
int32_t waitLocked() {
    const uint32_t m = millis();
    int64_t remMs = 0;
    if (s_attempted) {
        const uint32_t since = m - s_lastReqMs;
        if (since < MIN_GAP_MS) remMs = MIN_GAP_MS - since;
    }
    int64_t utc;
    if (!utcNow(utc)) {                            // before the first reply: allowed now (but spaced)
        if (s_blockMs) {
            const uint32_t since = m - s_blockAt;
            if (since < s_blockMs) remMs = std::max<int64_t>(remMs, s_blockMs - since);
            else s_blockMs = 0;
        }
        return (int32_t)((remMs + 999) / 1000);
    }
    if (s_budget.blockedUntilMs > utc)
        return (int32_t)std::max<int64_t>((s_budget.blockedUntilMs - utc + 999) / 1000, (remMs + 999) / 1000);
    const int32_t w = s_budget.wait_s(utc);        // spacing that spreads what is left over the day
    if (w < 0) return -1;
    if (s_attempted) {
        const int64_t spacing = (int64_t)w * 1000 - (int64_t)(uint32_t)(m - s_lastReqMs);
        if (spacing > remMs) remMs = spacing;
    }
    return (int32_t)((remMs + 999) / 1000);
}

// Caller holds the lock. One plane request is about to be made.
void spendLocked() {
    s_attempted = true;
    s_lastReqMs = millis();
    int64_t utc;
    if (utcNow(utc)) s_budget.spent(utc);
    else if (s_unbilled < 0xFFFF) ++s_unbilled;
}

// Caller holds the lock. A reply gave the server's time.
void setClockLocked(int64_t nowMs, uint32_t atMs) {
    if (nowMs <= 0) return;
    s_haveUtc = true;
    s_utcBase = nowMs;
    s_utcAt = atMs;
    int64_t utc = nowMs;
    while (s_unbilled) { s_budget.spent(utc); --s_unbilled; }
    if (s_blockMs) {                               // carry a 429 from before the clock over
        const uint32_t since = atMs - s_blockAt;
        if (since < s_blockMs) s_budget.limited(utc, (int32_t)((s_blockMs - since + 999) / 1000));
        s_blockMs = 0;
    }
}

// Ends a fetch: clears s_busy, does a deferred planes_end.
PlaneErr finish(PlaneErr e) {
    planes_lock();
    s_busy = false;
    if (s_endPending) freeAll();
    planes_unlock();
    return e;
}

// Reads the body into s_body: at most `cap` + 1 bytes, stops at the deadline or at
// Content-Length. Returns the bytes read; more than `cap` means "too big".
size_t readBody(HTTPClient &http, size_t cap) {
    NetworkClient *st = http.getStreamPtr();
    if (!st) return 0;
    const int len = http.getSize();                // -1 = not given (read to close)
    if (len > (int)cap) return cap + 1;
    size_t got = 0;
    const uint32_t start = millis();
    while (got <= cap && (len < 0 || got < (size_t)len) && millis() - start < READ_MS) {
        const int avail = st->available();
        if (avail <= 0) {
            if (!st->connected()) break;           // closed and drained
            delay(5);
            continue;
        }
        const size_t room = cap + 1 - got;         // never past s_body[cap]
        const size_t n = st->readBytes(s_body + got, (size_t)avail < room ? (size_t)avail : room);
        if (n == 0) break;
        got += n;
    }
    return got;
}

void setupHttp(HTTPClient &http) {
    http.setConnectTimeout(10000);
    http.setTimeout(READ_MS);
    http.setReuse(false);
    http.useHTTP10(false);                         // adsb.lol answers HTTP/1.0 with 426; replies carry Content-Length (readBody stops there)
    http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);   // a key goes to its own host only
}

}  // namespace

void planes_lock()   { if (s_mx) xSemaphoreTake(s_mx, portMAX_DELAY); }
void planes_unlock() { if (s_mx) xSemaphoreGive(s_mx); }

bool planes_begin() {
    if (!s_mx) s_mx = xSemaphoreCreateMutex();
    if (!s_mx) return false;
    planes_lock();
    s_endPending = false;                          // a quick re-select keeps the buffers
    for (int i = 0; i < 2; ++i)
        if (!s_buf[i]) s_buf[i] = (planes::Reply *)heap_caps_calloc(1, sizeof(planes::Reply), MALLOC_CAP_SPIRAM);
    if (!s_body) s_body = (char *)heap_caps_malloc(planes::MAX_REPLY + 1, MALLOC_CAP_SPIRAM);
    if (!s_routes) s_routes = (RouteEntry *)heap_caps_calloc(ROUTE_SLOTS, sizeof(RouteEntry), MALLOC_CAP_SPIRAM);
    const bool ok = s_buf[0] && s_buf[1] && s_body && s_routes;
    planes_unlock();
    if (!ok) planes_end();
    return ok;
}

void planes_end() {
    if (!s_mx) return;                             // planes_begin never ran: nothing to free
    planes_lock();
    ++s_gen;
    resetState();
    if (s_busy) s_endPending = true;               // the running fetch frees on its way out
    else        freeAll();
    planes_unlock();
}

const planes::Reply *planes_reply() { return s_cur >= 0 && s_buf[s_cur] ? s_buf[s_cur] : nullptr; }
uint32_t planes_reply_ms() { return s_ms; }

int32_t planes_wait_s() {
    if (!s_mx) return 0;
    planes_lock();
    const int32_t w = waitLocked();
    planes_unlock();
    return w;
}

PlaneErr planes_fetch(const PlaneRequest &r) {
    if (!s_mx) return PL_NO_MEMORY;
    planes_lock();
    const bool ready = s_body && s_buf[0] && s_buf[1] && !s_endPending && !s_busy;
    s_budget.perDay = r.perDay ? r.perDay : 1;
    const int32_t wait = ready ? waitLocked() : 0;
    const uint32_t gen = s_gen;
    const int spare = s_cur == 0 ? 1 : 0;          // readers only ever touch s_buf[s_cur]
    if (ready && wait == 0) s_busy = true;
    planes_unlock();
    if (!ready) return PL_NO_MEMORY;
    if (wait != 0) return PL_WAIT;                 // no network call

    String base = r.url;
    while (base.endsWith("/")) base.remove(base.length() - 1);
    if (!base.startsWith("https://")) return finish(PL_UNREACHABLE);   // never send the key in clear
    int radius = isnan(r.radiusNm) ? 1 : (int)ceilf(r.radiusNm);
    if (radius < 1) radius = 1;
    if (radius > 250) radius = 250;
    char tail[64];
    snprintf(tail, sizeof(tail), "/%.4f/%.4f/%d", r.lat, r.lon, radius);
    const String url = base + tail;

    NetworkClientSecure client;
    client.setCACert(ROOT_CAS);
    HTTPClient http;
    setupHttp(http);
    if (!http.begin(client, url)) return finish(PL_UNREACHABLE);
    static const char *kHeaders[] = { "Retry-After" };
    http.collectHeaders(kHeaders, 1);
    http.addHeader("Accept", "application/json");
    http.setUserAgent(FW_NAME "/" FW_VERSION);
    if (r.key.length()) http.addHeader("x-api-key", r.key);    // plane key only; never the sky key

    planes_lock();
    spendLocked();                                 // counts against the day whatever the outcome
    planes_unlock();
    const int code = http.GET();
    if (code <= 0) { http.end(); return finish(PL_UNREACHABLE); }

    if (code == 429) {
        int32_t retry = (int32_t)http.header("Retry-After").toInt();   // an HTTP-date gives 0
        http.end();
        if (retry <= 0) retry = RETRY_DEFAULT;
        if (retry > 86400) retry = 86400;
        planes_lock();
        int64_t utc;
        if (utcNow(utc)) s_budget.limited(utc, retry);
        else { s_blockAt = millis(); s_blockMs = (uint32_t)retry * 1000u; }
        planes_unlock();
        return finish(PL_LIMITED);
    }
    if (code != 200) { http.end(); return finish(PL_HTTP); }

    const size_t got = readBody(http, planes::MAX_REPLY);
    http.end();
    if (got > planes::MAX_REPLY) return finish(PL_BAD_REPLY);
    s_body[got] = 0;
    const uint32_t arrived = millis();

    // Parse without the lock: a reader may hold s_buf[s_cur], never s_buf[spare].
    if (!planes::parse(s_body, got, r.lat, r.lon, *s_buf[spare])) return finish(PL_BAD_REPLY);
    planes_lock();
    setClockLocked(s_buf[spare]->now_ms, arrived); // the budget clock, whatever planes_end did
    if (gen == s_gen) {
        s_cur = spare;
        s_ms = arrived;
    }
    planes_unlock();
    return finish(PL_OK);
}

void planes_route_want(const char *callsign) {
    if (!s_mx) return;
    char cs[9];
    const bool ok = cleanCallsign(callsign, cs);
    planes_lock();
    if (ok) strlcpy(s_want, cs, sizeof(s_want));
    else    s_want[0] = 0;                         // not a callsign: "none", no request
    planes_unlock();
}

bool planes_route_get(const char *callsign, char *out, size_t n) {
    if (n) out[0] = 0;
    if (!s_mx || !out || !n) return false;
    char cs[9];
    if (!cleanCallsign(callsign, cs)) return false;   // not a callsign: "none"
    planes_lock();
    RouteEntry *e = findRoute(cs);
    bool have = false;
    if (e) {
        e->used = millis();
        if (e->text[0]) { strlcpy(out, e->text, n); have = true; }
    }
    planes_unlock();
    return have;
}

void planes_route_tick() {
    if (!s_mx) return;
    planes_lock();
    char cs[9];
    strlcpy(cs, s_want, sizeof(cs));
    const bool ready = s_body && s_routes && !s_endPending && !s_busy;
    const bool due = !s_routeAsked || (uint32_t)(millis() - s_routeMs) >= ROUTE_GAP_MS;
    const bool go = ready && cs[0] && !findRoute(cs) && due;
    const uint32_t gen = s_gen;
    if (go) { s_busy = true; s_routeAsked = true; s_routeMs = millis(); }
    planes_unlock();
    if (!go) return;

    // Fixed host, no key of any kind.
    const String url = String("https://api.adsbdb.com/v0/callsign/") + cs;
    NetworkClientSecure client;
    client.setCACert(ROOT_CAS);
    HTTPClient http;
    setupHttp(http);
    if (!http.begin(client, url)) { finish(PL_UNREACHABLE); return; }
    http.addHeader("Accept", "application/json");
    http.setUserAgent(FW_NAME "/" FW_VERSION);
    const int code = http.GET();

    bool cache = false;
    char text[20] = "";
    if (code == 200) {
        const size_t got = readBody(http, ROUTE_MAX);
        if (got <= ROUTE_MAX) {
            s_body[got] = 0;
            if (!planes::route_text(s_body, got, text, sizeof(text))) text[0] = 0;
            cache = true;                          // a reply we can't read is "none" too
        }
    } else if (code == 404) {
        cache = true;                              // unknown callsign: "none"
    }
    http.end();                                    // 5xx, other codes, network: try again in 20 s

    planes_lock();
    if (cache && gen == s_gen) putRoute(cs, text);
    planes_unlock();
    finish(cache ? PL_OK : (code <= 0 ? PL_UNREACHABLE : PL_HTTP));
}
