#!/command/with-contenv bash
# Capture a bounded, restart-resilient microphone session for train research.
set -Eeuo pipefail

exec /usr/local/bin/record_train_audio.sh
