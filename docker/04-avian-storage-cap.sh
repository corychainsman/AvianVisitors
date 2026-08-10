#!/usr/bin/env bash
# Enforce the persistent AvianVisitors data ceiling independently of host df(1).
set -Eeuo pipefail

export PYTHONUNBUFFERED=1
exec /home/pi/BirdNET-Pi/birdnet/bin/python \
    /usr/local/bin/enforce_avian_storage_cap.py
