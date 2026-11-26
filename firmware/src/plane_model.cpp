#include "plane_model.h"
#include "sky_model.h"
#include <ArduinoJson.h>
#include <ctype.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

namespace planes {

static constexpr double  NM_PER_DEG = 60.0;
static constexpr int     MAX_CAND   = 200;            // candidates before the nearest-60 cut
static constexpr int64_t DAY_MS     = 86400000LL;

float distance_nm(double la1, double lo1, double la2, double lo2) {
    const double r = M_PI / 180.0, dla = (la2 - la1) * r, dlo = (lo2 - lo1) * r;
    const double h = sin(dla / 2) * sin(dla / 2) + cos(la1 * r) * cos(la2 * r) * sin(dlo / 2) * sin(dlo / 2);
    return (float)(2 * atan2(sqrt(h), sqrt(1 - h)) * 3440.065);   // Earth radius in nm
}

static float num(JsonVariantConst v) { return v.is<float>() ? v.as<float>() : NAN; }

bool parse(const char *json, size_t len, double oLat, double oLon, Reply &out) {
    memset(&out, 0, sizeof(out));
    if (!json || len > MAX_REPLY) return false;
    JsonDocument filter;
    filter["now"] = true;
    JsonObject f = filter["ac"].add<JsonObject>();
    for (const char *k : { "hex", "flight", "t", "alt_baro", "gs", "track", "true_heading", "lat", "lon" }) f[k] = true;
    JsonDocument doc;
    if (deserializeJson(doc, json, len, DeserializationOption::Filter(filter))) return false;
    if (!doc["ac"].is<JsonArrayConst>()) return false;
    out.now_ms = doc["now"] | (int64_t)0;
    // Static: ~13 KB in .bss, reused on every parse (one parser at a time).
    static Aircraft all[MAX_CAND];
    int n = 0;
    for (JsonObjectConst a : doc["ac"].as<JsonArrayConst>()) {
        out.total++;
        if (n >= MAX_CAND || !a["lat"].is<double>() || !a["lon"].is<double>()) continue;
        Aircraft &d = all[n];
        memset(&d, 0, sizeof(d));
        sky::clean_text(d.hex, sizeof(d.hex), a["hex"] | "");
        for (char *p = d.hex; *p; ++p) *p = (char)tolower((unsigned char)*p);
        sky::clean_text(d.flight, sizeof(d.flight), a["flight"] | "");
        sky::clean_text(d.type, sizeof(d.type), a["t"] | "");
        JsonVariantConst alt = a["alt_baro"];
        if (alt.is<const char *>()) d.alt_ft = ALT_GROUND;              // "ground"
        else if (alt.is<float>() && isfinite(alt.as<float>())) d.alt_ft = (int32_t)lroundf(alt.as<float>());
        else d.alt_ft = ALT_UNKNOWN;
        d.gs_kt = num(a["gs"]);
        d.track_deg = num(a["track"]);
        if (isnan(d.track_deg)) d.track_deg = num(a["true_heading"]);
        if (!isfinite(d.gs_kt) || d.gs_kt < 0) d.gs_kt = NAN;
        if (!isfinite(d.track_deg)) d.track_deg = NAN;
        d.lat = a["lat"].as<double>(); d.lon = a["lon"].as<double>();
        if (!isfinite(d.lat) || !isfinite(d.lon) || fabs(d.lat) > 90 || fabs(d.lon) > 180) continue;
        d.dist_nm = distance_nm(oLat, oLon, d.lat, d.lon);
        ++n;
    }
    // Insertion sort: stable (ties keep reply order) and needs no heap.
    for (int i = 1; i < n; ++i) {
        const Aircraft t = all[i];
        int j = i - 1;
        while (j >= 0 && all[j].dist_nm > t.dist_nm) { all[j + 1] = all[j]; --j; }
        all[j + 1] = t;
    }
    out.n = (uint8_t)(n < MAX_AC ? n : MAX_AC);
    memcpy(out.ac, all, sizeof(Aircraft) * out.n);
    return true;
}

void dead_reckon(const Aircraft &a, float dt_s, double &lat, double &lon) {
    lat = a.lat; lon = a.lon;
    if (isnan(a.gs_kt) || isnan(a.track_deg)) return;
    const double d = a.gs_kt * dt_s / 3600.0, t = a.track_deg * M_PI / 180.0;
    lat = a.lat + d * cos(t) / NM_PER_DEG;
    const double c = cos(a.lat * M_PI / 180.0);
    if (fabs(c) > 1e-6) lon = a.lon + d * sin(t) / (NM_PER_DEG * c);
    if (lon > 180) lon -= 360;
    if (lon < -180) lon += 360;
}

bool project(double oLat, double oLon, double lat, double lon, float radiusNm, int radiusPx, int &x, int &y) {
    double dlo = lon - oLon;
    if (dlo > 180) dlo -= 360;
    if (dlo < -180) dlo += 360;
    const double dx = dlo * NM_PER_DEG * cos(oLat * M_PI / 180.0), dy = (lat - oLat) * NM_PER_DEG;
    if (radiusNm <= 0 || hypot(dx, dy) > radiusNm) return false;
    x = (int)lround(dx / radiusNm * radiusPx);
    y = (int)lround(-dy / radiusNm * radiusPx);
    return true;
}

// 12000 -> "12,000"
static void thousands(char *out, size_t n, long v) {
    char raw[24];
    snprintf(raw, sizeof(raw), "%ld", v < 0 ? -v : v);
    const size_t len = strlen(raw);
    size_t o = 0;
    if (v < 0 && o + 1 < n) out[o++] = '-';
    for (size_t i = 0; i < len && o + 1 < n; ++i) {
        if (i && (len - i) % 3 == 0 && o + 1 < n) out[o++] = ',';
        if (o + 1 < n) out[o++] = raw[i];
    }
    if (n) out[o < n ? o : n - 1] = 0;
}

void readout(char *l1, size_t n1, char *l2, size_t n2, const Aircraft &a, Units u) {
    const char *id = a.flight[0] ? a.flight : a.hex;
    if (a.type[0]) snprintf(l1, n1, "%s  %s", id, a.type);
    else snprintf(l1, n1, "%s", id);

    if (a.alt_ft == ALT_GROUND) { snprintf(l2, n2, "on the ground"); return; }
    const char *altUnit = u == U_KM_M ? "m" : "ft";
    char alt[20];
    if (a.alt_ft == ALT_UNKNOWN) snprintf(alt, sizeof(alt), "--");
    else thousands(alt, sizeof(alt), u == U_KM_M ? lround(a.alt_ft * 0.3048) : (long)a.alt_ft);
    if (isnan(a.gs_kt)) { snprintf(l2, n2, "%s %s", alt, altUnit); return; }
    const float f = u == U_MI_FT ? 1.15078f : u == U_KM_M ? 1.852f : 1.f;
    const char *spdUnit = u == U_MI_FT ? "mph" : u == U_KM_M ? "km/h" : "kt";
    snprintf(l2, n2, "%s %s  -  %ld %s", alt, altUnit, lroundf(a.gs_kt * f), spdUnit);
}

static void airport(char *out, size_t n, JsonObjectConst ap) {
    sky::clean_text(out, n, ap["iata_code"] | "");
    if (!out[0]) sky::clean_text(out, n, ap["icao_code"] | "");
}

bool route_text(const char *json, size_t len, char *out, size_t n) {
    if (n) out[0] = 0;
    if (!json || !n || len > 16 * 1024) return false;
    JsonDocument doc;
    if (deserializeJson(doc, json, len)) return false;
    JsonObjectConst fr = doc["response"]["flightroute"];
    if (fr.isNull()) return false;
    char o[8], d[8];
    airport(o, sizeof(o), fr["origin"]);
    airport(d, sizeof(d), fr["destination"]);
    if (!o[0] || !d[0]) return false;
    snprintf(out, n, "%s - %s", o, d);
    return true;
}

static int64_t day_of(int64_t ms) { return ms - ((ms % DAY_MS) + DAY_MS) % DAY_MS; }

int32_t Budget::wait_s(int64_t now) {
    const int64_t day = day_of(now);
    if (day != dayStartMs) { dayStartMs = day; used = 0; }
    if (blockedUntilMs > now) {
        const int64_t s = (blockedUntilMs - now + 999) / 1000;
        return (int32_t)(s > 30 ? s : 30);
    }
    const int left = (int)perDay - (int)used;
    if (left <= 0) return -1;
    const int64_t secLeft = (day + DAY_MS - now) / 1000;
    const int64_t w = secLeft / left;
    return (int32_t)(w > 30 ? w : 30);
}

void Budget::spent(int64_t now) {
    const int64_t day = day_of(now);
    if (day != dayStartMs) { dayStartMs = day; used = 0; }
    if (used < 0xFFFF) ++used;
}

void Budget::limited(int64_t now, int32_t retryS) {
    blockedUntilMs = now + (int64_t)(retryS > 0 ? retryS : 900) * 1000;
}

}  // namespace planes
