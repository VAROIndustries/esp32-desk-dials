// Sky scope renderer: one custom-drawn object, redrawn whole on each scope_set_dots.
#include "scope.h"
#include <string.h>

#ifndef SCOPE_MAX_DOTS
#define SCOPE_MAX_DOTS sky::MAX_OBJ        // gate fallback 3: 30
#endif
#ifndef SCOPE_TAILS
#define SCOPE_TAILS 1                      // gate fallback 2: 0
#endif

static lv_obj_t *s_obj = nullptr, *s_hdr, *s_l1, *s_l2, *s_foot;
static ScopeDot  s_dots[sky::MAX_OBJ];
static int       s_n = 0;
static bool      s_tails = SCOPE_TAILS;
static int       s_north = 0;
static char      s_ringLbl[3][12] = { "", "", "" };   // inner, mid, outer

#define C_BG       lv_color_hex(0x0B1424)      // the dial's own background (ui.cpp)

//                                       name           bg        ring      dot       letters   hilite    header    text
const ScopeTheme SCOPE_THEMES[TH_COUNT] = {
    { "Night",       0x0B1424, 0x1E2A40, 0x7DD3FC, 0x8A93A6, 0xF2A93B, 0xF2A93B, 0xF4EBDD },
    { "Radar green", 0x000000, 0x0F3D1E, 0x39FF6A, 0x2FAF55, 0xE8FFE8, 0x39FF6A, 0xE8FFE8 },
    { "Amber",       0x000000, 0x3A2600, 0xFFB000, 0xB07A00, 0xFFF1C2, 0xFFB000, 0xFFF1C2 },
    { "Ice",         0x061424, 0x16324A, 0x7FE7FF, 0x7FA8C9, 0xFFFFFF, 0x7FE7FF, 0xFFFFFF },
    { "Mono",        0x000000, 0x2A2A2A, 0xE6E6E6, 0x8C8C8C, 0xFFFFFF, 0xE6E6E6, 0xFFFFFF },
};
static int s_theme = TH_NIGHT;
static lv_obj_t *s_parent = nullptr;
static inline const ScopeTheme &TH() { return SCOPE_THEMES[s_theme]; }
#define C_RINGLINE lv_color_hex(TH().ring)
#define C_DIM      lv_color_hex(TH().letters)
#define C_DOT      lv_color_hex(TH().dot)
#define C_HILITE   lv_color_hex(TH().hilite)

