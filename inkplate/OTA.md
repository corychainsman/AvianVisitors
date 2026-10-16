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
work. Only an entirely erased data partition may be initialized automatically;
an unrecognized existing filesystem is preserved. Battery percentage is an
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
