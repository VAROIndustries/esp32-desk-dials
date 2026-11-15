#!/usr/bin/env bash
# Regenerates src/font_num_96.c and src/font_num_64.c (big digits) from Montserrat Medium.
# Needs Node (npx) and the TTF that ships with LVGL (copied here as Montserrat-Medium.ttf).
set -e
cd "$(dirname "$0")"
for sz in 96 64; do
  npx -y lv_font_conv@1 --font Montserrat-Medium.ttf -r '0x24,0x2C-0x2E,0x30-0x39,0x4B,0x6B' \
    --size $sz --bpp 4 --format lvgl --no-compress --lv-include lvgl.h -o ../src/font_num_$sz.c
done
