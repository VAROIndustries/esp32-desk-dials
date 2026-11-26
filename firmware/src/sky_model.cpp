#include "sky_model.h"
#include <ArduinoJson.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

namespace sky {

static const char *const KEYS[SCR_COUNT]   = { "starlink", "nasa", "spy" };
static const char *const TITLES[SCR_COUNT] = { "STARLINK", "NASA", "SPY SATS" };
static const int         CUTS[SCR_COUNT]   = { 60, 20, 20 };

const char *screen_key(ScreenId id)   { return id < SCR_COUNT ? KEYS[id] : ""; }
const char *screen_title(ScreenId id) { return id < SCR_COUNT ? TITLES[id] : ""; }
int default_cut(ScreenId id)          { return id < SCR_COUNT ? CUTS[id] : 20; }

void clean_text(char *dst, size_t n, const char *src) {
    if (!n) return;
    size_t o = 0;
    for (const char *p = src ? src : ""; *p && o + 1 < n; ++p) {
        const char c = *p;
        const bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                        c == ' ' || c == '-' || c == '.' || c == '(' || c == ')' || c == '/' || c == '+' || c == '_';
        if (ok && !(c == ' ' && (o == 0 || dst[o - 1] == ' '))) dst[o++] = c;
    }
    while (o && dst[o - 1] == ' ') --o;
    dst[o] = 0;
}

static float num(JsonVariantConst v, float dflt) {
    if (!v.is<float>()) return dflt;
    const float f = v.as<float>();
    return isfinite(f) ? f : dflt;
}

static void parse_screen(JsonObjectConst js, Screen &s) {
    if (js["locked"] | false)      { s.state = ST_LOCKED; return; }
    if (js["unavailable"] | false) { s.state = ST_UNAVAILABLE; return; }
    s.state = ST_OK;
    s.count = (int32_t)num(js["count"], 0);
    if (s.count < 0) s.count = 0;
    s.age_h = num(js["age_h"], 0);
    s.hobby = js["hobby"].is<bool>() ? (js["hobby"].as<bool>() ? 1 : 0) : -1;
    for (JsonObjectConst o : js["objects"].as<JsonArrayConst>()) {
        if (s.n >= MAX_OBJ) break;
        Obj &d = s.obj[s.n];
        clean_text(d.name, sizeof(d.name), o["n"] | "");
        clean_text(d.tag, sizeof(d.tag), o["tag"] | "");
        d.old = o["old"] | false;
        d.npts = 0;
        for (JsonArrayConst p : o["trk"].as<JsonArrayConst>()) {
            if (d.npts >= TRK_PTS) break;
            const float az = num(p[0], NAN), el = num(p[1], NAN);
            if (isnan(az) || isnan(el) || az < 0 || az > 360 || el < -90 || el > 90) break;
            d.az[d.npts] = az; d.el[d.npts] = el; ++d.npts;
        }
        if (d.npts) ++s.n;
    }
    JsonObjectConst nx = js["next"];
    if (!nx.isNull()) {
        s.next.have = true;
        clean_text(s.next.name, sizeof(s.next.name), nx["n"] | "");
        s.next.in_s = (int32_t)num(nx["in_s"], 0);
        clean_text(s.next.from, sizeof(s.next.from), nx["from"] | "");
        s.next.max_el = (int16_t)num(nx["max_el"], 0);
    }
}

bool parse(const char *json, size_t len, Reply &out) {
    memset(&out, 0, sizeof(out));
    out.poll_s = 50;
    if (!json || len > MAX_REPLY) return false;
    JsonDocument doc;
    if (deserializeJson(doc, json, len)) return false;
    JsonObjectConst screens = doc["screens"];
    if (screens.isNull()) return false;
    const float poll = num(doc["poll"], 50);
    out.poll_s = (uint16_t)(poll < 20 ? 20 : poll > 600 ? 600 : poll);
    const float lag = num(doc["lag_ms"], 0);
    out.lag_ms = (uint32_t)(lag < 0 ? 0 : lag > 60000 ? 60000 : lag);
    clean_text(out.notice, sizeof(out.notice), doc["notice"] | "");
    for (int i = 0; i < SCR_COUNT; ++i) {
        JsonObjectConst js = screens[KEYS[i]];
        if (!js.isNull()) parse_screen(js, out.scr[i]);
    }
    return true;
}

bool position(const Obj &o, float t_s, float &az, float &el) {
    if (o.npts == 0 || t_s < 0) return false;
    const float span = (float)(o.npts - 1) * TRK_STEP_S;
    if (t_s > span) return false;
    int i = (int)(t_s / TRK_STEP_S);
    if (i >= o.npts - 1) { az = o.az[o.npts - 1]; el = o.el[o.npts - 1]; return true; }
    const float f = (t_s - i * TRK_STEP_S) / TRK_STEP_S;
    float d = o.az[i + 1] - o.az[i];
    if (d > 180) d -= 360;
    if (d < -180) d += 360;
    az = fmodf(o.az[i] + f * d + 360.f, 360.f);
    el = o.el[i] + f * (o.el[i + 1] - o.el[i]);
    return true;
}

bool project(float az, float el, int cut, int radius, int &x, int &y) {
    if (el < (float)cut) return false;
    const float r = radius * (90.f - el) / (90.f - (float)cut);
    const float a = az * (float)M_PI / 180.f;
    x = (int)lroundf(r * sinf(a));
    y = (int)lroundf(-r * cosf(a));
    return true;
}

int build_query(char *out, size_t n, float lat, float lon, const ScreenId *ids, int nIds, const int *cuts) {
    int w = snprintf(out, n, "lat=%.1f&lon=%.1f&screens=", lat, lon);
    for (int i = 0; i < nIds && w >= 0 && (size_t)w < n; ++i)
        w += snprintf(out + w, n - w, "%s%s", i ? "," : "", screen_key(ids[i]));
    bool first = true;
    for (int i = 0; i < nIds && w >= 0 && (size_t)w < n; ++i) {
        if (cuts[ids[i]] == default_cut(ids[i])) continue;
        w += snprintf(out + w, n - w, "%s%s:%d", first ? "&cut=" : ",", screen_key(ids[i]), cuts[ids[i]]);
        first = false;
    }
    return (w < 0 || (size_t)w >= n) ? -1 : w;
}

void next_text(char *out, size_t n, const Next &nx) {
    const int32_t m = (nx.in_s + 30) / 60;
    char when[24];
    if (m < 1)        snprintf(when, sizeof(when), "now");
    else if (m <= 90) snprintf(when, sizeof(when), "in %ld min", (long)m);
    else              snprintf(when, sizeof(when), "in %ld h %ld min", (long)(m / 60), (long)(m % 60));
    snprintf(out, n, "%s %s, from %s, peaks %d deg up", nx.name, when, nx.from[0] ? nx.from : "?", (int)nx.max_el);
}

void rotate(int &x, int &y, int northOffsetDeg) {
    if (northOffsetDeg % 360 == 0) return;
    // The dial's top faces `off` degrees east of north, so everything turns by -off.
    const float a = -northOffsetDeg * (float)M_PI / 180.f, c = cosf(a), s = sinf(a);
    const float nx = x * c - y * s, ny = x * s + y * c;
    x = (int)lroundf(nx); y = (int)lroundf(ny);
}

}  // namespace sky
