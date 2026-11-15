#pragma once
#include <stdint.h>

namespace display {
bool    begin();                 // panel + LVGL + touch indev
void    loop();                  // pump LVGL; call every loop()
void    setBrightness(uint8_t v);  // 0..255 (CO5300 cmd 0x51)
uint8_t brightness();
}  // namespace display
