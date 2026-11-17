// VARO Desk Dial — round 466x466 UI (LVGL 8).
#include "ui.h"
#include "config.h"
#include <lvgl.h>
#include <stdio.h>
#include <string.h>
#include <math.h>

LV_FONT_DECLARE(font_num_96);
LV_FONT_DECLARE(font_num_64);

// FairKeep / Live View palette
#define C_BG       lv_color_hex(0x0B1424)
#define C_CREAM    lv_color_hex(0xF4EBDD)
#define C_DIM      lv_color_hex(0x8A93A6)
#define C_MARIGOLD lv_color_hex(0xF2A93B)
#define C_GREEN    lv_color_hex(0x4ADE80)
#define C_RED      lv_color_hex(0xF87171)
#define C_RING     lv_color_hex(0x1E2A40)
#define C_STOPPED  lv_color_hex(0x3A4660)

static lv_obj_t *s_scr, *s_ring;
static lv_obj_t *s_hdr, *s_big, *s_sub;
static lv_obj_t *s_colHdr[2], *s_colVal[2], *s_colSub[2];
static lv_obj_t *s_msgTitle, *s_msgBody;
static lv_obj_t *s_status, *s_batt;
#define MAX_DOTS 4
static lv_obj_t *s_dots[MAX_DOTS];
#define MAX_RING 96
static lv_obj_t *s_ringDots[MAX_RING];

static int       s_dotCount = 0, s_dotActive = 0;
static bool      s_cents = false;
static bool      s_msgShown = false;
static ViewData  s_view;
static void (*s_onTap)() = nullptr;
static void (*s_onLong)() = nullptr;

// ---------- helpers ----------
static void fmt_money(char *out, size_t n, float v, bool cents) {
    const bool neg = v < 0;
    if (neg) v = -v;
    if (v >= 1000000.f) { snprintf(out, n, "%s$%.2fM", neg ? "-" : "", v / 1000000.f); return; }
    if (v >= 100000.f)  { snprintf(out, n, "%s$%.1fk", neg ? "-" : "", v / 1000.f);    return; }
    long whole = cents ? (long)v : lroundf(v);
    int  c = cents ? (int)lroundf((v - (float)whole) * 100.f) : 0;
    if (c == 100) { whole += 1; c = 0; }
    char digits[16];
    snprintf(digits, sizeof(digits), "%ld", whole);
    char grouped[24];
    const int len = (int)strlen(digits);
    int gi = 0;
    for (int i = 0; i < len; ++i) {
        grouped[gi++] = digits[i];
        const int rem = len - 1 - i;
        if (rem > 0 && rem % 3 == 0) grouped[gi++] = ',';
    }
    grouped[gi] = 0;
    if (cents) snprintf(out, n, "%s$%s.%02d", neg ? "-" : "", grouped, c);
    else       snprintf(out, n, "%s$%s", neg ? "-" : "", grouped);
}

