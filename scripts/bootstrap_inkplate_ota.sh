#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sketch="$root/inkplate/AvianVisitorsInkplate"
build_dir="$root/build/inkplate"
fqbn='soldered-inkplate-boards:esp32:Inkplate13SPECTRA:PartitionScheme=app13M_data7M_32MB'
port="${1:-}"

if [[ -z "$port" ]]; then
  mapfile -t ports < <(
    arduino-cli board list | awk 'NR > 1 && $1 ~ /^\/dev\/tty(ACM|USB)/ { print $1 }'
  )
  if (( ${#ports[@]} != 1 )); then
    echo "Expected exactly one USB serial device; found ${#ports[@]}." >&2
    echo "Pass the Inkplate port explicitly, for example: $0 /dev/ttyACM0" >&2
    exit 1
  fi
  port="${ports[0]}"
fi

if [[ ! -e "$port" ]]; then
  echo "Serial port does not exist: $port" >&2
  exit 1
fi

mkdir -p "$build_dir"
arduino-cli compile \
  --fqbn "$fqbn" \
  --output-dir "$build_dir" \
  "$sketch"
arduino-cli upload \
  --fqbn "$fqbn" \
  --port "$port" \
  --input-dir "$build_dir" \
  --verify \
  "$sketch"

echo "Inkplate OTA bootstrap installed and verified on $port"
