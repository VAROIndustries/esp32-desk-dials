// VARO Desk Dial — sales on a round desk display.
//
// Two data sources:
//   FairKeep  GET {fkBase}/device/today   (bb_live_ API key, Reports read)
//             views: Total / POS / Web / All three
//   Etsy      GET {etBase}/summary        (Etsy dial service key, see etsy-service/)
//             views: Today / Month / Year; gross by default, optionally after Etsy fees
//             (and optionally after postage labels too)
//   Sky       GET {skyBase}/v1/sky       (no key; a sky key opens the Spy screen)
//             + GET {plUrl}/<lat>/<lon>/<radius> (adsb.lol, on a daily request budget)
//             screens: Planes / Starlink / NASA / Spy, drawn as a scope with moving dots
// Setup: first boot opens the DeskDial-Setup hotspot (WiFi + key). Afterwards
// everything is on http://deskdial.local/ (or the dial's IP).
// Touch: tap = next view (unless the view is locked), long-press = settings screen
// (IP, brightness, auto-cycle, refresh, screen off). With the screen off, a tap wakes it.
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <NetworkClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Update.h>
#include <mbedtls/base64.h>
#include <esp_random.h>

#include "config.h"
#include "certs.h"
#include "display.h"
#include "battery.h"
#include "ui.h"
#include "sky.h"
#include "planes.h"
#include "scope.h"
#include <math.h>

enum Source : uint8_t { SRC_FAIRKEEP = 0, SRC_ETSY = 1, SRC_STATUS = 2, SRC_SKY = 3, SRC_COUNT };
enum FkMode : uint8_t { FK_TOTAL = 0, FK_POS, FK_WEB, FK_ALL, FK_COUNT };
enum EtMode : uint8_t { ET_TODAY = 0, ET_MONTH, ET_YEAR, ET_SHIP, ET_COUNT };
enum StMode : uint8_t { ST_ALL = 0, ST_SERVERS, ST_COUNT };
// Sky source screens on the dial: Planes first, then the three sky-service screens
// (dial screen i >= 1 is sky::ScreenId i - 1).
enum SkyScr : uint8_t { DS_PLANES = 0, DS_STARLINK, DS_NASA, DS_SPY, DS_COUNT };
static inline sky::ScreenId skyIdOf(int scr) { return (sky::ScreenId)(scr - 1); }
#define DEFAULT_PLANE_URL "https://api.adsb.lol/v2/point"
static const int ET_PERIODS = 3;   // today / month / year come as money periods; ET_SHIP is the to-ship count

// ------------------------------------------------------------------ settings
struct Settings {
    uint8_t  src = SRC_FAIRKEEP;
    String   fkKey, fkBase = DEFAULT_API_BASE;
    uint8_t  fkMode = FK_TOTAL;
    String   etKey, etBase = DEFAULT_ETSY_BASE;
    uint8_t  etMode = ET_TODAY;
    String   stKey, stBase = DEFAULT_STATUS_BASE;
    uint8_t  stMode = ST_ALL;
    bool     etNet = false;      // show after Etsy fees instead of gross
    bool     etLabels = false;   // ...and after postage labels too
    bool     tapCycles = true;   // false = view locked to the one picked on the settings page
    uint16_t pollS = DEFAULT_POLL_S;
    bool     cents = false;
    uint8_t  bright = DEFAULT_BRIGHTNESS;
    String   pin;                // web settings page PIN (6 digits, made up on first boot)
    bool     cycleOn = false;    // step through the views by itself
    uint8_t  cycleS = 15;        // ...every this many seconds
    float    skyLat = NAN, skyLon = NAN;   // NAN = not set
    String   skyKey, skyBase = DEFAULT_SKY_BASE;
    uint8_t  skyOn = 0b011;                // bit per sky::ScreenId; default Starlink + NASA
    uint8_t  skyCut[sky::SCR_COUNT] = { 60, 20, 20 };
    uint8_t  skyScr = DS_PLANES;           // dial screen, SkyScr
    bool     plOn = true;                  // Planes screen on
    uint8_t  plRadius = 25;                // nm, 5..100
    uint8_t  plUnits = planes::U_NM_FT;
    String   plUrl = DEFAULT_PLANE_URL, plKey;
    uint32_t plDay = 500;                  // requests/day, 50..100000
    uint16_t north = 0;                    // which way the top of the dial faces, 0..359
    uint8_t  theme = TH_NIGHT;             // scope colour scheme
    bool     plColor = true;               // colour planes by airline

    const String &key() const  { return src == SRC_ETSY ? etKey : src == SRC_STATUS ? stKey : src == SRC_SKY ? skyKey : fkKey; }
    const String &base() const { return src == SRC_ETSY ? etBase : src == SRC_STATUS ? stBase : src == SRC_SKY ? skyBase : fkBase; }
    bool skyHasLoc() const { return !isnan(skyLat) && !isnan(skyLon); }
    // dial screen (SkyScr) enabled = switched on, and spy only with a key
    bool skyEnabled(int scr) const {
        if (scr == DS_PLANES) return plOn;
        const int id = scr - 1;
        return (skyOn >> id & 1) && (id != sky::SCR_SPY || skyKey.length());
    }
};

// Next enabled dial screen after `from` (wraps); `from` itself if no other is enabled.
static int skyNextEnabled(const Settings &s, int from) {
    for (int k = 1; k < DS_COUNT; ++k) {
        const int id = (from + k) % DS_COUNT;
        if (s.skyEnabled(id)) return id;
    }
    return from;
}
// Keep skyScr on an enabled screen (it stays put if none is enabled).
static void skyFixMode(Settings &s) {
    if (!s.skyEnabled(s.skyScr)) s.skyScr = skyNextEnabled(s, s.skyScr);
}
// Display units for the plane radius: nm, statute miles, km.
static float unitPerNm(uint8_t u) { return u == planes::U_MI_FT ? 1.15078f : u == planes::U_KM_M ? 1.852f : 1.f; }
static const char *unitName(uint8_t u) { return u == planes::U_MI_FT ? "mi" : u == planes::U_KM_M ? "km" : "nm"; }
static Settings    g_set;
static Preferences g_prefs;

// NVS keys "apikey", "apibase", "mode" are the FairKeep ones from v1.0 — kept so a
// dial updated over the air keeps its setup.
static void loadSettings() {
    g_prefs.begin("fkdial", true);   // namespace kept from the FairKeep Dial days so updates keep settings
    g_set.src      = g_prefs.getUChar("src", SRC_FAIRKEEP);
    if (g_set.src >= SRC_COUNT) g_set.src = SRC_FAIRKEEP;
    g_set.fkKey    = g_prefs.getString("apikey", "");
    g_set.fkBase   = g_prefs.getString("apibase", DEFAULT_API_BASE);
    g_set.fkMode   = g_prefs.getUChar("mode", FK_TOTAL) % FK_COUNT;
    g_set.etKey    = g_prefs.getString("etkey", "");
    g_set.etBase   = g_prefs.getString("etbase", DEFAULT_ETSY_BASE);
    g_set.etMode   = g_prefs.getUChar("etmode", ET_TODAY) % ET_COUNT;
    g_set.stKey    = g_prefs.getString("stkey", "");
    g_set.stBase   = g_prefs.getString("stbase", DEFAULT_STATUS_BASE);
    g_set.stMode   = g_prefs.getUChar("stmode", ST_ALL) % ST_COUNT;
    g_set.etNet    = g_prefs.getBool("etnet", false);
    g_set.etLabels = g_prefs.getBool("etlbl", false);
    g_set.tapCycles = g_prefs.getBool("tap", true);
    g_set.pollS    = constrain(g_prefs.getUShort("poll", DEFAULT_POLL_S), MIN_POLL_S, MAX_POLL_S);
    g_set.cents    = g_prefs.getBool("cents", false);
    g_set.bright   = constrain(g_prefs.getUChar("bright", DEFAULT_BRIGHTNESS), 10, 255);
    g_set.pin      = g_prefs.getString("pin", "");
    g_set.cycleOn  = g_prefs.getBool("cycle", false);
    g_set.cycleS   = constrain(g_prefs.getUChar("cycles", 15), 5, 120);
    g_set.skyLat   = g_prefs.getFloat("skylat", NAN);
    g_set.skyLon   = g_prefs.getFloat("skylon", NAN);
    if (!isfinite(g_set.skyLat) || !isfinite(g_set.skyLon)) g_set.skyLat = g_set.skyLon = NAN;
    g_set.skyKey   = g_prefs.getString("skykey", "");
    g_set.skyBase  = g_prefs.getString("skybase", DEFAULT_SKY_BASE);
    g_set.skyOn    = g_prefs.getUChar("skyon", 0b011) & 0b111;
    for (int i = 0; i < sky::SCR_COUNT; ++i)
        g_set.skyCut[i] = constrain(g_prefs.getUChar((String("skycut") + i).c_str(), sky::default_cut((sky::ScreenId)i)), 0, 85);
    // 1.4.0-beta1 kept the sky-service screen in "skymode"; the dial list now has Planes first.
    if (g_prefs.isKey("skyscr")) g_set.skyScr = g_prefs.getUChar("skyscr", DS_PLANES) % DS_COUNT;
    else                         g_set.skyScr = g_prefs.getUChar("skymode", sky::SCR_STARLINK) % sky::SCR_COUNT + 1;
    g_set.plOn     = g_prefs.getBool("plon", true);
    g_set.plRadius = constrain(g_prefs.getUChar("pradius", 25), 5, 100);
    g_set.plUnits  = g_prefs.getUChar("punits", planes::U_NM_FT) % 3;
    g_set.plUrl    = g_prefs.getString("purl", DEFAULT_PLANE_URL);
    g_set.plKey    = g_prefs.getString("pkey", "");
    g_set.plDay    = constrain(g_prefs.getUInt("pday", 500), 50u, 100000u);
    g_set.north    = g_prefs.getUShort("north", 0) % 360;
    g_set.theme    = g_prefs.getUChar("theme", TH_NIGHT) % TH_COUNT;
    g_set.plColor  = g_prefs.getBool("pcolor", true);
    skyFixMode(g_set);
    g_prefs.end();
}