static void draw_cb(lv_event_t *e) {
    lv_draw_ctx_t *ctx = lv_event_get_draw_ctx(e);
    lv_obj_t *o = lv_event_get_target(e);
    lv_area_t a; lv_obj_get_coords(o, &a);
    const lv_coord_t cx = (a.x1 + a.x2) / 2, cy = (a.y1 + a.y2) / 2;

    // rings at the rim, 2/3 and 1/3 of the way in; compass letters
    lv_draw_arc_dsc_t ring; lv_draw_arc_dsc_init(&ring);
    ring.color = C_RINGLINE; ring.width = 2;
    lv_point_t c = { cx, cy };
    for (int k = 1; k <= 3; ++k) lv_draw_arc(ctx, &ring, &c, (uint16_t)(SCOPE_R * k / 3), 0, 360);
    lv_draw_label_dsc_t lbl; lv_draw_label_dsc_init(&lbl);
    lbl.color = C_DIM; lbl.font = &lv_font_montserrat_16;
    static const char *nesw[4] = { "N", "E", "S", "W" };
    static const int dx[4] = { 0, 1, 0, -1 }, dy[4] = { -1, 0, 1, 0 };
    for (int i = 0; i < 4; ++i) {
        int lx = dx[i] * (SCOPE_R + 14), ly = dy[i] * (SCOPE_R + 14);
        sky::rotate(lx, ly, s_north);              // the letter moves; its text stays upright
        lv_area_t t = { (lv_coord_t)(cx + lx - 8), (lv_coord_t)(cy + ly - 9),
                        (lv_coord_t)(cx + lx + 8), (lv_coord_t)(cy + ly + 9) };
        lv_draw_label(ctx, &lbl, &t, nesw[i], nullptr);
    }
    // ring labels: right-aligned, bottom-right corner just inside each ring on the lower-right diagonal
    lv_draw_label_dsc_t rl; lv_draw_label_dsc_init(&rl);
    rl.color = C_DIM; rl.font = &lv_font_montserrat_14; rl.align = LV_TEXT_ALIGN_RIGHT; rl.opa = LV_OPA_80;
    for (int k = 0; k < 3; ++k) {
        if (!s_ringLbl[k][0]) continue;
        const int r = SCOPE_R * (k + 1) / 3 - 4, q = (int)(r * 0.7071f);
        lv_area_t t = { (lv_coord_t)(cx + q - 60), (lv_coord_t)(cy + q - 16), (lv_coord_t)(cx + q), (lv_coord_t)(cy + q) };
        lv_draw_label(ctx, &rl, &t, s_ringLbl[k], nullptr);
    }

    lv_draw_line_dsc_t tail; lv_draw_line_dsc_init(&tail);
    tail.width = 2; tail.opa = LV_OPA_50; tail.round_end = 1;
    lv_draw_rect_dsc_t dot; lv_draw_rect_dsc_init(&dot);
    dot.radius = LV_RADIUS_CIRCLE;
    const int n = s_n < SCOPE_MAX_DOTS ? s_n : SCOPE_MAX_DOTS;
    for (int i = 0; i < n; ++i) {
        const ScopeDot &d = s_dots[i];
        lv_point_t p0 = { (lv_coord_t)(cx + d.x), (lv_coord_t)(cy + d.y) };
        const lv_color_t base = d.rgb ? lv_color_hex(d.rgb) : C_DOT;
        tail.color = base;
        if (s_tails) {
            lv_point_t prev = p0;
            for (int k = 0; k < d.ntail; ++k) {
                lv_point_t p1 = { (lv_coord_t)(cx + d.tx[k]), (lv_coord_t)(cy + d.ty[k]) };
                lv_draw_line(ctx, &tail, &prev, &p1);
                prev = p1;
            }
        }
        const bool hi = d.style & SD_HILITE;
        const int r = hi ? 7 : 5;
        lv_area_t da = { (lv_coord_t)(p0.x - r), (lv_coord_t)(p0.y - r), (lv_coord_t)(p0.x + r), (lv_coord_t)(p0.y + r) };
        const lv_color_t col = hi && !d.rgb ? C_HILITE : base;   // coloured dots keep their colour
        if (d.style & SD_HOLLOW) { dot.bg_opa = LV_OPA_TRANSP; dot.border_color = col; dot.border_width = 2; dot.border_opa = LV_OPA_COVER; }
        else                     { dot.bg_opa = LV_OPA_COVER; dot.bg_color = col; dot.border_width = 0; }
        lv_draw_rect(ctx, &dot, &da);
        if (hi && d.rgb) {                           // ...and get a white ring
            const int rr = r + 4;
            lv_area_t ra = { (lv_coord_t)(p0.x - rr), (lv_coord_t)(p0.y - rr), (lv_coord_t)(p0.x + rr), (lv_coord_t)(p0.y + rr) };
            lv_draw_rect_dsc_t ring2; lv_draw_rect_dsc_init(&ring2);
            ring2.radius = LV_RADIUS_CIRCLE; ring2.bg_opa = LV_OPA_TRANSP;
            ring2.border_color = lv_color_white(); ring2.border_width = 2; ring2.border_opa = LV_OPA_COVER;
            lv_draw_rect(ctx, &ring2, &ra);
        }
    }
}

static lv_obj_t *mk_label(lv_obj_t *p, const lv_font_t *f, lv_color_t c, int y) {
    lv_obj_t *l = lv_label_create(p);
    lv_obj_set_style_text_font(l, f, 0);
    lv_obj_set_style_text_color(l, c, 0);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(l, 300);
    lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
    lv_obj_align(l, LV_ALIGN_CENTER, 0, y);
    lv_label_set_text(l, "");
    lv_obj_clear_flag(l, LV_OBJ_FLAG_CLICKABLE);
    return l;
}

