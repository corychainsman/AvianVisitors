#!/usr/bin/env bash
set -Eeuo pipefail

export AMTRAK_REALTIME_ENABLED="${AMTRAK_REALTIME_ENABLED:-true}"
export AMTRAK_REALTIME_POLL_SECONDS="${AMTRAK_REALTIME_POLL_SECONDS:-60}"
exec /usr/local/bin/record_amtrak_realtime.py
