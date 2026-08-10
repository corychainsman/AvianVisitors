# Inkplate over-the-air firmware updates

Between 6:00 a.m. and midnight in Wellesley (`America/New_York`), the Inkplate
checks `/inkplate-ota/manifest.txt` on its configured backend no more than once
per hour. It accepts only a higher numeric build and only a firmware image
signed by the repository's paired RSA-4096 key. Display-data checks retain
their normal 15-minute cadence inside that window; neither check runs during
the midnight-to-6:00 a.m. quiet window.

The physical Wake button on GPIO 18 also wakes the Inkplate from deep sleep.
To force an immediate firmware check, press it five times and hold the fifth
press for at least three seconds. Complete the sequence within ten seconds.
This explicit gesture checks for firmware even during the quiet window, but it
does not fetch refreshed display data outside normal hours.

A quick triple press toggles trains in the Inkplate collage. The choice is
stored across sleep, reboot, and firmware updates; trains are enabled by
default. This manual gesture immediately fetches the selected frame even in
the overnight quiet window, but does not perform an overnight firmware check.

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
