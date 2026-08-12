#!/command/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

config=/config/birdnet.conf
repo=/home/pi/BirdNET-Pi

if [[ ! -f "$config" ]]; then
    echo "AvianVisitors: BirdNET configuration is not ready" >&2
    exit 1
fi

# The attached DCMT lavalier is a mono USB capture device. Direct plughw access
# avoids depending on a host PulseAudio socket and lets ALSA perform any format
# conversion BirdNET needs.
if [[ -n "${ALSA_CARD:-}" ]]; then
    sed -i "s|^REC_CARD=.*|REC_CARD=${ALSA_CARD}|" "$config"
fi
sed -i "s|^CHANNELS=.*|CHANNELS=${AVIAN_AUDIO_CHANNELS:-1}|" "$config"
sed -i 's|^SITE_NAME=.*|SITE_NAME="Avian Visitors"|' "$config"

# The standalone base image already asks its systemctl replacement to run
# these units as pi. Its generic Docker hook also prepends `sudo -u pi` to each
# ExecStart, which fails because the LinuxServer `abc` account shares pi's UID.
# Remove only that redundant prefix; the unit's User=pi remains authoritative.
for unit in "$repo"/templates/*.service; do
    if grep -q '^User=pi$' "$unit"; then
        sed -i 's|^ExecStart=/usr/bin/sudo -u pi |ExecStart=|' "$unit"
    fi
done

# The base image tightens /tmp to 0755 during its structure pass. Chromium's
# sandboxed helper processes require the normal shared-temp mode.
chmod 1777 /tmp

# Adapt the native update helper to a container where Caddy is supervised by
# s6. `caddy reload` is harmless before the first start and reloads in place
# later; systemctl reload-or-restart would launch a competing Caddy process.
sed -i \
    -e 's|^sudo caddy |caddy |' \
    -e 's;^sudo systemctl reload-or-restart caddy.*;caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>/dev/null || true;' \
    "$repo/scripts/update_caddyfile.sh"

# The base Docker image is built on a public runner and otherwise ships with
# that runner's coordinates. Set explicit coordinates when supplied, or make
# the same one-time public-IP estimate as the native AvianVisitors installer.
location_marker=/config/.avian-location-initialized
if [[ -n "${BIRDNET_LATITUDE:-}" && -n "${BIRDNET_LONGITUDE:-}" ]]; then
    sed -i "s|^LATITUDE=.*|LATITUDE=${BIRDNET_LATITUDE}|" "$config"
    sed -i "s|^LONGITUDE=.*|LONGITUDE=${BIRDNET_LONGITUDE}|" "$config"
    printf '%s\n' explicit > "$location_marker"
elif [[ "${AVIAN_AUTO_LOCATION:-true}" == "true" && ! -f "$location_marker" ]]; then
    location_json="$(curl -fsS4 --max-time 15 http://ip-api.com/json 2>/dev/null || true)"
    if [[ "$(jq -r '.status // empty' <<<"$location_json" 2>/dev/null)" == "success" ]]; then
        latitude="$(jq -r '.lat' <<<"$location_json")"
        longitude="$(jq -r '.lon' <<<"$location_json")"
        if [[ "$latitude" =~ ^-?[0-9]+([.][0-9]+)?$ && "$longitude" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
            sed -i "s|^LATITUDE=.*|LATITUDE=${latitude}|" "$config"
            sed -i "s|^LONGITUDE=.*|LONGITUDE=${longitude}|" "$config"
            printf '%s\n' auto > "$location_marker"
            echo "AvianVisitors: estimated the BirdNET location from the public IP"
        fi
    fi
fi

# The container's structure step creates the standard BirdNET web root before
# this script runs. Add the AvianVisitors overlay and stable frame endpoints.
# shellcheck disable=SC1091
source "$config"
source "$repo/scripts/link_webroot.sh"
link_avian_visitors_webroot "$repo" "$EXTRACTED" pi

# Older train-detector builds ran as root and could create the day's output
# directory before BirdNET did, preventing BirdNET from saving any birds for
# that date.  Normalize directory ownership on every container start both to
# repair such installations and to keep the shared tree writable by uid 1000.
mkdir -p /config/BirdSongs/By_Date /config/BirdSongs/TrainDetector
find /config/BirdSongs/By_Date -type d -exec chown pi:pi {} +
chown -R pi:pi /config/BirdSongs/TrainDetector

ln -sfn /config/frame/frame.jpg "$EXTRACTED/frame.jpg"
ln -sfn /config/frame/frame.sha256 "$EXTRACTED/frame.sha256"
ln -sfn /config/frame/frame-birds.jpg "$EXTRACTED/frame-birds.jpg"
ln -sfn /config/frame/frame-birds.sha256 "$EXTRACTED/frame-birds.sha256"
mkdir -p /config/frame
chown -R pi:pi /config/frame
chmod 0755 /config/frame

# Signed Inkplate OTA releases are published into this persistent host-backed
# directory. Linking it into the web root lets a sleeping display pull the
# current manifest and versioned binary without rebuilding the container.
mkdir -p /config/InkplateOTA/status
chown -R pi:pi /config/InkplateOTA
chmod 0755 /config/InkplateOTA /config/InkplateOTA/status
ln -sfn /config/InkplateOTA "$EXTRACTED/inkplate-ota"

echo "AvianVisitors: overlay and Inkplate frame endpoints configured"
