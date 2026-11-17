"""Build the Sales Dial installer files into ../docs/sales (dial.varo.industries/sales/).

  python tools/make_release.py

Produces:
  docs/sales/VARODeskDial-esp32s3.bin   full image at 0x0 (bootloader + partitions + app), for the web installer
  docs/sales/VARODeskDial-ota.bin       app only, for the device's own "Firmware update" upload
  docs/sales/manifest.json              ESP Web Tools manifest (version from src/config.h)
Merge offsets/flags match Capsule Radar's release workflow for this board.
"""
import glob, json, os, re, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = "fairkeep-dial"
B = os.path.join(ROOT, ".pio", "build", ENV)
OUT = os.path.join(os.path.dirname(ROOT), "docs", "sales")   # the Sales Dial installer page

subprocess.check_call(["pio", "run", "-e", ENV], cwd=ROOT)

home = os.path.expanduser("~/.platformio/packages")
boot_app0 = (glob.glob(os.path.join(home, "framework-arduinoespressif32*", "tools", "partitions", "boot_app0.bin")) or [None])[0]
if not boot_app0:
    sys.exit("boot_app0.bin not found under ~/.platformio/packages")

version = re.search(r'#define FW_VERSION\s+"([^"]+)"', open(os.path.join(ROOT, "src", "config.h")).read()).group(1)
os.makedirs(OUT, exist_ok=True)
full = os.path.join(OUT, "VARODeskDial-esp32s3.bin")
# PlatformIO ships esptool as a package; fall back to an installed "esptool" module.
esptool = glob.glob(os.path.join(home, "tool-esptoolpy", "esptool.py"))
esp_cmd = [sys.executable, esptool[0]] if esptool else [sys.executable, "-m", "esptool"]
subprocess.check_call(esp_cmd + ["--chip", "esp32s3", "merge_bin", "-o", full,
                       "--flash_mode", "dio", "--flash_freq", "80m", "--flash_size", "16MB",
                       "0x0", os.path.join(B, "bootloader.bin"),
                       "0x8000", os.path.join(B, "partitions.bin"),
                       "0xe000", boot_app0,
                       "0x10000", os.path.join(B, "firmware.bin")])
shutil.copy(os.path.join(B, "firmware.bin"), os.path.join(OUT, "VARODeskDial-ota.bin"))
json.dump({
    "name": "VARO Desk Dial",
    "version": version,
    "new_install_prompt_erase": True,
    "builds": [{"chipFamily": "ESP32-S3", "parts": [{"path": "VARODeskDial-esp32s3.bin", "offset": 0}]}],
}, open(os.path.join(OUT, "manifest.json"), "w"), indent=2)
print(f"VARO Desk Dial {version} -> {OUT}")
