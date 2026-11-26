#pragma once
// VARO Desk Dial — board + app constants.
// Board: Waveshare ESP32-S3-Touch-AMOLED-1.75 (CO5300 466x466 QSPI AMOLED, CST9217 touch,
// AXP2101 PMIC). Pin map taken from Capsule Radar (MIT, (c) 2026 Quique Tortosa), which
// verified it against ESPHome, xiaozhi-esp32 and the Waveshare schematic.

#define FW_NAME             "VARO Desk Dial"
#define FW_VERSION          "1.4.0-beta2"
#define BOARD_NAME          "ESP32-S3-Touch-AMOLED-1.75"

#define SCREEN_W            466
#define SCREEN_H            466
#define LVGL_BUF_LINES      40

// ---------- Panel ----------
#define PIN_LCD_CS          12
#define PIN_LCD_RST         39
#define PIN_LCD_SCLK        38
#define PIN_LCD_D0          4
#define PIN_LCD_D1          5
#define PIN_LCD_D2          6
#define PIN_LCD_D3          7
#define LCD_COL_OFFSET      6
#define LCD_ROW_OFFSET      0
#define LCD_QSPI_HZ         80000000

// ---------- Shared I2C (touch + PMIC) ----------
#define PIN_I2C_SDA         15
#define PIN_I2C_SCL         14

// ---------- Touch ----------
#define TOUCH_DRIVER_CST9217 1
#define I2C_ADDR_TOUCH      0x5A
#define PIN_TP_INT          11
#define PIN_TP_RST          40
#define TP_MIRROR_X         true
#define TP_MIRROR_Y         true

// ---------- PMIC ----------
#define BOARD_HAS_PMIC      1

// ---------- App defaults ----------
#define DEFAULT_API_BASE    "https://api.fairkeep.app/api/v1"
#define DEFAULT_ETSY_BASE   "https://etsydial.varo.industries"
#define DEFAULT_STATUS_BASE "https://dialstatus.varo.industries"
#define DEFAULT_SKY_BASE    "https://sky.varo.industries"
#define DEFAULT_POLL_S      60      // FairKeep allows 60 calls/min per key; once a minute is plenty
#define MIN_POLL_S          30
#define MAX_POLL_S          900
#define DEFAULT_BRIGHTNESS  200
#define SETUP_AP_NAME       "DeskDial-Setup"
#define MDNS_HOST           "deskdial"   // -> http://deskdial.local/
