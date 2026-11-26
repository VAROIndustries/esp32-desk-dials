#pragma once
// Sky scope: ONE custom-drawn LVGL object (rings, compass letters, dots, tails) plus four labels.
// Nothing is allocated until scope_open; scope_close frees everything.
#include <lvgl.h>
#include "sky_model.h"

struct ScopeDot {
    int16_t x, y;                  // offset from the scope centre, px
    int16_t tx[2], ty[2];          // tail: 2 earlier positions (same frame), oldest last
    uint8_t ntail;                 // 0..2
    uint8_t style;                 // SD_FILL, SD_HOLLOW (old / hobby), SD_HILITE (readout object), | SD_BIG (Moon)
    uint32_t rgb;                  // 0xRRGGBB; 0 = the theme's dot colour. A coloured SD_HILITE dot keeps
                                   // its colour and gets a white ring instead of the theme's hilite colour.
};
enum : uint8_t { SD_FILL = 0, SD_HOLLOW = 1, SD_HILITE = 2, SD_BIG = 4 };
constexpr int SCOPE_R = 170;       // rim radius, px

// Scope colour schemes (0xRRGGBB). Only the scope uses them; the screen behind it takes `bg`
// while the scope is open and goes back to the dial's navy when it closes.
struct ScopeTheme { const char *name; uint32_t bg, ring, dot, letters, hilite, header, text; };
enum : uint8_t { TH_NIGHT = 0, TH_RADAR, TH_AMBER, TH_ICE, TH_MONO, TH_COUNT };
extern const ScopeTheme SCOPE_THEMES[TH_COUNT];

void scope_open(lv_obj_t *parent);            // creates the scope (no-op if open)
void scope_close();                           // deletes it and frees everything
bool scope_is_open();
void scope_set_dots(const ScopeDot *d, int n);   // n <= sky::MAX_OBJ; invalidates the scope area
void scope_set_text(const char *header, const char *line1, const char *line2, const char *footer);
void scope_set_tails(bool on);                // gate fallback 2
void scope_set_visible(bool on);              // hide/show the scope and its labels (no-op if not open)
// North offset (0..359, the direction the top of the dial faces, clockwise from north): the
// N/E/S/W letters move round the rim (they stay upright). Dots arrive already rotated
// (sky::rotate). Rings are circles, so they do not change. Kept across scope_open/close.
void scope_set_north(int offsetDeg);
void scope_set_theme(int theme);              // TH_*; kept across scope_open/close
// Small dim labels just inside each ring (map view, e.g. "8", "17", "25 nm"); "" = none (sky view).
void scope_set_ring_labels(const char *inner, const char *mid, const char *outer);
