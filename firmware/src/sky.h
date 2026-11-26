#pragma once
// Sky source fetch: GET <base>/v1/sky over HTTPS verified with ROOT_CAS, into a PSRAM double
// buffer. sky_fetch runs on the fetch task (core 0); readers on the UI loop (core 1) take the
// current reply under sky_lock()/sky_unlock() and never see a half-written one.
#include <Arduino.h>
#include "sky_model.h"

struct SkyRequest {
    String        base, key;               // base must start with https://; key may be empty
    float         lat, lon;
    sky::ScreenId ids[3];
    int           nIds;
    int           cuts[sky::SCR_COUNT];    // degrees, indexed by ScreenId; default_cut() = not sent
};
enum SkyErr : uint8_t { SKY_OK = 0, SKY_UNREACHABLE, SKY_HTTP, SKY_BAD_REPLY, SKY_BUSY, SKY_NO_MEMORY };

bool              sky_begin();             // allocates both buffers in PSRAM; false = no memory
void              sky_end();               // frees them (deferred to the end of a running fetch)
SkyErr            sky_fetch(const SkyRequest &r, int *httpCode);   // fetch task only (core 0)
const sky::Reply *sky_reply();             // nullptr until the first good reply; hold sky_lock()
uint32_t          sky_reply_ms();          // millis() when it arrived
void              sky_lock();
void              sky_unlock();
uint16_t          sky_next_poll_s();       // from the reply, or back-off after errors
