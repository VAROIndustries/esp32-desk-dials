#pragma once
#include <stdint.h>

// One screen's worth of content. main.cpp builds it from the data source + settings;
// the UI only lays it out.
struct ViewData {
    bool  have = false;            // false = still loading
    char  header[48] = "";         // small marigold line, e.g. "ETSY  -  NOVEMBER"
    float big = 0;                 // the main money figure
    char  sub[96] = "";            // under the big number, e.g. "44 orders"
    bool  twoCol = false;          // FairKeep "All three": big total + two columns
    char  colHdr[2][12] = {"", ""};
    float colVal[2] = {0, 0};
    char  colSub[2][32] = {"", ""};
};

void ui_create(void (*onTap)(), void (*onLongPress)());
void ui_set_view(const ViewData &v);
void ui_set_dots(int count, int active);   // count 0 hides them
void ui_set_cents(bool show);
// One centred message instead of the numbers (setup steps, fatal errors). nullptr = hide it.
void ui_set_message(const char *title, const char *body);
// Footer line: "updated 2m ago" (ok) or an error in red.
void ui_set_status(const char *text, bool error);
void ui_set_battery(int percent, bool charging, bool present);
