# Inkplate over-the-air firmware updates

Between 6:00 a.m. and midnight in Wellesley (`America/New_York`), the Inkplate
checks `/inkplate-ota/manifest.txt` on its configured backend no more than once
per hour. It accepts only a higher numeric build and only a firmware image
signed by the repository's paired RSA-4096 key. Display-data checks retain
their normal 15-minute cadence inside that window; neither check runs during
the midnight-to-6:00 a.m. quiet window.

The physical Wake button on GPIO 18 opens a small status menu at the top left,
using the installed Inkplate library's `displayPartial()` support. It shows the
Wi-Fi network and last connection result (the radio is asleep), estimated battery
percentage and measured voltage, last successful screen update, and next scheduled
data check. It also shows the running e-ink firmware version/build and the last
server version fetched from `/avian/server-version.txt`. A missing server version
shows `Unknown`; a network failure retains the last known version. Bump
`avian/server-version.txt` when releasing server changes (calendar version
`YYYY.MM.DD.release`). The file ships with the server image.
Times use Wellesley local time. Opening the menu does not wait for Wi-Fi.

Press Wake again to fetch and redraw the latest data immediately, including during
quiet hours. With no second press, the menu begins restoring the underlying image
after ten seconds of visibility, then resumes the existing scheduled wake. The
panel's physical refresh takes additional time when showing and removing the menu.

To force an immediate firmware check, press it five times and hold the fifth
press for at least three seconds. Complete the sequence within the menu interval.
This explicit gesture checks for firmware even during the quiet window, but it
does not fetch refreshed display data outside normal hours.

A quick triple press toggles trains in the Inkplate collage. The choice is
stored across sleep, reboot, and firmware updates; trains are enabled by
default. This manual gesture immediately fetches the selected frame even in
the overnight quiet window, but does not perform an overnight firmware check.

The menu's background is cached in the existing SPIFFS data partition so it can
be restored after deep sleep without downloading the frame. The first successful
frame refresh after upgrading seeds this cache. If a valid matching cache is
unavailable, the menu is skipped to preserve the image; the button gestures still
work. An erased data partition is initialized automatically. If existing storage
cannot mount, firmware 1.3.3 first uploads the entire 6.75 MiB partition to
`/avian/api/inkplate-cache-backup.php`. It requires a matching SHA-256/length
receipt before formatting the named `spiffs` partition at `0x1910000`. Backups
and offset metadata are retained in `runtime/config/InkplateCacheBackups/`,
outside the webroot. Failed backups preserve the partition; interrupted formatting
resumes using the already-verified backup. Wi-Fi credentials and firmware slots
are separate and untouched. The device reports `menu-ready` after saving and
reading back the background, or the specific failure through its status endpoint. Battery percentage is an
estimate from voltage, not a calibrated fuel-gauge reading.

### Wake-menu verification

Run `python3 -m unittest tests.test_inkplate_wake -v` for host checks of the
production button logic. Compile with the same board and partition options used
by `scripts/bootstrap_inkplate_ota.sh`. Physical verification after installation:

- **a. Menu:** let one frame refresh seed the cache, then press Wake; check the
  top-left status fields and the serial log's partial-refresh duration.
- **b. Timeout:** leave the menu untouched; confirm restoration starts after ten
  seconds and the scheduled check time stays unchanged.
- **c. Refresh:** press Wake again; confirm data refreshes even if unchanged or
  during quiet hours.
- **d. Shortcuts:** check triple press and five-press/hold, including presses
  entered while the menu is rendering.
- **e. Recovery:** check image preservation with Wi-Fi unavailable and confirm a
  held button does not cause repeated rebooting.

## Publishing a release

1. Change the firmware and increment both values in
   `AvianVisitorsInkplate/firmware_version.h`.
2. Run `scripts/publish_inkplate_firmware.py` from the repository root.
3. The script compiles, signs, independently verifies, and atomically publishes
   the release under `runtime/config/InkplateOTA/`.
4. The Inkplate installs it on its next wake, reboots, confirms Wi-Fi, and POSTs
   its state to `/avian/api/inkplate-status.php`.

The private signing key is `secrets/inkplate-ota-private.pem`. It is excluded
from Git and the Docker build context. Back it up as a secret; losing it means a
one-time USB reflash is required to establish a new signing key.

