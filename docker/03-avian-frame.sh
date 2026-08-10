#!/usr/bin/env bash
# Render the live browser collage into a stable JPEG for the Inkplate.
set -u

python=/home/pi/BirdNET-Pi/birdnet/bin/python
shoot=/home/pi/BirdNET-Pi/frame/shoot.py
publish=/usr/local/bin/publish_avian_frame.py
base_url="${AVIAN_FRAME_URL:-http://127.0.0.1:8081}"
interval="${AVIAN_FRAME_INTERVAL_SECONDS:-900}"
out_dir=/config/frame
train_source_png=/tmp/avian-frame-trains.png
birds_source_png=/tmp/avian-frame-birds.png

mkdir -p "$out_dir"

until curl -fsS --max-time 10 \
    "$base_url/avian/api/birdnet-api.php?action=recent&hours=24" >/dev/null 2>&1; do
    echo "AvianVisitors frame: waiting for the web API"
    sleep 10
done

while true; do
    rm -f "$train_source_png" "$birds_source_png"
    if PLAYWRIGHT_BROWSERS_PATH=/ms-playwright "$python" "$shoot" \
        --url "$base_url/collage?trains=1" \
        --out "$train_source_png" \
        --title "Avian Visitors" \
        --subtitle "Heard Today" \
        --window-hours 24 \
        --mat 0 \
        --collage-vh 100 \
        --packing-scale 1.9 \
        --packing-cap 0.62 \
        --timeout 180000; then
        if "$python" "$publish" \
            --input "$train_source_png" \
            --output "$out_dir/frame.jpg" \
            --signature "$out_dir/frame.sha256"; then
            if PLAYWRIGHT_BROWSERS_PATH=/ms-playwright "$python" "$shoot" \
                --url "$base_url/collage?trains=0" \
                --out "$birds_source_png" \
                --title "Avian Visitors" \
                --subtitle "Heard Today" \
                --window-hours 24 \
                --mat 0 \
                --collage-vh 100 \
                --packing-scale 1.9 \
                --packing-cap 0.62 \
                --timeout 180000; then
                if "$python" "$publish" \
                    --input "$birds_source_png" \
                    --output "$out_dir/frame-birds.jpg" \
                    --signature "$out_dir/frame-birds.sha256"; then
                    echo "AvianVisitors frame: train and bird-only renders complete"
                    sleep "$interval"
                    continue
                fi
            fi
        fi
    fi

    echo "AvianVisitors frame: render failed; retrying in 60 seconds" >&2
    sleep 60
done
