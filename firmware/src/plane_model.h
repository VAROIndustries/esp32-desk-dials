#pragma once
// Planes on the Sky source: everything about an adsb.lol / airplanes.live "v2" reply and the
// daily request budget that needs no Arduino and no LVGL (pio test -e native).
#include <stddef.h>
#include <stdint.h>

namespace planes {

constexpr int     MAX_AC      = 60;
constexpr size_t  MAX_REPLY   = 96 * 1024;
constexpr int32_t ALT_GROUND  = INT32_MIN;        // alt_baro "ground"
constexpr int32_t ALT_UNKNOWN = INT32_MIN + 1;    // alt_baro missing

enum Units : uint8_t { U_NM_FT = 0, U_MI_FT, U_KM_M };

struct Aircraft {
    char     hex[7];          // ICAO 24-bit, lower case
    char     flight[9];       // callsign, cleaned, may be ""
    char     type[5];         // "A320", may be ""
    int32_t  alt_ft;          // ALT_GROUND = on the ground, ALT_UNKNOWN = not reported
    float    gs_kt;           // ground speed, NAN = unknown
    float    track_deg;       // NAN = unknown
    double   lat, lon;
    float    dist_nm;         // from the observer (computed here, not trusted from the server)
};
struct Reply { int64_t now_ms; uint8_t n; int32_t total; Aircraft ac[MAX_AC]; };

// Parse an adsb.lol / airplanes.live v2 reply. Keeps at most MAX_AC, nearest first; `total` = planes in the reply.
bool parse(const char *json, size_t len, double obsLat, double obsLon, Reply &out);
// Where a plane is `dt_s` seconds after its report (straight line at its track and speed; unchanged if unknown).
void dead_reckon(const Aircraft &a, float dt_s, double &lat, double &lon);
// Map view: centre = observer, rim = radiusNm, north up before rotation. false = outside the rim.
bool project(double obsLat, double obsLon, double lat, double lon, float radiusNm, int radiusPx, int &x, int &y);
float distance_nm(double lat1, double lon1, double lat2, double lon2);
// "JBU1234  A320" / "12,000 ft  -  450 kt"  (units per setting; ground -> "on the ground")
void readout(char *l1, size_t n1, char *l2, size_t n2, const Aircraft &a, Units u);
// Route text from an adsbdb callsign reply: "SJU - MCO" (IATA, else ICAO). false = no route.
bool route_text(const char *json, size_t len, char *out, size_t n);

// Daily request budget (adsb.lol resets at 00:00 UTC). Times are UTC milliseconds.
struct Budget {
    uint16_t perDay = 500;
    uint16_t used = 0;
    int64_t  dayStartMs = 0;      // 00:00 UTC of the day `used` counts
    int64_t  blockedUntilMs = 0;  // after a 429
    // Seconds to wait before the next request, given the current UTC time. Spreads what is left
    // over the rest of the day; never under 30 s; returns -1 when nothing is left today.
    int32_t wait_s(int64_t nowMs);
    void    spent(int64_t nowMs);                    // one request made
    void    limited(int64_t nowMs, int32_t retryS);  // got 429
};

}  // namespace planes
