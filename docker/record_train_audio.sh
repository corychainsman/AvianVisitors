#!/usr/bin/env bash
set -Eeuo pipefail

enabled="${TRAIN_RECORD_ENABLED:-false}"
if [[ "$enabled" != "true" ]]; then
    echo "Train recorder: disabled"
    exec sleep infinity
fi

session_id="${TRAIN_RECORD_SESSION_ID:-wellesley-train-study}"
if [[ ! "$session_id" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "Train recorder: unsafe session id: $session_id" >&2
    exit 1
fi

duration_seconds="${TRAIN_RECORD_DURATION_SECONDS:-86400}"
if [[ ! "$duration_seconds" =~ ^[0-9]+$ ]] || (( duration_seconds < 60 )); then
    echo "Train recorder: duration must be an integer of at least 60 seconds" >&2
    exit 1
fi

bitrate="${TRAIN_RECORD_BITRATE:-192k}"
root="${TRAIN_RECORD_ROOT:-/config/BirdSongs/TrainRecordings}"
session_dir="$root/$session_id"
raw_dir="$session_dir/raw"
deadline_file="$session_dir/deadline_epoch"
status_file="$session_dir/status.json"
log_file="$session_dir/recorder.log"

mkdir -p "$raw_dir"

write_status() {
    local state="$1"
    local now_epoch="$2"
    local remaining="$3"
    local temporary="$status_file.tmp"

    jq -n \
        --arg state "$state" \
        --arg session_id "$session_id" \
        --arg started_at "$(date --date="@$started_epoch" --iso-8601=seconds)" \
        --arg deadline "$(date --date="@$deadline_epoch" --iso-8601=seconds)" \
        --arg updated_at "$(date --date="@$now_epoch" --iso-8601=seconds)" \
        --argjson remaining_seconds "$remaining" \
        '{state:$state, session_id:$session_id, started_at:$started_at,
          deadline:$deadline, updated_at:$updated_at,
          remaining_seconds:$remaining_seconds,
          format:"MP3, mono, 48 kHz, 192 kbps", segment_seconds:60}' \
        > "$temporary"
    mv "$temporary" "$status_file"
}

if [[ -s "$deadline_file" ]]; then
    IFS=' ' read -r started_epoch deadline_epoch < "$deadline_file"
else
    started_epoch="$(date +%s)"
    deadline_epoch="$((started_epoch + duration_seconds))"
    temporary="$deadline_file.tmp"
    printf '%s %s\n' "$started_epoch" "$deadline_epoch" > "$temporary"
    mv "$temporary" "$deadline_file"
fi

if [[ ! "$started_epoch" =~ ^[0-9]+$ || ! "$deadline_epoch" =~ ^[0-9]+$ ]]; then
    echo "Train recorder: invalid persisted deadline" >&2
    exit 1
fi

echo "Train recorder: session $session_id ends $(date --date="@$deadline_epoch" --iso-8601=seconds)"

while true; do
    now_epoch="$(date +%s)"
    remaining="$((deadline_epoch - now_epoch))"
    if (( remaining <= 0 )); then
        write_status "complete" "$now_epoch" 0
        echo "Train recorder: 24-hour capture complete"
        break
    fi

    write_status "recording" "$now_epoch" "$remaining"
    echo "$(date --iso-8601=seconds) starting capture with $remaining seconds remaining" >> "$log_file"

    set +e
    timeout --signal=INT --kill-after=20 "${remaining}s" \
        ffmpeg -hide_banner -nostdin -loglevel warning \
        -f alsa -i "${ALSA_CARD:-avian_capture}" \
        -vn -map 0:a:0 -af 'asetpts=N/SR/TB' \
        -ar 48000 -ac 1 -c:a libmp3lame -b:a "$bitrate" \
        -f segment -segment_time 60 -segment_atclocktime 1 \
        -reset_timestamps 1 -strftime 1 \
        "$raw_dir/%Y-%m-%d_%H-%M-%S.mp3" \
        >> "$log_file" 2>&1
    capture_status=$?
    set -e

    now_epoch="$(date +%s)"
    remaining="$((deadline_epoch - now_epoch))"
    if (( remaining <= 0 )); then
        write_status "complete" "$now_epoch" 0
        echo "Train recorder: 24-hour capture complete"
        break
    fi

    echo "$(date --iso-8601=seconds) capture exited $capture_status; retrying" >> "$log_file"
    write_status "retrying" "$now_epoch" "$remaining"
    sleep 5
done

# Keep the supervised service healthy without starting a new recording. The
# persisted deadline prevents restarts from extending the requested session.
exec sleep infinity
