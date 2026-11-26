#pragma once
// Sky source data model: everything about a sky-service reply that needs no Arduino and no
// LVGL, so it can be unit-tested on the host (pio test -e native).
#include <stddef.h>
#include <stdint.h>

namespace sky {

constexpr int    MAX_OBJ    = 60;          // per screen, whatever the server sends
constexpr int    TRK_PTS    = 13;          // 60 s of track at 5 s steps
constexpr int    TRK_STEP_S = 5;
constexpr int    NAME_LEN   = 24;          // 23 characters + NUL
constexpr size_t MAX_REPLY  = 64 * 1024;
constexpr float  OLD_DATA_H = 168.f;       // element sets older than this: "data is old"

// Order = the dial's order after Planes. New screens go at the end (saved settings index by it).
enum ScreenId : uint8_t { SCR_STARLINK = 0, SCR_NASA, SCR_SPY, SCR_BRIGHT, SCR_GNSS, SCR_NEW, SCR_PLANETS, SCR_COUNT };
constexpr int  GNSS_TAGS = 4;              // by_tag order: GPS, GAL, GLO, BDS
extern const char *const GNSS_TAG[GNSS_TAGS];
constexpr int  MAX_TAG_COUNT = 9999;
constexpr int  MAX_DAYS = 36500;
enum State : uint8_t { ST_NONE = 0, ST_OK, ST_LOCKED, ST_UNAVAILABLE };

struct Obj {
    char    name[NAME_LEN];
    char    tag[8];                         // "", "HOBBY", "MIL", "GPS"..., "MOON", "PLANET"
    float   az[TRK_PTS], el[TRK_PTS];
    uint8_t npts;
    bool    old;                            // orbit measured > 14 days ago: position approximate
    int16_t d;                              // new: whole days since launch, -1 = not known
};
struct Next {
    bool    have;
    char    name[NAME_LEN];
    int32_t in_s;
    char    from[4];                        // "NW"
    int16_t max_el;
};
struct Screen {
    State   state;
    int32_t count;                          // true number overhead (may exceed n)
    float   age_h;
    int8_t  hobby;                          // spy only: 1 loaded, 0 missing, -1 not said
    int8_t  dark;                           // bright only: 1 observer dark, 0 not, -1 not said
    bool    have_by_tag;                    // gnss only
    int16_t by_tag[GNSS_TAGS];              // gnss: count above the cutoff per GNSS_TAG, 0..9999
    uint8_t n;
    Obj     obj[MAX_OBJ];
    Next    next;
};
struct Reply {
    uint16_t poll_s;
    uint32_t lag_ms;
    char     notice[121];
    Screen   scr[SCR_COUNT];
};

const char *screen_key(ScreenId id);
const char *screen_title(ScreenId id);
int  default_cut(ScreenId id);

// Keep A-Z a-z 0-9 space - . ( ) / + _ ; drop everything else; trim; cut to n-1.
void clean_text(char *dst, size_t n, const char *src);
// false = not a usable reply (too big, not JSON, no "screens"). `out` is reset first.
bool parse(const char *json, size_t len, Reply &out);
// t_s = seconds since the track's first point. false outside the track.
bool position(const Obj &o, float t_s, float &az, float &el);
// Sky view: centre = zenith, rim (radius px) = cut elevation, north up, east right.
// x, y are offsets from the centre in px. false = below the cutoff.
bool project(float az, float el, int cut, int radius, int &x, int &y);
// "lat=..&lon=..&screens=a,b[&cut=a:45]". cuts[] is indexed by ScreenId. -1 = did not fit.
int  build_query(char *out, size_t n, float lat, float lon, const ScreenId *ids, int nIds, const int *cuts);
// "ISS in 12 min, from NW, peaks 64 deg up"
void next_text(char *out, size_t n, const Next &nx);
// "Venus rises in 3 h 10 min, E" (name in title case; no "peaks": rises are at the horizon)
void rise_text(char *out, size_t n, const Next &nx);
// "MOON" -> "Moon", "ISS (ZARYA)" -> "Iss (Zarya)": first letter of each word upper, rest lower.
void title_case(char *out, size_t n, const char *src);
// 8-point compass name of an azimuth in degrees: "N", "NE", ... "NW".
const char *compass8(float az);
// Index into GNSS_TAG, -1 if not one of them.
int gnss_index(const char *tag);

// Turn a scope point (offset from the centre, px) so true north sits at -northOffsetDeg on the
// dial: northOffsetDeg is the direction the top of the dial faces, clockwise from true north.
void rotate(int &x, int &y, int northOffsetDeg);

}  // namespace sky