static void saveSettings() {
    g_prefs.begin("fkdial", false);
    g_prefs.putUChar("src", g_set.src);
    g_prefs.putString("apikey", g_set.fkKey);
    g_prefs.putString("apibase", g_set.fkBase);
    g_prefs.putUChar("mode", g_set.fkMode);
    g_prefs.putString("etkey", g_set.etKey);
    g_prefs.putString("etbase", g_set.etBase);
    g_prefs.putUChar("etmode", g_set.etMode);
    g_prefs.putString("stkey", g_set.stKey);
    g_prefs.putString("stbase", g_set.stBase);
    g_prefs.putUChar("stmode", g_set.stMode);
    g_prefs.putBool("etnet", g_set.etNet);
    g_prefs.putBool("etlbl", g_set.etLabels);
    g_prefs.putBool("tap", g_set.tapCycles);
    g_prefs.putUShort("poll", g_set.pollS);
    g_prefs.putBool("cents", g_set.cents);
    g_prefs.putUChar("bright", g_set.bright);
    g_prefs.putString("pin", g_set.pin);
    g_prefs.putBool("cycle", g_set.cycleOn);
    g_prefs.putUChar("cycles", g_set.cycleS);
    g_prefs.putFloat("skylat", g_set.skyLat);
    g_prefs.putFloat("skylon", g_set.skyLon);
    g_prefs.putString("skykey", g_set.skyKey);
    g_prefs.putString("skybase", g_set.skyBase);
    g_prefs.putUChar("skyon", g_set.skyOn);
    for (int i = 0; i < sky::SCR_COUNT; ++i) g_prefs.putUChar((String("skycut") + i).c_str(), g_set.skyCut[i]);
    g_prefs.putUChar("skyscr", g_set.skyScr);
    if (g_set.skyScr != DS_PLANES) g_prefs.putUChar("skymode", skyIdOf(g_set.skyScr));   // for a downgrade
    g_prefs.putBool("plon", g_set.plOn);
    g_prefs.putUChar("pradius", g_set.plRadius);
    g_prefs.putUChar("punits", g_set.plUnits);
    g_prefs.putString("purl", g_set.plUrl);
    g_prefs.putString("pkey", g_set.plKey);
    g_prefs.putUInt("pday", g_set.plDay);
    g_prefs.putUShort("north", g_set.north);
    g_prefs.putUChar("theme", g_set.theme);
    g_prefs.putBool("pcolor", g_set.plColor);
    g_prefs.end();
}

// ------------------------------------------------------------------ fetched data
struct FkData {
    bool  have = false;
    float total = 0, pos = 0, web = 0;
    int   totalOrders = 0, posOrders = 0, webOrders = 0, shows = 0;
    char  webNote[96] = "";
};
struct EtPeriod {
    char  label[16] = "";
    int   orders = 0;
    float gross = 0, fees = 0, labels = 0;
};
struct EtShip {
    bool  have = false;           // false = service too old to send it
    int   orders = 0, overdue = 0, dueToday = 0;
    char  next[16] = "";          // "Mon Nov 23"
};
struct EtData {
    bool     have = false;
    EtPeriod p[ET_PERIODS];
    EtShip   ship;
};
#define ST_MAX_SERVERS 5
#define ST_MAX_PROBLEMS 3
struct StServer  { char name[20] = ""; char state = 'U'; int cpu = -1; };
struct StProblem { char name[28] = ""; char why[32] = ""; char state = 'D'; };
struct StData {
    bool      have = false;
    char      overall = 'U';
    int       up = 0, warn = 0, down = 0, stopped = 0, total = 0, nProblems = 0;
    int       nServers = 0, serversUp = 0;
    StServer  srv[ST_MAX_SERVERS];
    StProblem prob[ST_MAX_PROBLEMS];
    char      dots[161] = "";
    int       age = 0;
};

// Written by the fetch task (core 0), read by the UI loop (core 1).
static SemaphoreHandle_t g_lock;
static FkData            g_fk;
static EtData            g_et;
static StData            g_st;
static bool              g_dirty = true;
static bool              g_screenOff = false;    // dimmed to black from the settings screen; a tap wakes it
static uint32_t          g_lastCycle = 0;
static String            g_err;            // last error, "" = last fetch ok
static String            g_warn;           // non-fatal note from the server (e.g. Etsy sync delayed)
static uint32_t          g_lastOkMs = 0;
static uint8_t           g_dataSrc = 0xFF; // which source g_fk/g_et/g_lastOkMs belong to
static volatile bool     g_fetchNow = true;
static volatile uint8_t  g_plErr = PL_OK;  // last plane fetch outcome (PL_WAIT never stored)
static volatile bool     g_plReady = false; // planes_begin succeeded (Sky selected)

static String keyPrefix(const String &k) {
    return k.length() > 12 ? k.substring(0, 12) + "..." : (k.length() ? String("(set)") : String(""));
}

static void setError(const String &e) {
    xSemaphoreTake(g_lock, portMAX_DELAY);
    g_err = e;
    g_dirty = true;
    xSemaphoreGive(g_lock);
}

// Forget what we showed for the other source.
static void resetData() {
    xSemaphoreTake(g_lock, portMAX_DELAY);
    g_fk = FkData();
    g_et = EtData();
    g_st = StData();
    g_err = "";
    g_warn = "";
    g_lastOkMs = 0;
    g_dataSrc = g_set.src;
    g_dirty = true;
    xSemaphoreGive(g_lock);
}

static void doFetch() {
    String key, base;
    uint8_t src;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    src = g_set.src;
    key = g_set.key();
    base = g_set.base();
    xSemaphoreGive(g_lock);
    if (key.isEmpty() || WiFi.status() != WL_CONNECTED) return;

    while (base.endsWith("/")) base.remove(base.length() - 1);
    const String url = base + (src == SRC_ETSY ? "/summary" : src == SRC_STATUS ? "/status" : "/device/today");
    const char *who = src == SRC_ETSY ? "the Etsy dial service" : src == SRC_STATUS ? "the status service" : "FairKeep";

    NetworkClientSecure client;
    client.setCACert(ROOT_CAS);
    HTTPClient http;
    http.setTimeout(15000);
    http.setConnectTimeout(10000);
    if (!http.begin(client, url)) { setError("Bad API URL"); return; }
    http.addHeader("Authorization", "Bearer " + key);
    http.addHeader("Accept", "application/json");
    http.setUserAgent(FW_NAME "/" FW_VERSION);

    const int code = http.GET();
    String body = code > 0 ? http.getString() : String();
    http.end();

    if (code <= 0) {
        Serial.printf("[fetch] %s -> %d (%s)\n", url.c_str(), code, HTTPClient::errorToString(code).c_str());
        setError(String("Can't reach ") + who);
        return;
    }

    JsonDocument doc;
    const DeserializationError jerr = deserializeJson(doc, body);
    if (code != 200) {
        String msg = (!jerr && doc["error"].is<const char *>()) ? String((const char *)doc["error"]) : String();
        Serial.printf("[fetch] HTTP %d %s\n", code, msg.c_str());
        if (code == 401)      setError("Key rejected - update it on the setup page");
        else if (code == 404) setError(src == SRC_FAIRKEEP ? "FairKeep doesn't have the dial endpoint yet"
                                                           : String(who) + " not found - check the URL");
        else if (code == 429) setError("Too many requests - slow the refresh down");
        else if (msg.length()) setError(msg);
        else                  setError(String(who) + " error " + String(code));
        return;
    }
    if (jerr) { setError(String("Unreadable reply from ") + who); return; }

    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (src != g_set.src) { xSemaphoreGive(g_lock); return; }   // source changed mid-fetch
    if (src == SRC_STATUS) {
        StData t;
        t.have = true;
        const char *ov = doc["overall"] | "up";
        t.overall = (char)toupper((unsigned char)ov[0]);
        t.up = doc["counts"]["up"] | 0;       t.warn = doc["counts"]["warn"] | 0;
        t.down = doc["counts"]["down"] | 0;   t.stopped = doc["counts"]["stopped"] | 0;
        t.total = doc["counts"]["total"] | 0;
        for (JsonObject o : doc["servers"].as<JsonArray>()) {
            const char *st = o["state"] | "up";
            if (st[0] == 'u') t.serversUp++;
            if (t.nServers >= ST_MAX_SERVERS) { t.nServers++; continue; }
            StServer &v = t.srv[t.nServers++];
            strlcpy(v.name, o["name"] | "?", sizeof(v.name));
            v.state = (char)toupper((unsigned char)st[0]);
            v.cpu = o["cpu"].isNull() ? -1 : (int)lroundf(o["cpu"].as<float>());
        }
        for (JsonObject o : doc["problems"].as<JsonArray>()) {
            if (t.nProblems >= ST_MAX_PROBLEMS) { t.nProblems++; continue; }
            StProblem &v = t.prob[t.nProblems++];
            strlcpy(v.name, o["name"] | "?", sizeof(v.name));
            strlcpy(v.why, o["why"] | "", sizeof(v.why));
            const char *st = o["state"] | "down";
            v.state = (char)toupper((unsigned char)st[0]);
        }
        t.nProblems = doc["problems"].as<JsonArray>().size();
        strlcpy(t.dots, doc["dots"] | "", sizeof(t.dots));
        t.age = doc["ageSeconds"] | 0;
        g_st = t;
        g_warn = t.age > 600 ? String("status service hasn't checked in ") + String(t.age / 60) + "m" : String();
    } else if (src == SRC_ETSY) {
        static const char *names[ET_PERIODS] = { "today", "month", "year" };
        EtData e;
        e.have = true;
        JsonObject ts = doc["toShip"];
        if (!ts.isNull()) {
            e.ship.have     = true;
            e.ship.orders   = ts["orders"] | 0;
            e.ship.overdue  = ts["overdue"] | 0;
            e.ship.dueToday = ts["dueToday"] | 0;
            strlcpy(e.ship.next, ts["nextShipBy"] | "", sizeof(e.ship.next));
        }
        for (int i = 0; i < ET_PERIODS; ++i) {
            JsonObject o = doc[names[i]];
            strlcpy(e.p[i].label, o["label"] | "", sizeof(e.p[i].label));
            e.p[i].orders = o["orders"] | 0;
            e.p[i].gross  = o["gross"] | 0.0f;
            e.p[i].fees   = o["fees"] | 0.0f;
            e.p[i].labels = o["labels"] | 0.0f;
        }
        g_et = e;
        g_warn = doc["stale"] | false ? String("Etsy sync delayed - showing last numbers") : String();
    } else {
        FkData s;
        s.have        = true;
        s.total       = doc["total"]["revenue"] | 0.0f;
        s.totalOrders = doc["total"]["orders"] | 0;
        s.pos         = doc["pos"]["revenue"] | 0.0f;
        s.posOrders   = doc["pos"]["orders"] | 0;
        s.shows       = doc["pos"]["shows"] | 0;
        s.web         = doc["web"]["revenue"] | 0.0f;
        s.webOrders   = doc["web"]["orders"] | 0;
        strlcpy(s.webNote, doc["web"]["note"] | "", sizeof(s.webNote));
        g_fk = s;
        g_warn = "";
    }
    g_dataSrc = src;
    g_err = "";
    g_lastOkMs = millis();
    g_dirty = true;
    xSemaphoreGive(g_lock);
    Serial.printf("[fetch] ok (%s)\n", src == SRC_ETSY ? "etsy" : src == SRC_STATUS ? "status" : "fairkeep");
}

