#!/usr/bin/env bash
# BirdNET-Pi live stream with sample-count-derived timestamps.
#
# Some ALSA devices occasionally report timestamps that move backwards.
# Passing those through to the MP3 muxer produces non-monotonic DTS packets,
# which Chromium may tolerate but Safari and other media stacks can reject.
# This stream is audio-only, so derive PTS from the number of captured samples.

set -euo pipefail

# shellcheck disable=SC1091
source /etc/birdnet/birdnet.conf

logging_level="${LogLevel_LiveAudioStreamService:-error}"
channels="${CHANNELS:-1}"
filters='asetpts=N/SR/TB'

if [[ "${ACTIVATE_FREQSHIFT_IN_LIVESTREAM:-false}" == "true" ]]; then
  filters+=",rubberband=pitch=${FREQSHIFT_LO}/${FREQSHIFT_HI}"
fi

input_args=()
if [[ -n "${RTSP_STREAM:-}" ]]; then
  IFS=',' read -r -a rtsp_streams <<< "${RTSP_STREAM}"
  stream_index="${RTSP_STREAM_TO_LIVESTREAM:-0}"
  [[ "${stream_index}" =~ ^[0-9]+$ ]] || stream_index=0
  selected_stream="${rtsp_streams[stream_index]:-${rtsp_streams[0]:-}}"
  if [[ -z "${selected_stream}" ]]; then
    echo 'AvianVisitors livestream: no RTSP stream configured' >&2
    exit 1
  fi
  input_args=(-i "${selected_stream}")
elif [[ -n "${REC_CARD:-}" ]]; then
  input_args=(-ac "${channels}" -ar 48000 -f alsa -i "${REC_CARD}")
else
  echo 'AvianVisitors livestream: REC_CARD is not configured' >&2
  exit 1
fi

exec ffmpeg \
  -hide_banner \
  -nostdin \
  -loglevel "${logging_level}" \
  "${input_args[@]}" \
  -vn \
  -map 0:a:0 \
  -af "${filters}" \
  -c:a libmp3lame \
  -b:a 320k \
  -ar 48000 \
  -ac "${channels}" \
  -content_type audio/mpeg \
  -flush_packets 1 \
  -f mp3 \
  "icecast://source:${ICE_PWD}@localhost:8000/stream"
