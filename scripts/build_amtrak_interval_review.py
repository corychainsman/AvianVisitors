#!/usr/bin/env python3
"""Add archived Amtrak passage clips to the shared train brush review set."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path


UTC = dt.timezone.utc
SEGMENT = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.mp3$")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, default=Path("runtime/config"))
    parser.add_argument("--session-id", default="wellesley-24h-20260806")
    parser.add_argument("--regenerate-images", action="store_true")
    args = parser.parse_args()
    config_root = args.config_root.resolve()
    session = config_root / "BirdSongs" / "TrainRecordings" / args.session_id
    raw = session / "raw"
    review = session / "review"
    review.mkdir(parents=True, exist_ok=True)

    history_path = config_root / "BirdSongs" / "TrainRecordings" / "historical-backfill" / "train-history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    events = [event for event in history["events"] if event.get("operator") == "Amtrak"]

    segments: dict[dt.datetime, Path] = {}
    for path in raw.glob("*.mp3"):
        match = SEGMENT.fullmatch(path.name)
        if match and path.stat().st_size > 100_000:
            stamp = dt.datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
            segments[stamp] = path

    manifest = config_root / "train-review" / "candidates.json"
    existing = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
    retained = []
    for candidate in existing:
        if candidate.get("operator") == "Amtrak" or str(candidate.get("id", "")).startswith("amtrak-"):
            continue
        retained.append({"operator": "MBTA", "species": "Machina ferriviaria", **candidate})

    candidates = []
    for event in events:
        passage = dt.datetime.fromisoformat(event["passage_at"]).astimezone(UTC)
        clip_start = passage - dt.timedelta(seconds=60)
        first_minute = clip_start.replace(second=0, microsecond=0)
        source_segments = [
            segments[first_minute + dt.timedelta(minutes=minute)]
            for minute in range(4)
            if first_minute + dt.timedelta(minutes=minute) in segments
        ]
        if len(source_segments) < 3:
            continue

        train = str(event["train"])
        event_id = f"amtrak-{passage:%Y%m%d-%H%M%S}-train-{train}"
        audio = review / f"{event_id}.mp3"
        spectrogram = review / f"{event_id}.png"
        concat_file = review / f".{event_id}.concat.txt"
        concat_file.write_text(
            "".join(f"file '{path}'\n" for path in source_segments), encoding="utf-8"
        )
        offset = (clip_start - first_minute).total_seconds()
        if not audio.is_file():
            run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-ss", f"{offset:.3f}", "-t", "120", "-vn", "-ac", "1", "-ar", "48000",
                "-c:a", "libmp3lame", "-b:a", "160k", str(audio),
            ])
        if not spectrogram.is_file() or args.regenerate_images:
            run([
                "sox", str(audio), "-n", "spectrogram",
                "-r", "-m", "-x", "1200", "-y", "257", "-z", "90", "-o", str(spectrogram),
            ])
        concat_file.unlink(missing_ok=True)

        window_start = dt.datetime.fromisoformat(event["passage_window_start"])
        window_end = dt.datetime.fromisoformat(event["passage_window_end"])
        evidence = event["evidence"].replace("_", " ")
        candidates.append({
            "id": event_id,
            "operator": "Amtrak",
            "species": "Machina ferriviaria amtrak",
            "recorded_at": dt.datetime.fromisoformat(event["passage_at"]).isoformat(),
            "passage_window": f"{window_start:%H:%M:%S}–{window_end:%H:%M:%S}",
            "context": (
                f"Lake Shore Limited train {train}, {event['direction']}. "
                f"Closest approach is expected near 1:00; source evidence: {evidence}."
            ),
            "audio": f"/train-audio/{session.name}/review/{audio.name}",
            "spectrogram": f"/train-audio/{session.name}/review/{spectrogram.name}?v=plasma-1",
        })

    combined = sorted(retained + candidates, key=lambda item: item["recorded_at"], reverse=True)
    atomic_json(manifest, combined)
    print(f"added {len(candidates)} Amtrak review clips; preserved {len(retained)} MBTA clips")
    print(f"manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