// The dial screens on show and (with auto-cycle) next. Caller holds g_lock. next = -1 if none.
static void skyShownNext(int &shown, int &next) {
    shown = g_set.skyScr;
    next = -1;
    if (g_set.cycleOn) {
        const int nx = skyNextEnabled(g_set, shown);
        if (nx != shown) next = nx;
    }
}

// Sky: fetch task only. Lock order: g_lock is never taken while holding sky_lock or planes_lock.
// Asks only for the sky-service screens on show / next; nothing when only Planes is involved.
static void doSkyFetch() {
    SkyRequest r;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (g_set.src != SRC_SKY || !g_set.skyHasLoc()) { xSemaphoreGive(g_lock); return; }   // switched away meanwhile
    r.base = g_set.skyBase; r.key = g_set.skyKey; r.lat = g_set.skyLat; r.lon = g_set.skyLon;
    for (int i = 0; i < sky::SCR_COUNT; ++i) r.cuts[i] = g_set.skyCut[i];
    r.nIds = 0;
    int shown, next;
    skyShownNext(shown, next);
    if (shown != DS_PLANES) r.ids[r.nIds++] = skyIdOf(shown);
    if (next > DS_PLANES)   r.ids[r.nIds++] = skyIdOf(next);
    xSemaphoreGive(g_lock);
    if (!r.nIds) return;
    int code = 0;
    const SkyErr e = sky_fetch(r, &code);
    String notice;
    if (e == SKY_OK) {
        sky_lock();
        const sky::Reply *rp = sky_reply();
        if (rp && rp->notice[0]) notice = rp->notice;
        sky_unlock();
    }
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (g_set.src == SRC_SKY) {
        if (e == SKY_OK) {
            g_err = ""; g_warn = notice; g_lastOkMs = millis(); g_dataSrc = SRC_SKY;
        } else if (e == SKY_BUSY)    g_err = "sky service busy - trying again later";
        else if (e == SKY_NO_MEMORY) g_err = "Not enough memory for Sky";
        else                         g_err = "sky service unreachable. Check dial.varo.industries for status.";
        g_dirty = true;
    }
    xSemaphoreGive(g_lock);
    Serial.printf("[sky] fetch %d (http %d)\n", (int)e, code);
}

// Planes: fetch task only, and only when the budget allows (planes_wait_s() == 0).
static void doPlaneFetch() {
    PlaneRequest r;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (g_set.src != SRC_SKY || !g_set.skyHasLoc()) { xSemaphoreGive(g_lock); return; }
    r.url = g_set.plUrl; r.key = g_set.plKey; r.lat = g_set.skyLat; r.lon = g_set.skyLon;
    r.radiusNm = g_set.plRadius;
    r.perDay = (uint16_t)min<uint32_t>(g_set.plDay, 65535u);   // above ~2,880/day the 30 s floor rules anyway
    xSemaphoreGive(g_lock);
    const PlaneErr e = planes_fetch(r);
    if (e == PL_WAIT) return;                       // budget says not yet: normal
    g_plErr = e;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (g_set.src == SRC_SKY) {
        if (e == PL_OK) { g_lastOkMs = millis(); g_dataSrc = SRC_SKY; }
        g_dirty = true;
    }
    xSemaphoreGive(g_lock);
    Serial.printf("[planes] fetch %d (next in %ld s)\n", (int)e, (long)planes_wait_s());
}

