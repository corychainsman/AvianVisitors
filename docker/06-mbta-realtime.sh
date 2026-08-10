#!/usr/bin/env bash
set -Eeuo pipefail

# The persistent fallback lets the already-running capture container recover
# after a host reboot. Compose normally injects the same value from .env.
if [[ -r /config/secrets/mbta.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source /config/secrets/mbta.env
    set +a
fi

export MBTA_REALTIME_ENABLED="${MBTA_REALTIME_ENABLED:-true}"
export MBTA_REALTIME_POLL_SECONDS="${MBTA_REALTIME_POLL_SECONDS:-60}"
exec /usr/local/bin/record_mbta_realtime.py
