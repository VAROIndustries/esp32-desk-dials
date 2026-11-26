#pragma once
// Sky scope: ONE custom-drawn LVGL object (rings, compass letters, dots, tails) plus four labels.
// Nothing is allocated until scope_open; scope_close frees everything.
#include <lvgl.h>
#include "sky_model.h"

struct ScopeDot {
    int16_t x, y;                  // offset from the scope centre, px
    int16_t tx[2], ty[2];          // tail: 2 earlier positions (same frame), oldest last
    uint8_t ntail;                 // 0..2
    uint8_t style;                 // SD_FILL, SD_HOLLOW (old / hobby), SD_HILITE (readout object)
};
enum : uint8_t { SD_FILL = 0, SD_HOLLOW = 1, SD_HILITE = 2 };
constexpr int SCOPE_R = 170;       // rim radius, px

void scope_open(lv_obj_t *parent);            // creates the scope (no-op if open)
void scope_close();                           // deletes it and frees everything
bool scope_is_open();
void scope_set_dots(const ScopeDot *d, int n);   // n <= sky::MAX_OBJ; invalidates the scope area
void scope_set_text(const char *header, const char *line1, const char *line2, const char *footer);
void scope_set_tails(bool on);                // gate fallback 2
void scope_set_visible(bool on);              // hide/show the scope and its labels (no-op if not open)