void scope_open(lv_obj_t *parent) {
    if (s_obj) return;
    s_obj = lv_obj_create(parent);
    lv_obj_remove_style_all(s_obj);
    lv_obj_set_size(s_obj, 2 * SCOPE_R + 40, 2 * SCOPE_R + 40);
    lv_obj_align(s_obj, LV_ALIGN_CENTER, 0, -18);
    lv_obj_clear_flag(s_obj, (lv_obj_flag_t)(LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE));
    lv_obj_add_event_cb(s_obj, draw_cb, LV_EVENT_DRAW_MAIN, nullptr);
    s_hdr  = mk_label(parent, &lv_font_montserrat_16, lv_color_hex(TH().header), -196);
    lv_obj_set_style_text_letter_space(s_hdr, 2, 0);
    s_l1   = mk_label(parent, &lv_font_montserrat_20, lv_color_hex(TH().text), 128);
    s_l2   = mk_label(parent, &lv_font_montserrat_16, C_DIM, 152);
    s_foot = mk_label(parent, &lv_font_montserrat_14, C_DIM, 196);
    s_parent = parent;
    lv_obj_set_style_bg_color(parent, lv_color_hex(TH().bg), 0);
    s_n = 0;
}

void scope_close() {
    if (!s_obj) return;
    lv_obj_del(s_obj); lv_obj_del(s_hdr); lv_obj_del(s_l1); lv_obj_del(s_l2); lv_obj_del(s_foot);
    if (s_parent) lv_obj_set_style_bg_color(s_parent, C_BG, 0);   // back to the dial's navy
    s_obj = nullptr; s_parent = nullptr; s_n = 0;
}

bool scope_is_open() { return s_obj != nullptr; }

void scope_set_dots(const ScopeDot *d, int n) {
    if (!s_obj) return;
    if (n < 0) n = 0;
    if (n > sky::MAX_OBJ) n = sky::MAX_OBJ;
    if (!d) n = 0;
    if (n) memcpy(s_dots, d, sizeof(ScopeDot) * n);
    s_n = n;
    lv_obj_invalidate(s_obj);          // gate fallback 1: invalidate old+new dot areas only
}

void scope_set_text(const char *h, const char *l1, const char *l2, const char *f) {
    if (!s_obj) return;
    lv_label_set_text(s_hdr, h ? h : "");
    lv_label_set_text(s_l1, l1 ? l1 : "");
    lv_label_set_text(s_l2, l2 ? l2 : "");
    lv_label_set_text(s_foot, f ? f : "");
}

void scope_set_tails(bool on) { s_tails = on; if (s_obj) lv_obj_invalidate(s_obj); }

void scope_set_visible(bool on) {
    if (!s_obj) return;
    lv_obj_t *const all[] = { s_obj, s_hdr, s_l1, s_l2, s_foot };
    for (lv_obj_t *o : all) {
        if (on) lv_obj_clear_flag(o, LV_OBJ_FLAG_HIDDEN);
        else    lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
    }
}

void scope_set_north(int offsetDeg) {
    offsetDeg %= 360;
    if (offsetDeg < 0) offsetDeg += 360;
    if (offsetDeg == s_north) return;
    s_north = offsetDeg;
    if (s_obj) lv_obj_invalidate(s_obj);
}

void scope_set_ring_labels(const char *inner, const char *mid, const char *outer) {
    const char *in[3] = { inner, mid, outer };
    bool diff = false;
    for (int k = 0; k < 3; ++k) {
        const char *v = in[k] ? in[k] : "";
        if (strncmp(s_ringLbl[k], v, sizeof(s_ringLbl[k]) - 1)) { strlcpy(s_ringLbl[k], v, sizeof(s_ringLbl[k])); diff = true; }
    }
    if (diff && s_obj) lv_obj_invalidate(s_obj);
}

void scope_set_theme(int theme) {
    if (theme < 0 || theme >= TH_COUNT) theme = TH_NIGHT;
    if (theme == s_theme) return;
    s_theme = theme;
    if (!s_obj) return;
    lv_obj_set_style_bg_color(s_parent, lv_color_hex(TH().bg), 0);
    lv_obj_set_style_text_color(s_hdr, lv_color_hex(TH().header), 0);
    lv_obj_set_style_text_color(s_l1, lv_color_hex(TH().text), 0);
    lv_obj_set_style_text_color(s_l2, C_DIM, 0);
    lv_obj_set_style_text_color(s_foot, C_DIM, 0);
    lv_obj_invalidate(s_obj);
}
