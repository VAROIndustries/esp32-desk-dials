#include "sky.h"
#include <NetworkClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_heap_caps.h>
#include "certs.h"
#include "config.h"

// Everything below except the network I/O and the parse of the spare buffer is guarded by s_mx.
//
// sky_end vs. a running sky_fetch (other core): sky_end never frees while a fetch is running.
// sky_fetch sets s_busy under the lock when it starts; if sky_end comes in meanwhile it drops
// the current reply at once (readers see nullptr) and sets s_endPending; the fetch frees the
// buffers itself on its way out. sky_end therefore never blocks the UI loop for the length of a
// fetch (up to ~40 s with the timeouts below). s_gen counts sky_end calls so a fetch that
// started before one never swaps its result in after it, even if sky_begin ran again quickly.
static sky::Reply       *s_buf[2] = { nullptr, nullptr };
static int               s_cur = -1;              // index of the current reply, -1 = none
static uint32_t          s_ms = 0;
static uint16_t          s_poll = 50;
static uint16_t          s_backoff = 0;
static char             *s_body = nullptr;        // MAX_REPLY + 1 bytes, PSRAM
static SemaphoreHandle_t s_mx = nullptr;          // created once, never deleted
static bool              s_busy = false;
static bool              s_endPending = false;
static uint32_t          s_gen = 0;

static constexpr uint16_t BUSY_POLL_S = 300;
static constexpr uint32_t READ_MS = 15000;

void sky_lock()   { if (s_mx) xSemaphoreTake(s_mx, portMAX_DELAY); }
void sky_unlock() { if (s_mx) xSemaphoreGive(s_mx); }

// Caller holds the lock (or there is no mutex yet, so nothing was allocated by sky_begin).
static void freeAll() {
    for (int i = 0; i < 2; ++i) { heap_caps_free(s_buf[i]); s_buf[i] = nullptr; }
    heap_caps_free(s_body); s_body = nullptr;
    s_endPending = false;
}
static void resetState() { s_cur = -1; s_ms = 0; s_poll = 50; s_backoff = 0; }

bool sky_begin() {
    if (!s_mx) s_mx = xSemaphoreCreateMutex();
    if (!s_mx) return false;
    sky_lock();
    s_endPending = false;                          // a quick re-select keeps the buffers
    for (int i = 0; i < 2; ++i)
        if (!s_buf[i]) s_buf[i] = (sky::Reply *)heap_caps_calloc(1, sizeof(sky::Reply), MALLOC_CAP_SPIRAM);
    if (!s_body) s_body = (char *)heap_caps_malloc(sky::MAX_REPLY + 1, MALLOC_CAP_SPIRAM);
    const bool ok = s_buf[0] && s_buf[1] && s_body;
    sky_unlock();
    if (!ok) sky_end();
    return ok;
}

void sky_end() {
    if (!s_mx) return;                             // sky_begin never ran: nothing to free
    sky_lock();
    ++s_gen;
    resetState();
    if (s_busy) s_endPending = true;               // the running fetch frees on its way out
    else        freeAll();
    sky_unlock();
}

const sky::Reply *sky_reply() { return s_cur >= 0 ? s_buf[s_cur] : nullptr; }
uint32_t sky_reply_ms() { return s_ms; }
uint16_t sky_next_poll_s() { return s_backoff ? s_backoff : s_poll; }

// Caller holds the lock.
static void fail() { s_backoff = s_backoff ? (s_backoff * 2 > 120 ? 120 : s_backoff * 2) : 30; }

// Ends the fetch: records the outcome, clears s_busy, does a deferred sky_end.
static SkyErr finish(SkyErr e, uint32_t gen, uint16_t busyPoll = 0) {
    sky_lock();
    if (gen == s_gen) {                            // no sky_end since this fetch started
        if (e == SKY_BUSY)                          { s_backoff = 0; s_poll = busyPoll; }
        else if (e != SKY_OK && e != SKY_NO_MEMORY) fail();
    }
    s_busy = false;
    if (s_endPending) freeAll();
    sky_unlock();
    return e;
}

// Reads the body into s_body: at most `cap` + 1 bytes, stops at the deadline or at
// Content-Length. Returns the bytes read; more than `cap` means "too big".
static size_t readBody(HTTPClient &http, size_t cap) {
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

SkyErr sky_fetch(const SkyRequest &r, int *httpCode) {
    if (httpCode) *httpCode = 0;
    if (!s_mx) return SKY_NO_MEMORY;
    sky_lock();
    const bool ready = s_body && s_buf[0] && s_buf[1] && !s_endPending;
    const uint32_t gen = s_gen;
    const int spare = s_cur == 0 ? 1 : 0;          // readers only ever touch s_buf[s_cur]
    if (ready) s_busy = true;
    sky_unlock();
    if (!ready) return SKY_NO_MEMORY;

    String base = r.base;
    while (base.endsWith("/")) base.remove(base.length() - 1);
    if (!base.startsWith("https://")) return finish(SKY_UNREACHABLE, gen);   // never send the key in clear
    char q[200];
    if (sky::build_query(q, sizeof(q), r.lat, r.lon, r.ids, r.nIds, r.cuts) < 0) return finish(SKY_BAD_REPLY, gen);
    const String url = base + "/v1/sky?" + q;

    NetworkClientSecure client;
    client.setCACert(ROOT_CAS);
    HTTPClient http;
    http.setConnectTimeout(10000);
    http.setTimeout(READ_MS);
    http.setReuse(false);
    http.useHTTP10(true);                          // plain Content-Length body, no chunking
    http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);   // the key goes to `base` only
    if (!http.begin(client, url)) return finish(SKY_UNREACHABLE, gen);
    http.addHeader("Accept", "application/json");
    http.setUserAgent(FW_NAME "/" FW_VERSION);
    if (r.key.length()) http.addHeader("Authorization", "Bearer " + r.key);
    const int code = http.GET();
    if (httpCode) *httpCode = code;
    if (code <= 0) { http.end(); return finish(SKY_UNREACHABLE, gen); }

    if (code == 429 || code == 503) {
        // The service says how long to wait in {"error":..,"poll":N}; an edge page may not.
        uint16_t wait = BUSY_POLL_S;
        const size_t got = readBody(http, 1024);
        http.end();
        if (got <= 1024) {
            s_body[got] = 0;
            JsonDocument doc;
            if (!deserializeJson(doc, s_body, got) && doc["poll"].is<float>()) {
                const float p = doc["poll"].as<float>();
                wait = (uint16_t)(p < 20 ? 20 : p > 600 ? 600 : p);
            }
        }
        return finish(SKY_BUSY, gen, wait);
    }
    if (code != 200) { http.end(); return finish(SKY_HTTP, gen); }

    const size_t got = readBody(http, sky::MAX_REPLY);
    http.end();
    if (got > sky::MAX_REPLY) return finish(SKY_BAD_REPLY, gen);
    s_body[got] = 0;

    // Parse without the lock: a reader may hold s_buf[s_cur], never s_buf[spare].
    if (!sky::parse(s_body, got, *s_buf[spare])) return finish(SKY_BAD_REPLY, gen);
    sky_lock();
    if (gen == s_gen) {
        s_cur = spare;
        s_ms = millis();
        s_poll = s_buf[spare]->poll_s;
        s_backoff = 0;
    }
    sky_unlock();
    return finish(SKY_OK, gen);
}