// Biggest font in `fonts` that keeps the text inside maxW.
static void fit_text(lv_obj_t *lbl, const char *txt, const lv_font_t *const *fonts, int count, int maxW) {
    lv_label_set_text(lbl, txt);
    for (int i = 0; i < count; ++i) {
        lv_obj_set_style_text_font(lbl, fonts[i], 0);
        lv_point_t sz;
        lv_txt_get_size(&sz, txt, fonts[i], 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        if (sz.x <= maxW) return;
    }
}

static lv_obj_t *mk_label(const lv_font_t *f, lv_color_t c, lv_align_t a, int x, int y) {
    lv_obj_t *l = lv_label_create(s_scr);
    lv_obj_set_style_text_font(l, f, 0);
    lv_obj_set_style_text_color(l, c, 0);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(l, a, x, y);
    lv_label_set_text(l, "");
    return l;
}

static lv_color_t tone_color(uint8_t t) {
    switch (t) { case 1: return C_MARIGOLD; case 2: return C_RED; case 3: return C_CREAM; default: return C_GREEN; }
}

static int sev(char c) { return c == 'D' ? 3 : c == 'W' ? 2 : c == 'U' ? 1 : 0; }

// One dot per resource round the edge, worst-first when there are more than fit.
static void render_ring(const char *ring, bool show) {
    const int n = show ? (int)strlen(ring) : 0;
    const int shown = n > MAX_RING ? MAX_RING : n;
    for (int i = 0; i < MAX_RING; ++i) {
        if (i >= shown) { lv_obj_add_flag(s_ringDots[i], LV_OBJ_FLAG_HIDDEN); continue; }
        // bucket i covers ring[a..b); show the worst state in it
        const int a = i * n / shown, b = (i + 1) * n / shown;
        char c = 'S';
        for (int k = a; k < b; ++k) if (sev(ring[k]) > sev(c)) c = ring[k];
        const lv_color_t col = c == 'D' ? C_RED : c == 'W' ? C_MARIGOLD : c == 'U' ? C_GREEN : C_STOPPED;
        // spread over the circle, starting at the top, leaving a gap round the battery label
        const float gap = 0.32f;                       // radians kept clear at 12 o'clock
        const float ang = -1.5708f + gap / 2 + (6.2832f - gap) * (shown == 1 ? 0.5f : (float)i / (shown - 1));
        const int r = 212;
        lv_obj_set_style_bg_color(s_ringDots[i], col, 0);
        lv_obj_align(s_ringDots[i], LV_ALIGN_CENTER, (int)lroundf(r * cosf(ang)), (int)lroundf(r * sinf(ang)));
        lv_obj_clear_flag(s_ringDots[i], LV_OBJ_FLAG_HIDDEN);
    }
}

// ---------- render ----------
static void render() {
    const ViewData &v = s_view;
    const bool two = v.twoCol;
    for (int i = 0; i < MAX_DOTS; ++i) {
        lv_obj_set_style_bg_color(s_dots[i], i == s_dotActive ? C_MARIGOLD : C_RING, 0);
        lv_obj_align(s_dots[i], LV_ALIGN_CENTER, -9 * (s_dotCount - 1) + i * 18, 140);
        if (s_msgShown || i >= s_dotCount) lv_obj_add_flag(s_dots[i], LV_OBJ_FLAG_HIDDEN);
        else                               lv_obj_clear_flag(s_dots[i], LV_OBJ_FLAG_HIDDEN);
    }

    render_ring(v.ring, v.have && !s_msgShown);

    lv_obj_t *const cols[] = { s_colHdr[0], s_colVal[0], s_colSub[0], s_colHdr[1], s_colVal[1], s_colSub[1] };
    if (s_msgShown) {
        lv_obj_add_flag(s_hdr, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_big, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_sub, LV_OBJ_FLAG_HIDDEN);
        for (lv_obj_t *o : cols) lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    lv_obj_clear_flag(s_hdr, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(s_big, LV_OBJ_FLAG_HIDDEN);
    for (lv_obj_t *o : cols) {
        if (two && v.have) lv_obj_clear_flag(o, LV_OBJ_FLAG_HIDDEN);
        else               lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
    }
    if (two && v.have) lv_obj_add_flag(s_sub, LV_OBJ_FLAG_HIDDEN);
    else               lv_obj_clear_flag(s_sub, LV_OBJ_FLAG_HIDDEN);

    lv_label_set_text(s_hdr, v.header);

    lv_obj_set_style_text_color(s_big, v.have ? tone_color(v.tone) : C_GREEN, 0);
    lv_obj_set_style_text_font(s_sub, v.listSub ? &lv_font_montserrat_20 : &lv_font_montserrat_24, 0);
    lv_obj_set_style_text_line_space(s_sub, v.listSub ? 4 : 0, 0);
    lv_obj_set_width(s_sub, v.listSub ? 340 : 330);

    if (!v.have) {
        lv_obj_set_style_text_font(s_big, &lv_font_montserrat_48, 0);
        lv_label_set_text(s_big, "...");
        lv_obj_align(s_big, LV_ALIGN_CENTER, 0, -10);
        lv_label_set_text(s_sub, "loading");
        return;
    }

    char money[32], buf[sizeof(v.sub)];
    if (v.bigText[0]) {
        // The number fonts only carry digits and $ , - . / % k K; anything else uses Montserrat.
        bool numOnly = true;
        for (const char *c = v.bigText; *c; ++c) if (!strchr("0123456789$,-./%kK", *c)) numOnly = false;
        static const lv_font_t *const numFonts[] = { &font_num_96, &font_num_64, &lv_font_montserrat_48 };
        static const lv_font_t *const txtFonts[] = { &lv_font_montserrat_48, &lv_font_montserrat_40, &lv_font_montserrat_28 };
        fit_text(s_big, v.bigText, numOnly ? numFonts : txtFonts, 3, v.listSub ? 340 : 380);
        lv_obj_align(s_big, LV_ALIGN_CENTER, 0, v.listSub ? -44 : -6);
        lv_label_set_text(s_sub, v.sub);
        lv_obj_align(s_sub, LV_ALIGN_CENTER, 0, v.listSub ? 66 : 72);
        return;
    }
    lv_obj_align(s_sub, LV_ALIGN_CENTER, 0, 72);
    fmt_money(money, sizeof(money), v.big, s_cents);
    if (two) {
        static const lv_font_t *const bigFonts[] = { &font_num_64, &lv_font_montserrat_48, &lv_font_montserrat_40 };
        fit_text(s_big, money, bigFonts, 3, 330);
        lv_obj_align(s_big, LV_ALIGN_CENTER, 0, -62);

        // One font for both columns, the biggest that fits the wider of the two.
        static const lv_font_t *const colFonts[] = { &lv_font_montserrat_40, &lv_font_montserrat_28, &lv_font_montserrat_24 };
        char colTxt[2][32];
        int pick = 2;
        for (int i = 0; i < 2; ++i) fmt_money(colTxt[i], sizeof(colTxt[i]), v.colVal[i], s_cents);
        for (int f = 0; f < 3; ++f) {
            lv_point_t a, b;
            lv_txt_get_size(&a, colTxt[0], colFonts[f], 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
            lv_txt_get_size(&b, colTxt[1], colFonts[f], 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
            if (a.x <= 170 && b.x <= 170) { pick = f; break; }
        }
        for (int i = 0; i < 2; ++i) {
            lv_label_set_text(s_colHdr[i], v.colHdr[i]);
            lv_label_set_text(s_colVal[i], colTxt[i]);
            lv_obj_set_style_text_font(s_colVal[i], colFonts[pick], 0);
            lv_label_set_text(s_colSub[i], v.colSub[i]);
        }
    } else {
        static const lv_font_t *const bigFonts[] = { &font_num_96, &font_num_64, &lv_font_montserrat_48 };
        fit_text(s_big, money, bigFonts, 3, 400);
        lv_obj_align(s_big, LV_ALIGN_CENTER, 0, -6);
        snprintf(buf, sizeof(buf), "%s", v.sub);
        lv_label_set_text(s_sub, buf);
    }
}

// ---------- events ----------
static void scr_event_cb(lv_event_t *e) {
    const lv_event_code_t code = lv_event_get_code(e);
    if (code == LV_EVENT_SHORT_CLICKED && s_onTap) s_onTap();
    else if (code == LV_EVENT_LONG_PRESSED && s_onLong) s_onLong();
}

// ---------- public ----------
void ui_create(void (*onTap)(), void (*onLongPress)()) {
    s_onTap = onTap;
    s_onLong = onLongPress;

    s_scr = lv_scr_act();
    lv_obj_set_style_bg_color(s_scr, C_BG, 0);
    lv_obj_set_style_bg_opa(s_scr, LV_OPA_COVER, 0);
    lv_obj_clear_flag(s_scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(s_scr, scr_event_cb, LV_EVENT_ALL, nullptr);

    // thin ring just inside the bezel
    s_ring = lv_obj_create(s_scr);
    lv_obj_set_size(s_ring, SCREEN_W - 14, SCREEN_H - 14);
    lv_obj_center(s_ring);
    lv_obj_set_style_radius(s_ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(s_ring, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_color(s_ring, C_RING, 0);
    lv_obj_set_style_border_width(s_ring, 3, 0);
    lv_obj_clear_flag(s_ring, (lv_obj_flag_t)(LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE));

    s_batt = mk_label(&lv_font_montserrat_16, C_DIM, LV_ALIGN_TOP_MID, 0, 34);
    s_hdr  = mk_label(&lv_font_montserrat_20, C_MARIGOLD, LV_ALIGN_CENTER, 0, -128);
    lv_obj_set_style_text_letter_space(s_hdr, 2, 0);
    s_big  = mk_label(&font_num_96, C_GREEN, LV_ALIGN_CENTER, 0, -6);
    s_sub  = mk_label(&lv_font_montserrat_24, C_CREAM, LV_ALIGN_CENTER, 0, 72);
    lv_label_set_long_mode(s_sub, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_sub, 330);
    lv_obj_align(s_sub, LV_ALIGN_CENTER, 0, 72);
    lv_label_set_recolor(s_sub, true);

    for (int i = 0; i < 2; ++i) {
        const int x = i == 0 ? -88 : 88;
        s_colHdr[i] = mk_label(&lv_font_montserrat_16, C_MARIGOLD, LV_ALIGN_CENTER, x, 18);
        lv_obj_set_style_text_letter_space(s_colHdr[i], 2, 0);
        s_colVal[i] = mk_label(&lv_font_montserrat_40, C_CREAM, LV_ALIGN_CENTER, x, 58);
        s_colSub[i] = mk_label(&lv_font_montserrat_16, C_DIM, LV_ALIGN_CENTER, x, 96);
    }

    s_msgTitle = mk_label(&lv_font_montserrat_28, C_MARIGOLD, LV_ALIGN_CENTER, 0, -70);
    s_msgBody  = mk_label(&lv_font_montserrat_20, C_CREAM, LV_ALIGN_CENTER, 0, 20);
    lv_label_set_long_mode(s_msgBody, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_msgBody, 340);
    lv_obj_align(s_msgBody, LV_ALIGN_CENTER, 0, 20);
    lv_obj_add_flag(s_msgTitle, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_msgBody, LV_OBJ_FLAG_HIDDEN);

    for (int i = 0; i < MAX_DOTS; ++i) {
        s_dots[i] = lv_obj_create(s_scr);
        lv_obj_set_size(s_dots[i], 10, 10);
        lv_obj_set_style_radius(s_dots[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_border_width(s_dots[i], 0, 0);
        lv_obj_clear_flag(s_dots[i], (lv_obj_flag_t)(LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE));
    }

    for (int i = 0; i < MAX_RING; ++i) {
        s_ringDots[i] = lv_obj_create(s_scr);
        lv_obj_set_size(s_ringDots[i], 8, 8);
        lv_obj_set_style_radius(s_ringDots[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_border_width(s_ringDots[i], 0, 0);
        lv_obj_clear_flag(s_ringDots[i], (lv_obj_flag_t)(LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE));
        lv_obj_add_flag(s_ringDots[i], LV_OBJ_FLAG_HIDDEN);
    }

    s_status = mk_label(&lv_font_montserrat_16, C_DIM, LV_ALIGN_CENTER, 0, 172);
    lv_label_set_long_mode(s_status, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_status, 280);
    lv_obj_align(s_status, LV_ALIGN_CENTER, 0, 172);

    render();
}

void ui_set_view(const ViewData &v) { s_view = v; render(); }
void ui_set_cents(bool show)  { s_cents = show; render(); }
void ui_set_dots(int count, int active) {
    s_dotCount = count < 0 ? 0 : (count > MAX_DOTS ? MAX_DOTS : count);
    s_dotActive = active;
    render();
}

void ui_set_message(const char *title, const char *body) {
    s_msgShown = (title != nullptr);
    if (s_msgShown) {
        lv_label_set_text(s_msgTitle, title);
        lv_label_set_text(s_msgBody, body ? body : "");
        lv_obj_clear_flag(s_msgTitle, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(s_msgBody, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_msgTitle, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_msgBody, LV_OBJ_FLAG_HIDDEN);
    }
    render();
}

void ui_set_status(const char *text, bool error) {
    lv_label_set_text(s_status, text ? text : "");
    lv_obj_set_style_text_color(s_status, error ? C_RED : C_DIM, 0);
}

void ui_set_battery(int percent, bool charging, bool present) {
    if (!present || percent < 0) { lv_label_set_text(s_batt, ""); return; }
    char b[24];
    snprintf(b, sizeof(b), "%s%d%%", charging ? LV_SYMBOL_CHARGE " " : "", percent);
    lv_label_set_text(s_batt, b);
}
