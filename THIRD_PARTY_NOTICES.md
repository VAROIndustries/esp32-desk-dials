# Third-party notices

Code and assets in this repository that come from other projects:

| What | Where | License |
|------|-------|---------|
| Display, touch (CST9217) and PMIC (AXP2101) bring-up, pin map and LVGL config, adapted from [Capsule Radar](https://github.com/socquique/capsule-radar) by Quique Tortosa | `firmware/src/display.cpp`, `touch*`, `battery*`, `config.h`, `include/lv_conf.h` | MIT — [licenses/capsule-radar-MIT.txt](licenses/capsule-radar-MIT.txt) |
| Montserrat font (digits rendered to `font_num_64.c` / `font_num_96.c`, and the TTF used to regenerate them) | `firmware/src/font_num_*.c`, `firmware/tools/Montserrat-Medium.ttf` | SIL Open Font License 1.1 — [licenses/montserrat-OFL-1.1.txt](licenses/montserrat-OFL-1.1.txt) |
| Root CA certificates from the Mozilla CA bundle (via certifi) | `firmware/src/certs.h` | Mozilla Public License 2.0 |

Libraries downloaded at build time (not stored here) keep their own licenses:
[LVGL](https://github.com/lvgl/lvgl) 8.4, [GFX Library for Arduino](https://github.com/moononournation/Arduino_GFX),
[ArduinoJson](https://github.com/bblanchon/ArduinoJson), [WiFiManager](https://github.com/tzapu/WiFiManager),
[XPowersLib](https://github.com/lewisxhe/XPowersLib), the [Arduino core for ESP32](https://github.com/espressif/arduino-esp32),
and [ESP Web Tools](https://github.com/esphome/esp-web-tools) (loaded by the installer page).

The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not
endorsed or certified by Etsy, Inc. Waveshare is a trademark of its owner; this project is not
affiliated with Waveshare.
