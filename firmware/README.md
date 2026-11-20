# Desk dial firmware

PlatformIO / Arduino (pioarduino platform, LVGL 8.4) for the Waveshare
ESP32-S3-Touch-AMOLED-1.75.

## Data sources

**FairKeep** — `GET https://api.fairkeep.app/api/v1/device/today` with a FairKeep API key
(`Authorization: Bearer bb_live_…`, scope *Reports read*). Returns today's takings: the FairKeep Live
View reduced to three numbers.

```json
{ "date": "2026-11-14", "salesBasis": "gross",
  "total": { "revenue": 2203.19, "orders": 72 },
  "pos":   { "revenue": 1886.49, "orders": 66, "shows": 2 },
  "web":   { "revenue": 316.70,  "orders": 6,  "note": null },
  "otherIncome": 0 }
```

**Etsy** — `GET <service>/summary` from an [Etsy dial service](../etsy-service/):

```json
{ "today": { "label": "Sat Nov 14", "orders": 4,   "gross": 67.09,    "fees": 32.87,   "labels": 0,       "net": 34.22 },
  "month": { "label": "November",   "orders": 44,  "gross": 1607.60,  "fees": 594.64,  "labels": 264.99,  "net": 1012.96 },
  "year":  { "label": "2026",       "orders": 473, "gross": 17613.58, "fees": 7217.61, "labels": 2873.60, "net": 10395.97 } }
```

**Server status** — `GET <service>/status` from a [status service](../status-service/).

The setup hotspot takes any of the keys: one starting `bb_live_` selects FairKeep, `st_` the status
service, anything else Etsy.

## On the dial

- **Tap**: next view (unless locked on the settings page).
- **Press and hold**: settings screen with the dial's IP address and WiFi, firmware version,
  brightness (– / +), auto-cycle on/off, refresh now, and screen off. A tap wakes a dark screen.
  It closes itself after 30 seconds.
- **Auto-cycle**: steps through the current source's views on its own, every 5 s – 2 min
  (set on the settings page; switched on/off from either place).

## Settings PIN

Each dial makes up a 6-digit PIN on its first start (1.3.1+). The web settings page, and
everything it does (save, refresh, Reset WiFi, firmware upload), asks for it: any username,
the PIN as the password. The dial shows its PIN on its own settings screen (press and hold),
and you can change it on the web page (4–8 digits). Five wrong tries lock the page for 30 s.
Over-the-air update from a script: `curl -u dial:<PIN> -F fw=@VARODeskDial-ota.bin http://<ip>/update`.

## Settings page (`http://deskdial.local/` or the dial's IP)

Data source, keys and URLs per source, view, *Tap the dial to change view* (untick to lock it),
show cents, *after Etsy fees* / *after postage labels*, refresh interval, brightness, firmware upload,
WiFi reset. Saved keys are never shown back, only their prefix; cross-site form posts are refused.

## Build

```
pio run                          # build
pio run -t upload                # flash over USB (hold BOOT while plugging in if no port shows)
python tools/make_release.py     # refresh ../docs: merged image at 0x0, OTA image, manifest
tools/make_fonts.sh              # regenerate the big-digit fonts (needs Node)
```

Bump `FW_VERSION` in `src/config.h` for each release.

## Notes

- HTTPS is verified against the root CAs in `src/certs.h` (Let's Encrypt, Google Trust Services,
  DigiCert, Sectigo/USERTrust, Amazon, SSL.com, Microsoft). Add a root there if your API host uses
  another CA.
- The settings page is unauthenticated on your LAN, like most hobby devices.
