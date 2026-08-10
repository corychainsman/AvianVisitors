#!/usr/bin/env python3
"""Build review clips around observed Wellesley commuter-rail passages."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


LOCAL = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
API = "https://dashboard-api.labs.transitmatters.org/api/traveltimes/{date}"
SEGMENT = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.mp3$")
DIRECTIONS = {
    1: {
        "label": "Wellesley Square → Wellesley Hills",
        "from_stop": ["CR-Worcester_1_WML-0147-01", "CR-Worcester_1_WML-0147-02"],
        "to_stop": ["CR-Worcester_1_WML-0135-01", "CR-Worcester_1_WML-0135-02"],
    },
    0: {
        "label": "Wellesley Hills → Wellesley Square",
        "from_stop": ["CR-Worcester_0_WML-0135-01", "CR-Worcester_0_WML-0135-02"],
        "to_stop": ["CR-Worcester_0_WML-0147-01", "CR-Worcester_0_WML-0147-02"],
    },
}


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch_events(session: Path, day: dt.date, direction: int, refresh: bool) -> list[dict]:
    cache = session / f"transitmatters-{day.isoformat()}-direction-{direction}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    config = DIRECTIONS[direction]
    query = urllib.parse.urlencode(
        {"from_stop": config["from_stop"], "to_stop": config["to_stop"]}, doseq=True
    )
    request = urllib.request.Request(
        f"{API.format(date=day.isoformat())}?{query}",
        headers={"User-Agent": "AvianVisitors train-review/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        events = json.load(response)
    atomic_json(cache, events)
    time.sleep(1)
    return events


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--regenerate-images", action="store_true")
    args = parser.parse_args()
    session = args.session.resolve()
    raw = session / "raw"
    review = session / "review"
    review.mkdir(parents=True, exist_ok=True)

    status = json.loads((session / "status.json").read_text(encoding="utf-8"))
    started = dt.datetime.fromisoformat(status["started_at"]).astimezone(UTC)
    now = dt.datetime.now(UTC)
    segments: dict[dt.datetime, Path] = {}
    for path in raw.glob("*.mp3"):
        match = SEGMENT.fullmatch(path.name)
        if match and path.stat().st_size > 100_000:
            stamp = dt.datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
            segments[stamp] = path
    if not segments:
        raise RuntimeError("no completed raw recording segments")
    latest_segment = max(segments)

    first_day = started.astimezone(LOCAL).date()
    last_day = now.astimezone(LOCAL).date()
    days = [first_day + dt.timedelta(days=n) for n in range((last_day - first_day).days + 1)]
    events: list[dict] = []
    for day in days:
        for direction in (0, 1):
            for event in fetch_events(session, day, direction, args.refresh):
                events.append({**event, "direction": direction})

    candidates = []
    seen = set()
    for event in events:
        departure = dt.datetime.fromisoformat(event["dep_dt"]).replace(tzinfo=LOCAL)
        arrival = dt.datetime.fromisoformat(event["arr_dt"]).replace(tzinfo=LOCAL)
        midpoint = departure + (arrival - departure) / 2
        clip_start = midpoint.astimezone(UTC) - dt.timedelta(seconds=60)
        clip_end = clip_start + dt.timedelta(seconds=120)
        if clip_start < started or clip_end > latest_segment + dt.timedelta(seconds=60):
            continue
        event_id = f"actual-{departure:%Y%m%d-%H%M%S}-d{event['direction']}-{event.get('vehicle_label', 'unknown')}"
        if event_id in seen:
            continue
        seen.add(event_id)

        first_minute = clip_start.replace(second=0, microsecond=0)
        source_segments = []
        for minute in range(4):
            path = segments.get(first_minute + dt.timedelta(minutes=minute))
            if path is not None:
                source_segments.append(path)
        if len(source_segments) < 3:
            continue

        audio = review / f"{event_id}.mp3"
        spectrogram = review / f"{event_id}.png"
        concat_file = review / f".{event_id}.concat.txt"
        concat_file.write_text(
            "".join(f"file '{path}'\n" for path in source_segments), encoding="utf-8"
        )
        offset = (clip_start - first_minute).total_seconds()
        if not audio.exists():
            run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-ss", f"{offset:.3f}", "-t", "120", "-vn", "-ac", "1", "-ar", "48000",
                "-c:a", "libmp3lame", "-b:a", "160k", str(audio),
            ])
        if not spectrogram.exists() or args.regenerate_images:
            run([
                "sox", str(audio), "-n", "spectrogram",
                "-r", "-m", "-x", "1200", "-y", "257", "-z", "90",
                "-o", str(spectrogram),
            ])
        concat_file.unlink(missing_ok=True)

        direction = DIRECTIONS[event["direction"]]["label"]
        vehicle = event.get("vehicle_label") or event.get("vehicle_consist") or "unknown"
        candidates.append({
            "id": event_id,
            "recorded_at": midpoint.isoformat(),
            "passage_window": f"{departure:%H:%M:%S}–{arrival:%H:%M:%S}",
            "context": f"Observed {direction}, train {vehicle}. Expected closest approach is near 1:00.",
            "audio": f"/train-audio/{session.name}/review/{audio.name}",
            "spectrogram": f"/train-audio/{session.name}/review/{spectrogram.name}?v=plasma-1",
        })

    candidates.sort(key=lambda item: item["recorded_at"], reverse=True)
    config_root = session.parents[2]
    manifest = config_root / "train-review" / "candidates.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(manifest, candidates)
    print(f"built {len(candidates)} review clips; manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
