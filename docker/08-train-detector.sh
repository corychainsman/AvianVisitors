#!/usr/bin/env bash
# Confirm MBTA/Amtrak realtime passage candidates against the live microphone.
set -euo pipefail

# Custom s6 services start as root.  The detector writes synthetic detections
# into the same date/species tree as BirdNET, so it must use BirdNET's uid;
# otherwise whichever process creates a new date first can lock the other out.
exec s6-setuidgid abc \
    /home/pi/BirdNET-Pi/birdnet/bin/python \
    /usr/local/bin/detect_train_passages.py
