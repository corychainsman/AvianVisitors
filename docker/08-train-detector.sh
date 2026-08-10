#!/usr/bin/env bash
# Confirm MBTA/Amtrak realtime passage candidates against the live microphone.
set -euo pipefail

exec /home/pi/BirdNET-Pi/birdnet/bin/python /usr/local/bin/detect_train_passages.py
