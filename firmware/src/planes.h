#pragma once
// Planes on the Sky source: GET <url>/<lat>/<lon>/<radius_nm> (adsb.lol "v2/point") over HTTPS
// verified with ROOT_CAS into a PSRAM double buffer, on a daily request budget; and, only on
// dials with a sky key saved, one route at a time from adsbdb, cached in RAM. planes_fetch and
// planes_route_tick run on the fetch task (core 0); the UI loop (core 1) reads under planes_lock().
#include <Arduino.h>
#include "plane_model.h"

struct PlaneRequest { String url, key; double lat, lon; float radiusNm; uint16_t perDay; };
enum PlaneErr : uint8_t { PL_OK = 0, PL_WAIT, PL_LIMITED, PL_UNREACHABLE, PL_HTTP, PL_BAD_REPLY, PL_NO_MEMORY };

bool                 planes_begin();       // PSRAM: 2 Replies + body buffer + route cache; false = no memory
void                 planes_end();         // safe while a fetch runs (deferred free, as sky.cpp)
PlaneErr             planes_fetch(const PlaneRequest &r);   // fetch task only; PL_WAIT = budget says not yet
int32_t              planes_wait_s();      // seconds until the budget allows the next request (0 = now, -1 = done for today)
const planes::Reply *planes_reply();       // nullptr until the first good reply; hold planes_lock()
uint32_t             planes_reply_ms();    // millis() when it arrived
void                 planes_lock();
void                 planes_unlock();

// Routes (call only when a sky key is saved). Non-blocking for the UI: the fetch task calls
// planes_route_tick(), which does at most one lookup per 20 s for the callsign last asked for.
void planes_route_want(const char *callsign);                      // UI side
bool planes_route_get(const char *callsign, char *out, size_t n);  // cached text; false = none yet / no route
void planes_route_tick();                                          // fetch task
