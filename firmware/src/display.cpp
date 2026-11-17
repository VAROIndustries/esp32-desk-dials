// VARO Desk Dial — CO5300 QSPI AMOLED + LVGL 8 + CST9217 touch.
// A trimmed-down version of Capsule Radar's display.cpp (MIT, (c) 2026 Quique Tortosa):
// same panel bring-up, 2-px-aligned flush windows and internal-RAM draw buffer, without
// the radar sweep compositor or arbitrary rotation.
#include "config.h"
#include "display.h"
#include "touch.h"

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <lvgl.h>
#include <esp_heap_caps.h>

static Arduino_DataBus   *s_bus = nullptr;
static Arduino_CO5300    *s_gfx = nullptr;
static lv_disp_draw_buf_t s_draw_buf;
static lv_disp_drv_t      s_disp_drv;
static lv_indev_drv_t     s_indev_drv;
static lv_color_t        *s_buf1 = nullptr;
static uint8_t            s_brightness = 0;

static void flush_cb(lv_disp_drv_t *drv, const lv_area_t *area, lv_color_t *px) {
    const uint16_t w = (uint16_t)(area->x2 - area->x1 + 1);
    const uint16_t h = (uint16_t)(area->y2 - area->y1 + 1);
#if (LV_COLOR_16_SWAP != 0)
    s_gfx->draw16bitBeRGBBitmap(area->x1, area->y1, (uint16_t *)px, w, h);
#else
    s_gfx->draw16bitRGBBitmap(area->x1, area->y1, (uint16_t *)px, w, h);
#endif
    lv_disp_flush_ready(drv);
}

// CO5300 needs 2-pixel-aligned windows (even start, odd end) or partial updates tear.
static void rounder_cb(lv_disp_drv_t *drv, lv_area_t *area) {
    (void)drv;
    area->x1 &= ~1;
    area->y1 &= ~1;
    area->x2 |= 1;
    area->y2 |= 1;
}

static void touch_read_cb(lv_indev_drv_t *drv, lv_indev_data_t *data) {
    (void)drv;
    static uint16_t lx = 0, ly = 0;
    static bool     down = false;
    uint16_t x, y;
    switch (touch_read(&x, &y)) {
        case TOUCH_DOWN:   lx = x; ly = y; down = true; break;
        case TOUCH_UP:     down = false; break;
        case TOUCH_NODATA: break;   // one bad I2C read is not a lift
    }
    data->point.x = lx;
    data->point.y = ly;
    data->state = down ? LV_INDEV_STATE_PR : LV_INDEV_STATE_REL;
}

namespace display {

bool begin() {
    s_bus = new Arduino_ESP32QSPI(PIN_LCD_CS, PIN_LCD_SCLK, PIN_LCD_D0, PIN_LCD_D1, PIN_LCD_D2, PIN_LCD_D3);
    s_gfx = new Arduino_CO5300(s_bus, PIN_LCD_RST, 0, SCREEN_W, SCREEN_H, LCD_COL_OFFSET, LCD_ROW_OFFSET, 0, 0);
    if (!s_gfx->begin(LCD_QSPI_HZ)) {
        Serial.println("[display] gfx->begin() FAILED");
        return false;
    }
    s_gfx->fillScreen(RGB565_BLACK);
    setBrightness(DEFAULT_BRIGHTNESS);

    lv_init();
    const size_t buf_px = (size_t)SCREEN_W * LVGL_BUF_LINES;
    s_buf1 = (lv_color_t *)heap_caps_malloc(buf_px * sizeof(lv_color_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    if (!s_buf1) s_buf1 = (lv_color_t *)heap_caps_malloc(buf_px * sizeof(lv_color_t), MALLOC_CAP_SPIRAM);
    lv_disp_draw_buf_init(&s_draw_buf, s_buf1, nullptr, buf_px);

    lv_disp_drv_init(&s_disp_drv);
    s_disp_drv.hor_res    = SCREEN_W;
    s_disp_drv.ver_res    = SCREEN_H;
    s_disp_drv.flush_cb   = flush_cb;
    s_disp_drv.rounder_cb = rounder_cb;
    s_disp_drv.draw_buf   = &s_draw_buf;
    lv_disp_drv_register(&s_disp_drv);

    if (touch_begin()) {
        lv_indev_drv_init(&s_indev_drv);
        s_indev_drv.type    = LV_INDEV_TYPE_POINTER;
        s_indev_drv.read_cb = touch_read_cb;
        lv_indev_drv_register(&s_indev_drv);
    }
    return true;
}

void loop() { lv_timer_handler(); }

void setBrightness(uint8_t v) {
    s_brightness = v;
    if (s_gfx) s_gfx->setBrightness(v);
}

uint8_t brightness() { return s_brightness; }

}  // namespace display