static void fetchTask(void *) {
    uint32_t last = 0;
    for (;;) {
        xSemaphoreTake(g_lock, portMAX_DELAY);
        const bool     isSky = g_set.src == SRC_SKY;
        const uint32_t period = isSky ? (uint32_t)sky_next_poll_s() * 1000UL : (uint32_t)g_set.pollS * 1000UL;
        const bool     hasKey = isSky ? g_set.skyHasLoc() : !g_set.key().isEmpty();
        int shown = -1, next = -1;
        if (isSky) skyShownNext(shown, next);
        const bool planesWanted = isSky && g_plReady && g_set.plOn && (shown == DS_PLANES || next == DS_PLANES);
        const bool skyWanted = !isSky || shown != DS_PLANES || next > DS_PLANES;
        const bool routes = isSky && g_set.skyKey.length() > 0;
        xSemaphoreGive(g_lock);
        const bool online = WiFi.status() == WL_CONNECTED && hasKey;
        if (skyWanted && (g_fetchNow || (last == 0) || (millis() - last >= period))) {
            if (online) {
                g_fetchNow = false;
                if (isSky) doSkyFetch();
                else       doFetch();
                last = millis();
            }
        }
        if (online && planesWanted && planes_wait_s() == 0) doPlaneFetch();
        if (online && routes) planes_route_tick();
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}

// ------------------------------------------------------------------ view building
static void ordersText(char *out, size_t n, int orders) {
    snprintf(out, n, "%d order%s", orders, orders == 1 ? "" : "s");
}

static int sev(char c) { return c == 'D' ? 3 : c == 'W' ? 2 : c == 'U' ? 1 : 0; }

static void upper(char *s) { for (; *s; ++s) *s = (char)toupper((unsigned char)*s); }

// LVGL recolour uses '#'; keep it out of names that come from elsewhere.
static void noHash(char *s) { for (; *s; ++s) if (*s == '#') *s = ' '; }

static uint8_t toneFor(char state) { return state == 'D' ? 2 : state == 'W' ? 1 : 0; }

static ViewData buildStatusView() {
    ViewData v;
    const StData &t = g_st;
    v.have = t.have;
    v.listSub = true;
    if (!t.have) { snprintf(v.header, sizeof(v.header), "SERVER STATUS"); return v; }
    char line[64];
    if (g_set.stMode == ST_SERVERS) {
        snprintf(v.header, sizeof(v.header), "SERVERS");
        snprintf(v.bigText, sizeof(v.bigText), "%d/%d", t.serversUp, t.nServers);
        char worst = 'U';
        for (int i = 0; i < t.nServers && i < ST_MAX_SERVERS; ++i) {
            const StServer &s = t.srv[i];
            if (sev(s.state) > sev(worst)) worst = s.state;
            char nm[20]; strlcpy(nm, s.name, sizeof(nm)); noHash(nm);
            const char *col = s.state == 'D' ? "#F87171 " : s.state == 'W' ? "#F2A93B " : "";
            if (s.state == 'D')    snprintf(line, sizeof(line), "%s%s  off#", col, nm);
            else if (s.cpu >= 0)   snprintf(line, sizeof(line), "%s%s  %d%%%s", col, nm, s.cpu, col[0] ? "#" : "");
            else                   snprintf(line, sizeof(line), "%s%s%s", col, nm, col[0] ? "#" : "");
            if (v.sub[0]) strlcat(v.sub, "\n", sizeof(v.sub));
            strlcat(v.sub, line, sizeof(v.sub));
        }
        if (!t.nServers) strlcpy(v.sub, "no Hetzner servers", sizeof(v.sub));
        v.tone = toneFor(worst);
        return v;
    }
    // everything: apps, services, databases, domains, servers
    const int watched = t.total - t.stopped;
    snprintf(v.bigText, sizeof(v.bigText), "%d/%d", t.up, watched);
    v.tone = t.down ? 2 : t.warn ? 1 : 0;
    strlcpy(v.ring, t.dots, sizeof(v.ring));
    if (t.down)      snprintf(v.header, sizeof(v.header), "%d DOWN", t.down);
    else if (t.warn) snprintf(v.header, sizeof(v.header), "%d WARNING%s", t.warn, t.warn == 1 ? "" : "S");
    else             snprintf(v.header, sizeof(v.header), "ALL SYSTEMS UP");
    const int listed = t.nProblems < ST_MAX_PROBLEMS ? t.nProblems : ST_MAX_PROBLEMS;
    // three lines fit; if there are more problems the third says how many
    const int rows = t.nProblems > ST_MAX_PROBLEMS ? ST_MAX_PROBLEMS - 1 : listed;
    for (int i = 0; i < rows; ++i) {
        const StProblem &p = t.prob[i];
        char nm[28], why[32];
        strlcpy(nm, p.name, sizeof(nm)); noHash(nm);
        strlcpy(why, p.why, sizeof(why)); noHash(why);
        snprintf(line, sizeof(line), "%s%.18s#  %.22s", p.state == 'D' ? "#F87171 " : "#F2A93B ", nm, why);
        if (v.sub[0]) strlcat(v.sub, "\n", sizeof(v.sub));
        strlcat(v.sub, line, sizeof(v.sub));
    }
    if (t.nProblems > ST_MAX_PROBLEMS) {
        snprintf(line, sizeof(line), "\n+%d more", t.nProblems - rows);
        strlcat(v.sub, line, sizeof(v.sub));
    }
    if (!t.nProblems) {
        if (t.stopped) snprintf(v.sub, sizeof(v.sub), "%d up  -  %d stopped", t.up, t.stopped);
        else           snprintf(v.sub, sizeof(v.sub), "%d up", t.up);
    }
    return v;
}

static ViewData buildShipView() {
    ViewData v;
    const EtShip &s = g_et.ship;
    snprintf(v.header, sizeof(v.header), "ETSY  -  TO SHIP");
    v.have = g_et.have;
    if (!s.have) {
        strlcpy(v.bigText, "--", sizeof(v.bigText));
        v.tone = 3;
        strlcpy(v.sub, "update the Etsy dial service", sizeof(v.sub));
        return v;
    }
    snprintf(v.bigText, sizeof(v.bigText), "%d", s.orders);
    if (s.overdue) {
        v.tone = 2;
        if (s.dueToday) snprintf(v.sub, sizeof(v.sub), "%d overdue  -  %d due today", s.overdue, s.dueToday);
        else            snprintf(v.sub, sizeof(v.sub), "%d overdue", s.overdue);
    } else if (s.dueToday) {
        v.tone = 1;
        snprintf(v.sub, sizeof(v.sub), "%d due today", s.dueToday);
    } else if (s.orders) {
        v.tone = 3;
        if (s.next[0]) snprintf(v.sub, sizeof(v.sub), "next ship-by %s", s.next);
        else           snprintf(v.sub, sizeof(v.sub), "order%s to ship", s.orders == 1 ? "" : "s");
    } else {
        v.tone = 0;
        strlcpy(v.sub, "all shipped", sizeof(v.sub));
    }
    return v;
}

// Build the screen from the data + settings. Caller holds g_lock.
static ViewData buildView() {
    if (g_set.src == SRC_STATUS) return buildStatusView();
    if (g_set.src == SRC_ETSY && g_set.etMode == ET_SHIP) return buildShipView();
    ViewData v;
    if (g_set.src == SRC_ETSY) {
        const EtPeriod &p = g_et.p[g_set.etMode];
        static const char *fallback[ET_PERIODS] = { "TODAY", "THIS MONTH", "THIS YEAR" };
        char lbl[24];
        // "Today" reads better than the date the service sends for the day view.
        snprintf(lbl, sizeof(lbl), "%s", g_set.etMode == ET_TODAY || !p.label[0] ? fallback[g_set.etMode] : p.label);
        upper(lbl);
        snprintf(v.header, sizeof(v.header), "ETSY  -  %s", lbl);
        v.have = g_et.have;
        float amount = p.gross;
        if (g_set.etNet) amount -= p.fees + (g_set.etLabels ? p.labels : 0);
        v.big = amount;
        char o[24];
        ordersText(o, sizeof(o), p.orders);
        if (g_set.etNet) snprintf(v.sub, sizeof(v.sub), "%s  -  %s", o, g_set.etLabels ? "after fees + labels" : "after fees");
        else             snprintf(v.sub, sizeof(v.sub), "%s", o);
        return v;
    }

    const FkData &s = g_fk;
    v.have = s.have;
    int orders = s.totalOrders;
    v.big = s.total;
    switch (g_set.fkMode) {
        case FK_POS:
            if (s.shows > 0) snprintf(v.header, sizeof(v.header), "POS  -  %d SHOW%s", s.shows, s.shows == 1 ? "" : "S");
            else             snprintf(v.header, sizeof(v.header), "POS  -  NO SHOWS TODAY");
            v.big = s.pos; orders = s.posOrders; break;
        case FK_WEB:
            snprintf(v.header, sizeof(v.header), "WEB ORDERS");
            v.big = s.web; orders = s.webOrders; break;
        default:
            snprintf(v.header, sizeof(v.header), "TODAY'S SALES");
            break;
    }
    if (!s.have) snprintf(v.header, sizeof(v.header), "TODAY");
    if (g_set.fkMode == FK_ALL) {
        v.twoCol = true;
        strlcpy(v.colHdr[0], "POS", sizeof(v.colHdr[0]));
        strlcpy(v.colHdr[1], "WEB", sizeof(v.colHdr[1]));
        v.colVal[0] = s.pos;
        v.colVal[1] = s.web;
        ordersText(v.colSub[0], sizeof(v.colSub[0]), s.posOrders);
        ordersText(v.colSub[1], sizeof(v.colSub[1]), s.webOrders);
    } else if (g_set.fkMode == FK_WEB && s.webNote[0] && s.web == 0) {
        strlcpy(v.sub, s.webNote, sizeof(v.sub));
    } else {
        ordersText(v.sub, sizeof(v.sub), orders);
    }
    return v;
}

static void renderView() {
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (g_set.src == SRC_SKY) {          // the scope draws itself (skyFrame); only the page dots here
        int nEn = 0, active = 0;
        for (int i = 0; i < DS_COUNT; ++i) {
            if (!g_set.skyEnabled(i)) continue;
            if (i == g_set.skyScr) active = nEn;
            nEn++;
        }
        const int count = !(g_set.tapCycles || g_set.cycleOn) ? 0 : nEn;
        xSemaphoreGive(g_lock);
        ui_set_dots(count, active);
        return;
    }
    ViewData v = buildView();
    const int count = !(g_set.tapCycles || g_set.cycleOn) ? 0 : g_set.src == SRC_ETSY ? (int)ET_COUNT
                    : g_set.src == SRC_STATUS ? (int)ST_COUNT : (int)FK_COUNT;
    const int active = g_set.src == SRC_ETSY ? g_set.etMode : g_set.src == SRC_STATUS ? g_set.stMode : g_set.fkMode;
    xSemaphoreGive(g_lock);
    ui_set_dots(count, active);
    ui_set_view(v);
}

// One scope frame from the current sky reply. UI loop (core 1) only. g_set is written only on
// this core (web page, taps), so reading skyScr/skyCut here needs no g_lock.
static void skyFrame() {
    static ScopeDot dots[sky::MAX_OBJ];
    static int      objIx[sky::MAX_OBJ];     // dots[k] shows s->obj[objIx[k]]
    const int id = skyIdOf(g_set.skyScr), cut = g_set.skyCut[id], north = g_set.north;
    scope_set_north(north);
    scope_set_ring_labels("", "", "");
    char hdr[48], l1[64] = "", l2[96] = "", foot[64] = "";
    snprintf(hdr, sizeof(hdr), "%s", sky::screen_title((sky::ScreenId)id));
    int n = 0;
    sky_lock();
    const sky::Reply *rp = sky_reply();
    const sky::Screen *s = rp ? &rp->scr[id] : nullptr;
    const float t = rp ? rp->lag_ms / 1000.f + (millis() - sky_reply_ms()) / 1000.f : 0;
    if (!s || s->state == sky::ST_NONE) {
        snprintf(l1, sizeof(l1), "loading...");
    } else if (s->state == sky::ST_LOCKED) {
        snprintf(l1, sizeof(l1), "needs the sky key");
    } else if (s->state == sky::ST_UNAVAILABLE) {
        snprintf(l1, sizeof(l1), "sky service unreachable.");
        snprintf(l2, sizeof(l2), "Check dial.varo.industries for status.");
    } else {
        int best = -1; float bestEl = -99; bool anyHobby = false;
        bool ranOut = s->n > 0;              // true until some object still has a position at t
        for (int i = 0; i < s->n && n < sky::MAX_OBJ; ++i) {
            const sky::Obj &o = s->obj[i];
            float az, el;
            if (!sky::position(o, t, az, el)) continue;   // past the end of its track
            ranOut = false;
            int x, y;
            if (!sky::project(az, el, cut, SCOPE_R, x, y)) continue;   // below the cutoff: hidden
            sky::rotate(x, y, north);
            ScopeDot &d = dots[n];
            d.x = x; d.y = y; d.ntail = 0; d.rgb = 0;
            for (int k = 0; k < 2; ++k) {
                float a2, e2; int x2, y2;
                if (sky::position(o, t - 6.f * (k + 1), a2, e2) && sky::project(a2, e2, cut, SCOPE_R, x2, y2)) {
                    sky::rotate(x2, y2, north);
                    d.tx[d.ntail] = x2; d.ty[d.ntail] = y2; d.ntail++;
                } else break;
            }
            const bool hobby = !strcmp(o.tag, "HOBBY");
            anyHobby |= hobby;
            d.style = (o.old || hobby) ? SD_HOLLOW : SD_FILL;
            objIx[n] = i;
            if (el > bestEl) { bestEl = el; best = n; }
            n++;
        }
        if (ranOut) {
            snprintf(l1, sizeof(l1), "sky service unreachable.");
            snprintf(l2, sizeof(l2), "Check dial.varo.industries for status.");
            n = 0;
        } else if (best < 0) {
            snprintf(l1, sizeof(l1), "nothing overhead");
            if (s->next.have) sky::next_text(l2, sizeof(l2), s->next);
            else snprintf(l2, sizeof(l2), "no pass in the next %s", id == sky::SCR_STARLINK ? "20 min" : "24 h");
        } else {
            dots[best].style |= SD_HILITE;
            const sky::Obj &o = s->obj[objIx[best]];
            const int el = (int)lroundf(bestEl);
            if (id == sky::SCR_STARLINK) {
                snprintf(l1, sizeof(l1), "%ld overhead", (long)s->count);
                snprintf(l2, sizeof(l2), "%s%s", o.name, o.old ? "  -  position approximate" : "");
            } else {
                snprintf(l1, sizeof(l1), "%s%s%s", o.name, o.tag[0] ? "  -  " : "", o.tag);
                snprintf(l2, sizeof(l2), "%d deg up%s", el, o.old ? "  -  position approximate" : "");
            }
        }
        if (id == sky::SCR_SPY && anyHobby)           snprintf(foot, sizeof(foot), "amateur observations, may be inaccurate");
        else if (id == sky::SCR_SPY && s->hobby == 0) snprintf(foot, sizeof(foot), "military list only (amateur list not loaded)");
        else if (s->age_h > sky::OLD_DATA_H)          snprintf(foot, sizeof(foot), "data is old");
    }
    sky_unlock();
    scope_set_dots(dots, n);
    scope_set_text(hdr, l1, l2, foot);
}

// One scope frame of the Planes screen (map view: centre = the dial's location, rim = radius).
// UI loop only; positions are dead-reckoned from the reply's report time to now.
static char g_routeWanted[9] = "";           // callsign last passed to planes_route_want

static void planeFrame() {
    static ScopeDot dots[planes::MAX_AC];
    const int north = g_set.north;
    const uint8_t units = g_set.plUnits;
    const float radiusNm = g_set.plRadius, perNm = unitPerNm(units);
    const char *un = unitName(units);
    const double oLat = g_set.skyLat, oLon = g_set.skyLon;
    const bool keyed = g_set.skyKey.length() > 0;
    const bool colour = g_set.plColor;
    const uint32_t gaGrey = g_set.theme == TH_MONO ? planes::GA_GREY_MONO : planes::GA_GREY;
    const int rDisp = (int)lroundf(radiusNm * perNm);
    {
        char a[12], b[12], c[16];
        snprintf(a, sizeof(a), "%d", (int)lroundf(rDisp / 3.f));
        snprintf(b, sizeof(b), "%d", (int)lroundf(rDisp * 2 / 3.f));
        snprintf(c, sizeof(c), "%d %s", rDisp, un);
        scope_set_north(north);
        scope_set_ring_labels(a, b, c);
    }
    char hdr[48], l1[64] = "", l2[96] = "", foot[64] = "";
    snprintf(hdr, sizeof(hdr), "PLANES");
    int n = 0, best = -1;
    bool haveReply = false, stale = false;
    planes::Aircraft near{};
    planes_lock();
    const planes::Reply *rp = planes_reply();
    if (rp) {
        haveReply = true;
        const float dt = (uint32_t)(millis() - planes_reply_ms()) / 1000.f;   // positions are as of the reply
        float bestD = 1e9f;
        stale = dt > 600.f;      // dead reckoning past 10 min is guesswork: show nothing rather than drift
        for (int i = 0; i < rp->n && n < planes::MAX_AC && !stale; ++i) {
            const planes::Aircraft &a = rp->ac[i];
            double la, lo; int x, y;
            planes::dead_reckon(a, dt, la, lo);
            if (!planes::project(oLat, oLon, la, lo, radiusNm, SCOPE_R, x, y)) continue;   // outside the rim
            sky::rotate(x, y, north);
            ScopeDot &d = dots[n];
            d.x = x; d.y = y; d.ntail = 0; d.rgb = 0;
            if (colour) {
                uint32_t rgb;
                d.rgb = planes::carrier(a.flight, nullptr, nullptr, &rgb) ? rgb : gaGrey;
            }
            for (int k = 0; k < 2; ++k) {
                double la2, lo2; int x2, y2;
                planes::dead_reckon(a, dt - 10.f * (k + 1), la2, lo2);
                if (!planes::project(oLat, oLon, la2, lo2, radiusNm, SCOPE_R, x2, y2)) break;
                sky::rotate(x2, y2, north);
                d.tx[d.ntail] = x2; d.ty[d.ntail] = y2; d.ntail++;
            }
            d.style = a.alt_ft == planes::ALT_GROUND ? SD_HOLLOW : SD_FILL;
            const float dist = planes::distance_nm(oLat, oLon, la, lo);
            if (dist < bestD) { bestD = dist; best = n; near = a; }
            n++;
        }
    }
    planes_unlock();

    bool routeShown = false;
    if (!haveReply) {
        snprintf(l1, sizeof(l1), "loading...");
    } else {
        snprintf(hdr, sizeof(hdr), "PLANES  -  %d within %d %s", n, rDisp, un);
        if (stale) {
            snprintf(l1, sizeof(l1), "plane positions are over 10 min old");
        } else if (best < 0) {
            snprintf(l1, sizeof(l1), "no planes within %d %s", rDisp, un);
        } else {
            dots[best].style |= SD_HILITE;
            planes::readout(l1, sizeof(l1), l2, sizeof(l2), near, (planes::Units)units);
            const char *cname = nullptr;
            if (planes::carrier(near.flight, nullptr, &cname, nullptr) && cname) {   // "JetBlue  JBU1234  A320"
                char t[sizeof(l1)];
                snprintf(t, sizeof(t), "%s  %s", cname, l1);
                strlcpy(l1, t, sizeof(l1));
            }
            if (keyed && near.flight[0]) {        // routes only on key-holding dials (planes_route_* lock themselves)
                if (strcmp(g_routeWanted, near.flight)) {
                    strlcpy(g_routeWanted, near.flight, sizeof(g_routeWanted));
                    planes_route_want(near.flight);
                }
                char rt[24];
                if (planes_route_get(near.flight, rt, sizeof(rt))) {
                    strlcat(l2, "  -  ", sizeof(l2));
                    strlcat(l2, rt, sizeof(l2));
                    routeShown = true;
                }
            }
        }
    }
    const uint8_t err = g_plErr;
    const int32_t wait = planes_wait_s();
    if (err == PL_LIMITED)                                                   snprintf(foot, sizeof(foot), "plane data limit reached - waiting");
    else if (err == PL_UNREACHABLE || err == PL_HTTP || err == PL_BAD_REPLY) snprintf(foot, sizeof(foot), "plane data unreachable");
    else if (err == PL_NO_MEMORY)                                            snprintf(foot, sizeof(foot), "not enough memory for planes");
    else if (wait < 0)                                                       snprintf(foot, sizeof(foot), "daily plane limit reached");
    else if (routeShown)                                                     snprintf(foot, sizeof(foot), "routes: adsbdb.com");
    else if (wait > 90)                                                      snprintf(foot, sizeof(foot), "refresh in %ld min", (long)((wait + 59) / 60));
    scope_set_dots(dots, n);
    scope_set_text(hdr, l1, l2, foot);
}

// Sky selected: buffers + scope; another source: free everything Sky. UI core only
// (setup and handleSave), never the fetch task.
static void skyActivate(bool on) {
    if (on) {
        if (!sky_begin()) { setError("Not enough memory for Sky"); return; }
        g_plReady = planes_begin();            // Planes alone may fail; the sky screens still work
        if (!g_plReady) g_plErr = PL_NO_MEMORY;
        ui_set_scope_mode(true);
        scope_set_theme(g_set.theme);
        scope_open(ui_screen());
        if (ui_menu_open()) ui_menu_hide();   // the scope is created above the menu
    } else {
        scope_close();
        ui_set_scope_mode(false);
        g_plReady = false;
        planes_end();
        g_routeWanted[0] = 0;
        g_plErr = PL_OK;
        sky_end();
    }
}

// "41.9349" -> 41.9349f. false unless the whole (trimmed) string is a finite number within +-lim.
static bool parseCoord(String v, float lim, float &out) {
    v.trim();
    if (!v.length()) return false;
    const char *c = v.c_str();
    char *end = nullptr;
    const float f = strtof(c, &end);
    if (end == c || *end != 0 || !isfinite(f) || f < -lim || f > lim) return false;
    out = f;
    return true;
}

// ------------------------------------------------------------------ wifi + setup portal
static WiFiManager          g_wm;
static WiFiManagerParameter g_wmKey("apikey", "FairKeep API key (bb_live_...), Etsy dial key, or status key (st_...)", "", 80);
// WiFiManager writes value='{v}' before the custom attributes, so the default value "1" is
// what a ticked box posts; an unticked one posts nothing and the value becomes "".
static WiFiManagerParameter g_wmSky("sky", "Sky dial (no key needed)", "1", 2, "type='checkbox'", WFM_LABEL_AFTER);
static WiFiManagerParameter g_wmLat("skylat", "Sky dial latitude (optional)", "", 12);
static WiFiManagerParameter g_wmLon("skylon", "Sky dial longitude (optional)", "", 12);
static bool                 g_portalSaved = false;
static bool                 g_netUp = false;

// A key from the setup hotspot: FairKeep keys start with bb_live_, status service keys
// with st_, anything else is an Etsy dial service key. The word "sky" picks a Sky dial.
static void applyKey(String k) {
    k.trim();
    if (!k.length()) return;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (k.equalsIgnoreCase("sky"))      { g_set.src = SRC_SKY; }
    else if (k.startsWith("bb_live_"))  { g_set.fkKey = k; g_set.src = SRC_FAIRKEEP; }
    else if (k.startsWith("st_"))  { g_set.stKey = k; g_set.src = SRC_STATUS; }
    else                           { g_set.etKey = k; g_set.src = SRC_ETSY; }
    xSemaphoreGive(g_lock);
}

// ------------------------------------------------------------------ config web page
static WebServer g_web(80);

// A page on another site can make your browser POST to this device; refuse anything
// whose Origin doesn't match the address the browser used to reach us.
static bool crossSite() {
    const String origin = g_web.header("Origin");
    if (!origin.length()) return false;
    String auth = origin.substring(origin.indexOf("://") + 3);
    return !auth.equalsIgnoreCase(g_web.hostHeader());
}

static String htmlEscape(const String &s) {
    String o;
    for (char c : s) {
        if (c == '<') o += "&lt;"; else if (c == '>') o += "&gt;";
        else if (c == '&') o += "&amp;"; else if (c == '"') o += "&quot;";
        else if (c == '\'') o += "&#39;"; else o += c;
    }
    return o;
}

static const char PAGE_HEAD[] PROGMEM =
    "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>VARO Desk Dial</title><style>"
    "body{font-family:system-ui,sans-serif;background:#0b1424;color:#f4ebdd;margin:0;padding:24px;max-width:560px}"
    "h1{color:#f2a93b;font-size:22px;margin:0 0 4px}h2{color:#f2a93b;font-size:16px;margin:0 0 4px}.sub{color:#8a93a6;font-size:13px;margin:0 0 20px}"
    ".card{background:#131f35;border:1px solid #1e2a40;border-radius:12px;padding:16px;margin:0 0 16px}"
    "label{display:block;font-size:13px;color:#8a93a6;margin:12px 0 4px}label.c{color:#f4ebdd;font-size:15px}"
    "input,select{width:100%;box-sizing:border-box;padding:9px;border-radius:8px;border:1px solid #2a3a58;background:#0b1424;color:#f4ebdd;font-size:15px}"
    "input[type=checkbox]{width:auto;margin-right:8px}input[type=range]{padding:0}"
    "button{background:#f2a93b;color:#0b1424;border:0;border-radius:8px;padding:10px 16px;font-weight:600;font-size:15px;margin-top:16px;cursor:pointer}"
    "button.sec{background:#1e2a40;color:#f4ebdd}.ok{color:#4ade80}.err{color:#f87171}code{color:#f2a93b}"
    "</style></head><body>";

static String keyField(const char *name, const String &cur, const char *placeholder) {
    String p = "<input name=";
    p += name;
    p += " autocomplete=off placeholder='";
    p += cur.isEmpty() ? String(placeholder) : htmlEscape("Saved: " + keyPrefix(cur) + "  (leave blank to keep)");
    p += "'>";
    return p;
}

static String options(const char *const *names, int n, int sel) {
    String p;
    for (int i = 0; i < n; ++i)
        p += "<option value=" + String(i) + (sel == i ? " selected>" : ">") + names[i] + "</option>";
    return p;
}

static String checkbox(const char *name, bool on, const char *text) {
    return String("<label class=c><input type=checkbox name=") + name + " value=1" + (on ? " checked" : "") + ">" + text + "</label>";
}

// ------------------------------------------------------------------ web PIN
// HTTP Basic auth: any username, the PIN as the password. Five wrong tries lock the
// page for 30 s so a 6-digit PIN can't be walked through quickly.
static uint8_t  g_authFails = 0;
static uint32_t g_authLockUntil = 0;

static bool pinOk() {
    if (g_set.pin.isEmpty()) return true;
    if (g_authLockUntil && (int32_t)(g_authLockUntil - millis()) > 0) return false;
    String h = g_web.header("Authorization");
    bool ok = false;
    if (h.startsWith("Basic ")) {
        h = h.substring(6); h.trim();
        unsigned char out[96]; size_t n = 0;
        if (mbedtls_base64_decode(out, sizeof(out) - 1, &n, (const unsigned char *)h.c_str(), h.length()) == 0) {
            out[n] = 0;
            const char *colon = strchr((const char *)out, ':');
            if (colon) {
                const char *given = colon + 1;
                const String &want = g_set.pin;
                const size_t gl = strlen(given), wl = want.length();
                uint8_t diff = gl != wl;                       // compare every byte, no early exit
                for (size_t i = 0; i < wl; ++i) diff |= (uint8_t)((i < gl ? given[i] : 0) ^ want[i]);
                ok = diff == 0;
            }
        }
        if (!ok && ++g_authFails >= 5) { g_authFails = 0; g_authLockUntil = millis() + 30000; }
        if (ok) g_authFails = 0;
    }
    return ok;
}

// true = carry on; false = a 401 has been sent
static bool requirePin() {
    if (pinOk()) return true;
    g_web.sendHeader("WWW-Authenticate", "Basic realm=\"VARO Desk Dial - enter the dial's PIN (any username)\"");
    g_web.send(401, "text/html", String(FPSTR(PAGE_HEAD)) +
               "<h1>VARO Desk Dial</h1><div class=card>PIN needed. Press and hold the dial to see its PIN "
               "(any username; the PIN is the password).</div></body></html>");
    return false;
}

static void handleRoot() {
    if (!requirePin()) return;
    String err, warn; uint32_t lastOk;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    err = g_err; warn = g_warn; lastOk = g_lastOkMs;
    const Settings s = g_set;
    xSemaphoreGive(g_lock);

    String status;
    if (s.src == SRC_SKY && !s.skyHasLoc())        status = "<span class=err>Set your location below</span>";
    else if (s.src != SRC_SKY && s.key().isEmpty()) status = "<span class=err>No key for this data source yet</span>";
    else if (err.length())     status = "<span class=err>" + htmlEscape(err) + "</span>";
    else if (lastOk)           status = "<span class=ok>Connected</span> &middot; updated " + String((millis() - lastOk) / 1000) + "s ago";
    else                       status = "Waiting for first update&hellip;";
    if (warn.length()) status += "<br><span class=err>" + htmlEscape(warn) + "</span>";

    static const char *srcNames[SRC_COUNT] = { "FairKeep (today's POS + web sales)", "Etsy (via an Etsy dial service)",
                                                "Server status (via a status service)", "Sky (planes and satellites overhead)" };
    static const char *fkNames[FK_COUNT] = { "Total (POS + web)", "POS only", "Web only", "All three" };
    static const char *etNames[ET_COUNT] = { "Today", "This month", "This year", "To ship" };
    static const char *stNames[ST_COUNT] = { "Everything", "Servers (CPU)" };

    String p = FPSTR(PAGE_HEAD);
    p += "<h1>VARO Desk Dial</h1><p class=sub>" FW_VERSION " &middot; " + WiFi.localIP().toString() + " &middot; " + htmlEscape(WiFi.SSID()) + "</p>";
    p += "<div class=card>" + status + "</div>";
    p += "<form method=POST action=/save>";

    p += "<div class=card><label>Show</label><select name=src>" + options(srcNames, SRC_COUNT, s.src) + "</select>";
    p += checkbox("tap", s.tapCycles, "Tap the dial to change view (untick to lock it to the view below)");
    p += checkbox("cycle", s.cycleOn, "Cycle through the views by itself");
    {
        static const uint8_t secs[] = { 5, 10, 15, 20, 30, 45, 60, 120 };
        p += "<label>Change view every</label><select name=cycles>";
        for (uint8_t v : secs)
            p += "<option value=" + String(v) + (v == s.cycleS ? " selected" : "") + ">" +
                 (v < 60 ? String(v) + " seconds" : v == 60 ? String("1 minute") : String("2 minutes")) + "</option>";
        p += "</select>";
    }
    p += "<p class=sub style='margin:6px 0 0'>On the dial: press and hold for its settings screen (IP address, brightness, auto-cycle, screen off).</p>";
    p += checkbox("cents", s.cents, "Show cents") + "</div>";

    p += "<div class=card><h2>FairKeep</h2>";
    p += "<label>API key</label>" + keyField("fkkey", s.fkKey, "bb_live_...");
    p += "<p class=sub style='margin:6px 0 0'>FairKeep &rarr; Settings &rarr; API Keys. Tick <b>Reports read</b>.</p>";
    p += "<label>View</label><select name=fkmode>" + options(fkNames, FK_COUNT, s.fkMode) + "</select>";
    p += "<label>API URL</label><input name=fkbase value='" + htmlEscape(s.fkBase) + "'></div>";

    p += "<div class=card><h2>Etsy</h2>";
    p += "<label>Dial service key</label>" + keyField("etkey", s.etKey, "the SERVICE_KEY of the Etsy dial service");
    p += "<label>View</label><select name=etmode>" + options(etNames, ET_COUNT, s.etMode) + "</select>";
    p += checkbox("etnet", s.etNet, "Show after Etsy fees (default: gross)");
    p += checkbox("etlbl", s.etLabels, "...and after postage labels too");
    p += "<p class=sub style='margin:6px 0 0'>Gross = items + shipping after discounts and refunds; sales tax Etsy collects is left out. "
         "Fees = everything Etsy charged that period: transaction, processing, ads, listing and subscription fees.</p>"
         "<p class=sub style='margin:10px 0 0;font-size:11px'>The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.</p>";
    p += "<label>Service URL</label><input name=etbase value='" + htmlEscape(s.etBase) + "'></div>";

    p += "<div class=card><h2>Server status</h2>";
    p += "<label>Status service key</label>" + keyField("stkey", s.stKey, "st_...  (the SERVICE_KEY of your status service)");
    p += "<label>View</label><select name=stmode>" + options(stNames, ST_COUNT, s.stMode) + "</select>";
    p += "<p class=sub style='margin:6px 0 0'>Everything = every Coolify app, service and database, every domain and every Hetzner server; "
         "a dot round the edge for each. Servers = each Hetzner server with its CPU.</p>";
    p += "<label>Service URL</label><input name=stbase value='" + htmlEscape(s.stBase) + "'></div>";

    {
        static const char *rows[sky::SCR_COUNT] = { "Starlink", "NASA (ISS, Hubble, Landsat...)", "Spy satellites" };
        p += "<div class=card><h2>Sky</h2>";
        p += "<label>Latitude</label><input name=skylat inputmode=decimal value='" + (s.skyHasLoc() ? String(s.skyLat, 4) : String()) + "' placeholder='41.9349'>";
        p += "<label>Longitude</label><input name=skylon inputmode=decimal value='" + (s.skyHasLoc() ? String(s.skyLon, 4) : String()) + "' placeholder='-71.3172'>";
        p += "<p class=sub style='margin:6px 0 0'>Look yours up at <a style='color:#f2a93b' href='https://www.latlong.net/' target=_blank rel=noopener>latlong.net</a>. "
             "Sent to the sky service rounded to 0.1&deg; (about 10 km).</p>";
        {
            static const char *unitNames[3] = { "nautical miles / feet", "miles / feet", "kilometres / metres" };
            const float f = unitPerNm(s.plUnits);
            p += checkbox("plon", s.plOn, "Planes");
            p += "<label>Planes within (" + String(unitName(s.plUnits)) + ", " + String((int)lroundf(5 * f)) + "&ndash;" +
                 String((int)lroundf(100 * f)) + ")</label><input type=number name=pradius min=" + String((int)lroundf(5 * f)) +
                 " max=" + String((int)lroundf(100 * f)) + " value=" + String((int)lroundf(s.plRadius * f)) + ">";
            p += "<input type=hidden name=punitsold value=" + String(s.plUnits) + ">";
            p += "<label>Units</label><select name=punits>" + options(unitNames, 3, s.plUnits) + "</select>";
            p += checkbox("pcolor", s.plColor, "Colour planes by airline");
        }
        for (int i = 0; i < sky::SCR_COUNT; ++i) {
            if (i == sky::SCR_SPY && s.skyKey.isEmpty()) continue;
            p += checkbox((String("skyon") + i).c_str(), s.skyOn >> i & 1, rows[i]);
            p += "<label>Show objects above (degrees, 0&ndash;85)</label><input type=number min=0 max=85 name=skycut" + String(i) + " value=" + String(s.skyCut[i]) + ">";
        }
        {
            const char *themes[TH_COUNT];
            for (int i = 0; i < TH_COUNT; ++i) themes[i] = SCOPE_THEMES[i].name;
            p += "<label>Scope colours</label><select name=theme>" + options(themes, TH_COUNT, s.theme) + "</select>";
        }
        p += "<label>North offset (degrees, 0&ndash;359)</label><input type=number min=0 max=359 name=north value=" + String(s.north) + ">";
        p += "<p class=sub style='margin:6px 0 0'>Which way the top of the dial faces, in degrees clockwise from north (0 = north, 90 = east). "
             "Turns the scopes; text stays upright.</p>";
        p += "<label>Sky key (optional)</label>" + keyField("skykey", s.skyKey, "only if you were given one");
        p += "<label>Sky service URL</label><input name=skybase value='" + htmlEscape(s.skyBase) + "'>";
        p += "<label>Plane data URL</label><input name=purl value='" + htmlEscape(s.plUrl) + "'>";
        p += "<label>Plane data key</label>" + keyField("pkey", s.plKey, "optional adsb.lol key");
        p += "<label>Plane requests per day (50&ndash;100000)</label><input type=number min=50 max=100000 name=pday value=" + String(s.plDay) + ">";
        p += "<p class=sub style='margin:6px 0 0'>adsb.lol allows 500 a day without a key. The dial spreads them over the day and asks only while Planes is on screen.</p></div>";
    }

    p += "<div class=card><label>Refresh every (seconds, " + String(MIN_POLL_S) + "&ndash;" + String(MAX_POLL_S) + ")</label>";
    p += "<input type=number name=poll min=" + String(MIN_POLL_S) + " max=" + String(MAX_POLL_S) + " value=" + String(s.pollS) + ">";
    p += "<label>Brightness</label><input type=range name=bright min=10 max=255 value=" + String(s.bright) + ">";
    p += "<button>Save</button></div></form>";

    p += "<form method=POST action=/pin><div class=card><b>Settings PIN</b>"
         "<p class=sub style='margin:6px 0 0'>This page asks for it. The dial shows it too: press and hold the dial.</p>"
         "<label>New PIN (4&ndash;8 digits)</label><input name=pin inputmode=numeric pattern='[0-9]{4,8}' required>"
         "<button class=sec>Change PIN</button></div></form>";
    p += "<form method=POST action=/refresh><button class=sec>Refresh now</button></form>";
    p += "<div class=card style='margin-top:16px'><b>Firmware update</b>"
         "<form method=POST action=/update enctype=multipart/form-data>"
         "<input type=file name=fw accept=.bin style='margin-top:10px'><button class=sec>Upload .bin</button></form></div>";
    p += "<form method=POST action=/wifi onsubmit=\"return confirm('Forget WiFi and reopen the setup hotspot?')\">"
         "<button class=sec>Reset WiFi</button></form>";
    p += "<p class=sub>On the dial: tap to change view, press and hold for its settings screen.</p></body></html>";
    g_web.send(200, "text/html", p);
}

static void sendBack(const char *msg) {
    String p = FPSTR(PAGE_HEAD);
    p += "<h1>VARO Desk Dial</h1><div class=card>";
    p += msg;
    p += "</div><p><a style='color:#f2a93b' href='/'>Back</a></p><script>setTimeout(()=>location='/',1500)</script></body></html>";
    g_web.send(200, "text/html", p);
}

static void handleSave() {
    if (!requirePin()) return;
    if (crossSite()) { g_web.send(403, "text/plain", "cross-site request rejected"); return; }
    xSemaphoreTake(g_lock, portMAX_DELAY);
    const uint8_t oldSrc = g_set.src;
    const String oldKey = g_set.key(), oldBase = g_set.base();
    String k = g_web.arg("fkkey"); k.trim();
    if (k.length()) g_set.fkKey = k;
    k = g_web.arg("etkey"); k.trim();
    if (k.length()) g_set.etKey = k;
    k = g_web.arg("stkey"); k.trim();
    if (k.length()) g_set.stKey = k;
    const bool  spyRowShown = g_set.skyKey.length() > 0;   // the page only had the Spy row with a key
    const float oldLat = g_set.skyLat, oldLon = g_set.skyLon;
    const uint8_t oldSkyOn = g_set.skyOn;
    uint8_t oldCut[sky::SCR_COUNT];
    memcpy(oldCut, g_set.skyCut, sizeof(oldCut));
    k = g_web.arg("skykey"); k.trim();
    if (k.length()) g_set.skyKey = k;
    k = g_web.arg("pkey"); k.trim();
    if (k.length()) g_set.plKey = k;
    bool locBad = false;
    if (g_web.hasArg("skylat") || g_web.hasArg("skylon")) {
        String la = g_web.arg("skylat"), lo = g_web.arg("skylon");
        la.trim(); lo.trim();
        float fla, flo;
        if (!la.length() && !lo.length())                                     { g_set.skyLat = g_set.skyLon = NAN; }
        else if (parseCoord(la, 90, fla) && parseCoord(lo, 180, flo))         { g_set.skyLat = fla; g_set.skyLon = flo; }
        else                                                                  locBad = true;   // keep the old pair
    }
    if (g_web.hasArg("skycut0")) {                         // the Sky card was on the page
        for (int i = 0; i < sky::SCR_COUNT; ++i) {
            if (i == sky::SCR_SPY && !spyRowShown) continue;
            const String nm = String("skyon") + i;
            if (g_web.hasArg(nm)) g_set.skyOn |= 1 << i; else g_set.skyOn &= ~(1 << i);
        }
        for (int i = 0; i < sky::SCR_COUNT; ++i) {
            const String nm = String("skycut") + i;
            if (g_web.hasArg(nm)) g_set.skyCut[i] = constrain(g_web.arg(nm).toInt(), 0, 85);
        }
        g_set.plOn = g_web.hasArg("plon");
        g_set.plColor = g_web.hasArg("pcolor");
    }
    if (g_web.hasArg("pradius")) {                         // typed in the units the page showed
        const uint8_t shownUnits = g_web.hasArg("punitsold") ? constrain(g_web.arg("punitsold").toInt(), 0L, 2L) : g_set.plUnits;
        const float v = g_web.arg("pradius").toFloat() / unitPerNm(shownUnits);
        g_set.plRadius = constrain((int)lroundf(v), 5, 100);
    }
    if (g_web.hasArg("punits")) g_set.plUnits = constrain(g_web.arg("punits").toInt(), 0L, 2L);
    if (g_web.hasArg("pday"))   g_set.plDay = constrain(g_web.arg("pday").toInt(), 50L, 100000L);
    if (g_web.hasArg("theme"))  g_set.theme = constrain(g_web.arg("theme").toInt(), 0L, (long)TH_COUNT - 1);
    if (g_web.hasArg("north"))  g_set.north = constrain(g_web.arg("north").toInt(), 0L, 359L);
    if (g_web.hasArg("src"))    { const int v = g_web.arg("src").toInt(); g_set.src = v >= 0 && v < SRC_COUNT ? v : SRC_FAIRKEEP; }
    if (g_web.hasArg("stmode")) g_set.stMode = g_web.arg("stmode").toInt() % ST_COUNT;
    if (g_web.hasArg("fkmode")) g_set.fkMode = g_web.arg("fkmode").toInt() % FK_COUNT;
    if (g_web.hasArg("etmode")) g_set.etMode = g_web.arg("etmode").toInt() % ET_COUNT;
    if (g_web.hasArg("poll"))   g_set.pollS = constrain(g_web.arg("poll").toInt(), MIN_POLL_S, MAX_POLL_S);
    if (g_web.hasArg("bright")) g_set.bright = constrain(g_web.arg("bright").toInt(), 10, 255);
    g_set.cents     = g_web.hasArg("cents");
    g_set.tapCycles = g_web.hasArg("tap");
    g_set.cycleOn   = g_web.hasArg("cycle");
    if (g_web.hasArg("cycles")) g_set.cycleS = constrain(g_web.arg("cycles").toInt(), 5, 120);
    g_set.etNet     = g_web.hasArg("etnet");
    g_set.etLabels  = g_web.hasArg("etlbl");
    String b = g_web.arg("fkbase"); b.trim();
    if (b.startsWith("https://")) g_set.fkBase = b;
    b = g_web.arg("etbase"); b.trim();
    if (b.startsWith("https://")) g_set.etBase = b;
    b = g_web.arg("stbase"); b.trim();
    if (b.startsWith("https://")) g_set.stBase = b;
    b = g_web.arg("skybase"); b.trim();
    if (b.startsWith("https://")) g_set.skyBase = b;
    b = g_web.arg("purl"); b.trim();
    if (b.startsWith("https://")) g_set.plUrl = b;
    skyFixMode(g_set);
    const bool sameLoc = (isnan(oldLat) && isnan(g_set.skyLat) && isnan(oldLon) && isnan(g_set.skyLon)) ||
                         (oldLat == g_set.skyLat && oldLon == g_set.skyLon);
    const bool skyChanged = g_set.src == SRC_SKY &&
                            (!sameLoc || g_set.skyOn != oldSkyOn || memcmp(oldCut, g_set.skyCut, sizeof(oldCut)) != 0);
    const bool changed = g_set.src != oldSrc || g_set.key() != oldKey || g_set.base() != oldBase || skyChanged;
    xSemaphoreGive(g_lock);
    saveSettings();
    if (changed) resetData();
    if (changed && (oldSrc == SRC_SKY) != (g_set.src == SRC_SKY)) skyActivate(g_set.src == SRC_SKY);
    ui_set_cents(g_set.cents);
    scope_set_theme(g_set.theme);              // repaints the scope if it is open
    if (!g_screenOff) display::setBrightness(g_set.bright);
    g_lastCycle = millis();
    renderView();
    g_fetchNow = true;
    sendBack(locBad ? "Saved, but the location was not valid." : "Saved.");
}

static void handlePin() {
    if (!requirePin()) return;
    if (crossSite()) { g_web.send(403, "text/plain", "cross-site request rejected"); return; }
    String n = g_web.arg("pin"); n.trim();
    bool digits = n.length() >= 4 && n.length() <= 8;
    for (char c : n) if (c < '0' || c > '9') digits = false;
    if (!digits) { sendBack("PIN not changed: use 4 to 8 digits."); return; }
    xSemaphoreTake(g_lock, portMAX_DELAY);
    g_set.pin = n;
    xSemaphoreGive(g_lock);
    saveSettings();
    sendBack("PIN changed. Your browser will ask for the new one.");
}

static void handleRefresh() {
    if (!requirePin()) return;
    if (crossSite()) { g_web.send(403, "text/plain", "cross-site request rejected"); return; }
    g_fetchNow = true;
    sendBack("Refreshing&hellip;");
}

static void handleWifiReset() {
    if (!requirePin()) return;
    if (crossSite()) { g_web.send(403, "text/plain", "cross-site request rejected"); return; }
    sendBack("WiFi forgotten. Join <b>" SETUP_AP_NAME "</b> to set it up again.");
    delay(500);
    g_wm.resetSettings();
    ESP.restart();
}

static bool g_updateOk = false;
static void handleUpdateDone() {
    if (!requirePin()) return;
    if (!g_updateOk) { g_web.send(400, "text/plain", "Update failed: " + String(Update.errorString())); return; }
    sendBack("Update installed. Restarting&hellip;");
    delay(800);
    ESP.restart();
}
static void handleUpdateUpload() {
    HTTPUpload &up = g_web.upload();
    if (up.status == UPLOAD_FILE_START) {
        g_updateOk = false;
        if (crossSite() || !pinOk()) return;
        ui_set_message("Updating", "Installing new firmware...");
        Update.begin(UPDATE_SIZE_UNKNOWN);
    } else if (up.status == UPLOAD_FILE_WRITE) {
        if (Update.isRunning()) Update.write(up.buf, up.currentSize);
    } else if (up.status == UPLOAD_FILE_END) {
        g_updateOk = Update.isRunning() && Update.end(true);
        if (!g_updateOk) ui_set_message(nullptr, nullptr);
    }
}

static void startWeb() {
    static const char *hdrs[] = { "Origin", "Authorization" };
    g_web.collectHeaders(hdrs, 2);
    g_web.on("/", HTTP_GET, handleRoot);
    g_web.on("/save", HTTP_POST, handleSave);
    g_web.on("/pin", HTTP_POST, handlePin);
    g_web.on("/refresh", HTTP_POST, handleRefresh);
    g_web.on("/wifi", HTTP_POST, handleWifiReset);
    g_web.on("/update", HTTP_POST, handleUpdateDone, handleUpdateUpload);
    g_web.onNotFound([] { g_web.send(404, "text/plain", "not found"); });
    g_web.begin();
    if (MDNS.begin(MDNS_HOST)) MDNS.addService("http", "tcp", 80);
    Serial.printf("[web] http://%s/  (http://" MDNS_HOST ".local/)\n", WiFi.localIP().toString().c_str());
}

// ------------------------------------------------------------------ touch actions
static uint32_t g_modeSaveAt = 0;

static void nextView() {
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (g_set.src == SRC_ETSY)        g_set.etMode = (g_set.etMode + 1) % ET_COUNT;
    else if (g_set.src == SRC_STATUS) g_set.stMode = (g_set.stMode + 1) % ST_COUNT;
    else if (g_set.src == SRC_SKY)    g_set.skyScr = skyNextEnabled(g_set, g_set.skyScr);
    else                              g_set.fkMode = (g_set.fkMode + 1) % FK_COUNT;
    const bool sky = g_set.src == SRC_SKY;
    const int  skyScr = g_set.skyScr;
    xSemaphoreGive(g_lock);
    if (sky && skyScr != DS_PLANES) {         // Planes: the fetch task asks when the budget allows
        // Fetch only if the reply on hand does not carry this screen (auto-cycle fetches the next one ahead).
        sky_lock();
        const sky::Reply *rp = sky_reply();
        const bool inReply = rp && rp->scr[skyIdOf(skyScr)].state != sky::ST_NONE;
        sky_unlock();
        if (!inReply) g_fetchNow = true;
    }
    renderView();
}

static void screenOn() {
    g_screenOff = false;
    display::setBrightness(g_set.bright);
    g_lastCycle = millis();
}

static void onTap() {
    if (g_screenOff) { screenOn(); return; }
    if (!g_set.tapCycles) return;
    nextView();
    g_lastCycle = millis();             // a tap restarts the auto-cycle clock
    g_modeSaveAt = millis() + 3000;     // save once the taps settle
}

// ---- settings screen ----
static const uint8_t BRIGHT_STEPS[] = { 10, 26, 51, 82, 128, 179, 255 };
static uint32_t g_menuAt = 0;           // last interaction; closes itself after 30 s

static void showMenu() {
    MenuInfo m;
    if (g_netUp) {
        strlcpy(m.ip, WiFi.localIP().toString().c_str(), sizeof(m.ip));
        strlcpy(m.wifi, WiFi.SSID().c_str(), sizeof(m.wifi));
    } else {
        strlcpy(m.wifi, "setup hotspot: " SETUP_AP_NAME, sizeof(m.wifi));
    }
    static const char *srcShort[SRC_COUNT] = { "FairKeep", "Etsy", "Server status", "Sky" };
    snprintf(m.line, sizeof(m.line), "v" FW_VERSION "  -  %s  -  PIN %s", srcShort[g_set.src], g_set.pin.c_str());
    m.brightPct = (g_set.bright * 100 + 127) / 255;
    m.cycleSec = g_set.cycleOn ? g_set.cycleS : 0;
    ui_menu_show(m);
    g_menuAt = millis();
}

static void onMenu(int action) {
    g_menuAt = millis();
    switch (action) {
    case MA_BRIGHT_DOWN:
    case MA_BRIGHT_UP: {
        int i = 0;
        while (i < (int)sizeof(BRIGHT_STEPS) - 1 && BRIGHT_STEPS[i] < g_set.bright) ++i;
        if (action == MA_BRIGHT_UP)   i = BRIGHT_STEPS[i] > g_set.bright ? i : i + 1;
        else                          i = i - 1;
        i = constrain(i, 0, (int)sizeof(BRIGHT_STEPS) - 1);
        g_set.bright = BRIGHT_STEPS[i];
        display::setBrightness(g_set.bright);
        g_modeSaveAt = millis() + 3000;
        showMenu();
        break;
    }
    case MA_CYCLE:
        g_set.cycleOn = !g_set.cycleOn;
        g_lastCycle = millis();
        g_modeSaveAt = millis() + 1000;
        renderView();
        showMenu();
        break;
    case MA_REFRESH:
        g_fetchNow = true;
        ui_menu_hide();
        ui_set_status("refreshing...", false);
        break;
    case MA_SCREEN_OFF:
        ui_menu_hide();
        g_screenOff = true;
        display::setBrightness(0);
        break;
    case MA_DONE:
    default:
        ui_menu_hide();
        break;
    }
}

static void onLongPress() {
    if (g_screenOff) { screenOn(); return; }
    showMenu();
}

// ------------------------------------------------------------------ status line
static void refreshScreenState() {
    String err, warn; uint32_t lastOk; bool have; String key;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    err = g_err; warn = g_warn; lastOk = g_lastOkMs; key = g_set.key();
    have = g_set.src == SRC_ETSY ? g_et.have : g_set.src == SRC_STATUS ? g_st.have : g_fk.have;
    const uint8_t src = g_set.src;
    const bool hasLoc = g_set.skyHasLoc();
    xSemaphoreGive(g_lock);
    if (src == SRC_SKY) {                    // after g_lock is released
        sky_lock(); have = sky_reply() != nullptr; sky_unlock();
        if (!have) { planes_lock(); have = planes_reply() != nullptr; planes_unlock(); }
    }

    if (!g_netUp) {
        ui_set_message("Set up WiFi", "On your phone, join\n" SETUP_AP_NAME "\nthen pick your network and paste your dial key.");
        ui_set_status("", false);
        scope_set_visible(false);
        return;
    }
    if (src == SRC_SKY && !hasLoc) {
        String body = "at http://" + WiFi.localIP().toString() + "/\n(deskdial.local)";
        ui_set_message("Set your location", body.c_str());
        ui_set_status("", false);
        scope_set_visible(false);
        return;
    }
    if (src != SRC_SKY && key.isEmpty()) {
        String body = "Open http://" + WiFi.localIP().toString() + "/\non this WiFi and paste your " +
                      (src == SRC_ETSY ? "Etsy dial service key." : src == SRC_STATUS ? "status service key." : "FairKeep API key.");
        ui_set_message("Add key", body.c_str());
        ui_set_status("", false);
        return;
    }
    if (!have && err.length()) {
        String body = err + "\n\nSettings: http://" + WiFi.localIP().toString() + "/";
        ui_set_message(src == SRC_ETSY ? "Can't load Etsy" : src == SRC_STATUS ? "Can't load status"
                       : src == SRC_SKY ? "Can't load the sky" : "Can't load sales", body.c_str());
        ui_set_status("", false);
        scope_set_visible(false);
        return;
    }
    ui_set_message(nullptr, nullptr);
    scope_set_visible(true);

    char line[96];
    if (err.length()) {
        snprintf(line, sizeof(line), "%s", err.c_str());
        ui_set_status(line, true);
    } else if (warn.length()) {
        snprintf(line, sizeof(line), "%s", warn.c_str());
        ui_set_status(line, true);
    } else if (lastOk) {
        const uint32_t s = (millis() - lastOk) / 1000;
        if (s < 60) snprintf(line, sizeof(line), "updated just now");
        else        snprintf(line, sizeof(line), "updated %lum ago", (unsigned long)(s / 60));
        ui_set_status(line, false);
    } else {
        ui_set_status("loading...", false);
    }
}

// ------------------------------------------------------------------ setup / loop
void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\n[" FW_NAME " " FW_VERSION "] boot");
    g_lock = xSemaphoreCreateMutex();
    loadSettings();
    if (g_set.pin.isEmpty()) {      // first boot on 1.3.1+: make up this dial's PIN
        char b[8];
        snprintf(b, sizeof(b), "%06lu", (unsigned long)(esp_random() % 1000000UL));
        g_set.pin = b;
        saveSettings();
    }
    g_dataSrc = g_set.src;

    if (!display::begin()) Serial.println("[boot] display failed");
    battery_begin();
    display::setBrightness(g_set.bright);
    ui_create(onTap, onLongPress);
    if (g_set.src == SRC_SKY) skyActivate(true);
    ui_menu_set_callback(onMenu);
    ui_set_cents(g_set.cents);
    renderView();
    ui_set_message("VARO Desk Dial", "Connecting to WiFi...");
    scope_set_visible(false);
    display::loop();

    // Setup hotspot: non-blocking so the screen keeps drawing while it is open.
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(MDNS_HOST);
    g_wm.setDebugOutput(false);
    g_wm.setTitle(FW_NAME);
    g_wm.setConfigPortalBlocking(false);
    g_wm.setConnectTimeout(20);
    g_wm.addParameter(&g_wmKey);
    g_wm.addParameter(&g_wmSky);
    g_wm.addParameter(&g_wmLat);
    g_wm.addParameter(&g_wmLon);
    g_wm.setSaveConfigCallback([] { g_portalSaved = true; });
    g_wm.setCustomHeadElement(
        "<style>body{background:#0b1424;color:#f4ebdd}button{background:#f2a93b;color:#0b1424}"
        "input{background:#131f35;color:#f4ebdd;border:1px solid #2a3a58}</style>");
    if (g_wm.autoConnect(SETUP_AP_NAME)) {
        g_netUp = true;
        startWeb();
    } else {
        Serial.println("[wifi] setup hotspot open: " SETUP_AP_NAME);
    }

    xTaskCreatePinnedToCore(fetchTask, "fetch", 12288, nullptr, 1, nullptr, 0);
    refreshScreenState();
}