## Bootstrap

The existing non-OTA firmware cannot install its own updater. Flash build
`2026080801` once over USB using the board's default 32 MB partition layout.
Every firmware change after that can be delivered over the air.

With the Inkplate connected, run `scripts/bootstrap_inkplate_ota.sh`. It pins
the dual 12.5 MB application partition layout, compiles the bootstrap build,
detects the single attached serial device, flashes, and verifies it. An
explicit serial path can be supplied as the first argument if more than one
USB serial device is present.

### Opening latency telemetry (1.3.4)

The last successful menu opening is retained in RTC memory across deep sleep and
included as `menu_opening` in ordinary status uploads. Opening never joins Wi-Fi
for telemetry. After installing, open the menu once, then either press Wake again
to refresh data (uploading immediately after dismissal) or wait for a scheduled
network wake. A power loss/reset can discard the pending sample. The server keeps
the last reported sample when an older client or a later status lacks telemetry.

All intervals are microseconds. `boot_to_ready_us` ends when the panel releases
BUSY after the refresh command; it excludes subsequent power-off and the close
animation. The ESP timer starts during ESP-IDF startup, so this is **not** a
measurement from the physical button edge: ROM/bootloader time is not included.
BUSY completion is a hardware proxy for visibility, not an optical measurement.

- `boot_to_setup_us`, `setup_to_menu_us`: runtime startup and sketch preparation.
- `cache_us`: filesystem mount, cache read and verification.
- `graphics_init_us`, `draw_us`: framebuffer initialization, text and battery read.
- `driver_prepare_us`: driver entry, semaphore and rectangle preparation.
- `panel_init_us`: power-on/reset/configuration and power-ready waits.
- `transfer_us`: partial-window setup and SPI image transfer, including readiness waits.
- `refresh_us`: refresh command through the subsequent BUSY release.
- `poweroff_us`: power-down overhead after the image is ready, recorded separately.
- `driver_total_us`, `menu_total_us`: full driver call and full opening call.
- `driver_hooks`: true only if all six expected driver checkpoints ran.

The publish/bootstrap scripts build a private copy of the installed Inkplate
library using `scripts/prepare_inkplate_timing_library.py`. Six timestamp callbacks
are inserted into the reviewed, hash-pinned partial-update method. The installed
library, command bytes, waits, voltages and closing behavior are unchanged. To
compile manually, first run that helper, then pass its printed path with
`arduino-cli compile --library <path> ...`. A compile-time stamp prevents silently
building without the instrumented library. `INKPLATE_LIBRARY_DIR` overrides the
installed library location.

### Faster monochrome waveform research (2026-10-16)

No verified GDEP133C02 monochrome-only fast waveform or selection command was
found in the checked public sources. This is an unresolved capability question,
not proof that the hardware cannot support one.

The installed Arduino driver uses the same DRF command/data for every palette.
[Soldered's ESPHome implementation](https://github.com/SolderedElectronics/Soldered-Inkplate-ESPHome/blob/main/components/inkplate_spi/inkplate13.cpp)
also exposes partial windows and a fixed waveform selection sequence, without a
monochrome-specific refresh branch. The
[Sparkle announcement](https://www.crowdsupply.com/soldered/inkplate-13spectra/updates/week-five-status-report)
documents rectangular updates, not a B/W speed mode. Good Display's
[waveform explanation](https://www.good-display.com/news/205.html) confirms that
waveform data controls refresh speed and that some applications can load external
waveforms; it does not establish that this particular panel exposes that ability.
Its [GDEP133C02 product page](https://www.good-display.com/product/559.html) advertises
fast monochrome refresh under a **different 13.3-inch monochrome product** section;
that claim does not apply to GDEP133C02.

The precise unresolved vendor question is: does GDEP133C02 on Inkplate 13SPECTRA
support a manufacturer-approved faster monochrome waveform, and if so what
register/LUT sequence, temperature limits, timing, and prior-pixel-state constraints
apply? In particular, can it transition an arbitrary six-color background to
black/white, or only black/white to black/white? A permanent monochrome area would
only help in the latter case. No vendor inquiry has been sent, and no experimental
waveform, temperature override or driving-voltage changes have been deployed.
