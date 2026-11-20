#pragma once
#include <stdint.h>

// One screen's worth of content. main.cpp builds it from the data source + settings;
// the UI only lays it out.
struct ViewData {
    bool  have = false;            // false = still loading
    char  header[48] = "";         // small marigold line, e.g. "ETSY  -  NOVEMBER"
    float big = 0;                 // the main money figure
    char  sub[192] = "";            // under the big number, e.g. "44 orders"
    bool  twoCol = false;          // FairKeep "All three": big total + two columns
    char  colHdr[2][12] = {"", ""};
    float colVal[2] = {0, 0};
    char  colSub[2][32] = {"", ""};
    // Non-money views (Etsy to-ship, server status)
    char    bigText[16] = "";      // shown instead of the money figure when set
    uint8_t tone = 0;              // big number colour: 0 green, 1 amber, 2 red, 3 cream
    bool    listSub = false;       // sub is a short list (smaller font, LVGL #rrggbb recolour)
    char    ring[161] = "";        // status ring, one char per resource: U up, W warn, D down, S stopped
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

// ---- on-dial settings screen (long-press) ----
struct MenuInfo {
    char ip[24]    = "";        // "192.168.1.50"
    char wifi[33]  = "";        // SSID
    char line[48]  = "";        // "v1.3.0  -  Etsy"
    int  brightPct = 50;        // 4..100
    int  cycleSec  = 0;         // 0 = auto-cycle off
};
enum MenuAction : int { MA_BRIGHT_DOWN, MA_BRIGHT_UP, MA_CYCLE, MA_REFRESH, MA_SCREEN_OFF, MA_DONE };
void ui_menu_set_callback(void (*cb)(int action));
void ui_menu_show(const MenuInfo &m);   // opens it, or refreshes it if already open
void ui_menu_hide();
bool ui_menu_open();