void loop() {
    display::loop();

    if (!g_netUp) {
        g_wm.process();
        if (g_portalSaved) {
            applyKey(String(g_wmKey.getValue()));
            {
                String k = g_wmKey.getValue(); k.trim();
                float la, lo;
                xSemaphoreTake(g_lock, portMAX_DELAY);
                if (!k.length() && !strcmp(g_wmSky.getValue(), "1")) g_set.src = SRC_SKY;   // a typed key wins
                if (parseCoord(g_wmLat.getValue(), 90, la) && parseCoord(g_wmLon.getValue(), 180, lo)) {
                    g_set.skyLat = la; g_set.skyLon = lo;
                }
                xSemaphoreGive(g_lock);
            }
            saveSettings();
            Serial.println("[wifi] saved from setup page -> restarting");
            ui_set_message("Saved", "Restarting...");
            for (int i = 0; i < 20; ++i) { display::loop(); delay(25); }
            ESP.restart();
        }
    } else {
        g_web.handleClient();
    }

    bool dirty = false;
    xSemaphoreTake(g_lock, portMAX_DELAY);
    if (g_dirty) { dirty = true; g_dirty = false; }
    xSemaphoreGive(g_lock);
    if (dirty) { renderView(); refreshScreenState(); }

    static uint32_t lastStatus = 0;
    if (millis() - lastStatus > 5000) { lastStatus = millis(); refreshScreenState(); }

    static uint32_t lastBatt = 0;
    if (millis() - lastBatt > 30000 || lastBatt == 0) {
        lastBatt = millis();
        ui_set_battery(battery_percent(), battery_charging(), battery_present());
    }

    if (g_modeSaveAt && (int32_t)(millis() - g_modeSaveAt) >= 0) { g_modeSaveAt = 0; saveSettings(); }

    if (ui_menu_open() && millis() - g_menuAt > 30000) ui_menu_hide();

    // Auto-cycle: step to the next view on its own (not saved, so the flash isn't worn).
    if (g_set.cycleOn && !g_screenOff && !ui_menu_open() &&
        millis() - g_lastCycle >= (uint32_t)g_set.cycleS * 1000) {
        g_lastCycle = millis();
        nextView();
    }

    static uint32_t lastFrame = 0;
    if (g_set.src == SRC_SKY && scope_is_open() && !g_screenOff && !ui_menu_open() && millis() - lastFrame >= 200) {
        lastFrame = millis();
        if (g_set.skyScr == DS_PLANES) planeFrame();
        else                           skyFrame();
    }

    delay(5);
}
