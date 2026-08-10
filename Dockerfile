FROM ghcr.io/alexbelgium/birdnet-pi-amd64:latest

USER root

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN /home/pi/BirdNET-Pi/birdnet/bin/pip install --no-cache-dir \
      "playwright>=1.40" \
    && /home/pi/BirdNET-Pi/birdnet/bin/python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY --chown=pi:pi avian /home/pi/BirdNET-Pi/avian
COPY --chown=pi:pi frame /home/pi/BirdNET-Pi/frame
COPY --chown=pi:pi scripts/link_webroot.sh /home/pi/BirdNET-Pi/scripts/link_webroot.sh
COPY --chown=pi:pi scripts/update_caddyfile.sh /home/pi/BirdNET-Pi/scripts/update_caddyfile.sh
COPY docker/90-avian-overlay.sh /etc/cont-init.d/90-avian-overlay.sh
COPY docker/asound.conf /etc/asound.conf
COPY docker/03-avian-frame.sh /custom-services.d/03-avian-frame.sh
COPY docker/04-avian-storage-cap.sh /custom-services.d/04-avian-storage-cap.sh
COPY docker/05-train-recorder.sh /custom-services.d/05-train-recorder.sh
COPY docker/06-mbta-realtime.sh /custom-services.d/06-mbta-realtime.sh
COPY docker/07-amtrak-realtime.sh /custom-services.d/07-amtrak-realtime.sh
COPY docker/08-train-detector.sh /custom-services.d/08-train-detector.sh
COPY --chown=pi:pi docker/livestream.sh /home/pi/BirdNET-Pi/scripts/livestream.sh
COPY docker/publish_frame.py /usr/local/bin/publish_avian_frame.py
COPY docker/enforce_avian_storage_cap.py /usr/local/bin/enforce_avian_storage_cap.py
COPY docker/record_train_audio.sh /usr/local/bin/record_train_audio.sh
COPY docker/record_mbta_realtime.py /usr/local/bin/record_mbta_realtime.py
COPY docker/record_amtrak_realtime.py /usr/local/bin/record_amtrak_realtime.py
COPY docker/detect_train_passages.py /usr/local/bin/detect_train_passages.py
COPY scripts/build_train_interval_review.py /usr/local/bin/build_train_interval_review.py
COPY scripts/backfill_train_history.py /usr/local/bin/backfill_train_history.py
COPY scripts/build_amtrak_interval_review.py /usr/local/bin/build_amtrak_interval_review.py
COPY scripts/import_train_review.py /usr/local/bin/import_train_review.py

# Official MediaPipe YAMNet (AudioSet, float32). Pin and verify the model so a
# future upstream replacement cannot silently change the local classifier.
RUN mkdir -p /usr/local/share/avian \
    && curl -fsSL \
      https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite \
      -o /usr/local/share/avian/yamnet.tflite \
    && echo '4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf  /usr/local/share/avian/yamnet.tflite' \
      | sha256sum -c -

RUN chmod 0755 \
      /etc/cont-init.d/90-avian-overlay.sh \
      /custom-services.d/03-avian-frame.sh \
      /custom-services.d/04-avian-storage-cap.sh \
      /custom-services.d/05-train-recorder.sh \
      /custom-services.d/06-mbta-realtime.sh \
      /custom-services.d/07-amtrak-realtime.sh \
      /custom-services.d/08-train-detector.sh \
      /home/pi/BirdNET-Pi/scripts/livestream.sh \
      /usr/local/bin/publish_avian_frame.py \
      /usr/local/bin/enforce_avian_storage_cap.py \
      /usr/local/bin/record_train_audio.sh \
      /usr/local/bin/record_mbta_realtime.py \
      /usr/local/bin/record_amtrak_realtime.py \
      /usr/local/bin/detect_train_passages.py \
      /usr/local/bin/build_train_interval_review.py \
      /usr/local/bin/backfill_train_history.py \
      /usr/local/bin/build_amtrak_interval_review.py \
      /usr/local/bin/import_train_review.py \
      /home/pi/BirdNET-Pi/scripts/link_webroot.sh \
      /home/pi/BirdNET-Pi/scripts/update_caddyfile.sh \
    && chmod 0644 /etc/asound.conf
