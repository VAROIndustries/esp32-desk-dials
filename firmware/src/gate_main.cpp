// Drawing gate (spec 5): 60 moving dots with tails at >= 5 fps, touch responsive.
#include <Arduino.h>
#include <lvgl.h>
#include <math.h>
#include "display.h"
#include "ui.h"
#include "scope.h"

static ScopeDot g_d[sky::MAX_OBJ];
static volatile int g_taps = 0;
static void onTap() { g_taps++; }
static void onLong() {}
static unsigned g_free0 = 0, g_free1 = 0;

void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) delay(10);   // native USB: give the host a moment to open the port
    display::begin();
    display::setBrightness(200);
    ui_create(onTap, onLong);
    ui_set_scope_mode(true);
    lv_mem_monitor_t m0; lv_mem_monitor(&m0);
    scope_open(ui_screen()); scope_close();
    lv_mem_monitor_t m1; lv_mem_monitor(&m1);
    g_free0 = (unsigned)m0.free_size; g_free1 = (unsigned)m1.free_size;
    Serial.printf("[gate] lvgl free before %u after open/close %u\n", g_free0, g_free1);
    scope_open(ui_screen());
}

void loop() {
    static uint32_t last = 0, frames = 0, t0 = millis();
    display::loop();
    // LVGL makes its FPS/CPU label on the first refresh at LV_USE_PERF_MONITOR_POS (a corner,
    // outside the round panel). Move it once to the free band under the scope text.
    static bool perfMoved = false;
    if (!perfMoved && lv_obj_get_child_cnt(lv_layer_sys()) > 0) {
        lv_obj_align(lv_obj_get_child(lv_layer_sys(), -1), LV_ALIGN_CENTER, 0, 190);
        perfMoved = true;
    }
    if (millis() - last >= 200) {
        last = millis();
        const float t = millis() / 1000.f;
        for (int i = 0; i < sky::MAX_OBJ; ++i) {
            const float a = t * 0.2f + i * 0.105f, r = 20 + (i * 37) % 150;
            ScopeDot &d = g_d[i];
            d.x = (int16_t)(r * cosf(a)); d.y = (int16_t)(r * sinf(a));
            for (int k = 0; k < 2; ++k) { const float b = a - 0.06f * (k + 1); d.tx[k] = (int16_t)(r * cosf(b)); d.ty[k] = (int16_t)(r * sinf(b)); }
            d.ntail = 2;
            d.style = (g_taps & 1) ? SD_HILITE : (i % 7 == 0 ? SD_HOLLOW : SD_FILL);
        }
        scope_set_dots(g_d, sky::MAX_OBJ);
        frames++;
        char l1[48];
        snprintf(l1, sizeof(l1), "%.1f fps  -  taps %d", frames * 1000.f / (millis() - t0 + 1), g_taps);
        scope_set_text("DRAWING GATE", l1, "tap: dots change colour", "");
        if (millis() - t0 > 10000) {
            // repeat the boot line so a late serial reader still sees it
            Serial.printf("[gate] lvgl free before %u after open/close %u\n", g_free0, g_free1);
            frames = 0; t0 = millis();
        }
    }
}
