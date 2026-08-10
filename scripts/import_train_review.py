#!/usr/bin/env python3
"""Import precisely annotated train-review clips as BirdNET-style detections.

The importer is deliberately idempotent: a synthetic detection is uniquely
identified by its generated filename, so rerunning it only fills in anything
that is missing. It leaves the review audio and labels untouched.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse


ENTITIES = {
    "Machina ferriviaria": {
        "common_name": "MBTA Commuter Rail",
        "species_dir": "MBTA_Commuter_Rail",
    },
    "Machina ferriviaria amtrak": {
        "common_name": "Amtrak Lake Shore Limited",
        "species_dir": "Amtrak_Lake_Shore_Limited",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-root",
        type=Path,
        default=Path("runtime/config"),
        help="BirdNET config volume (default: runtime/config)",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_location(config_root: Path) -> tuple[float | None, float | None]:
    conf = config_root / "birdnet.conf"
    if not conf.is_file():
        return None, None
    values: dict[str, str] = {}
    for raw in conf.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"\'')
    try:
        lat = float(values["LATITUDE"])
        lon = float(values["LONGITUDE"])
        return lat, lon
    except (KeyError, ValueError):
        return None, None


def source_path(config_root: Path, candidate: dict) -> Path:
    path = urlparse(candidate["audio"]).path
    marker = "/train-audio/"
    if not path.startswith(marker):
        raise ValueError(f"unexpected review audio URL: {path}")
    relative = Path(path[len(marker) :])
    resolved = config_root / "BirdSongs" / "TrainRecordings" / relative
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def event_time(candidate: dict, start: float, end: float) -> datetime:
    # Review clips begin one minute before the expected closest passage.
    expected = datetime.fromisoformat(candidate["recorded_at"])
    return expected + timedelta(seconds=((start + end) / 2.0) - 60.0)


def output_name(at: datetime, species_dir: str) -> str:
    return f"{species_dir}-100-{at:%Y-%m-%d}-birdnet-{at:%H:%M:%S}.mp3"


def render_audio(source: Path, target: Path, start: float, end: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".train-", suffix=".mp3", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source),
                "-map", "0:a:0", "-ac", "1", "-ar", "48000",
                "-codec:a", "libmp3lame", "-b:a", "192k", str(temporary),
            ],
            check=True,
        )
        if temporary.stat().st_size < 64:
            raise RuntimeError(f"ffmpeg produced an empty clip for {source}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def render_spectrogram(audio: Path, target: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".train-spectrum-", suffix=".png", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(audio),
                "-lavfi", "showspectrumpic=s=1600x420:legend=0:color=plasma:scale=log:fscale=lin",
                "-frames:v", "1", str(temporary),
            ],
            check=True,
        )
        if temporary.stat().st_size < 64:
            raise RuntimeError(f"ffmpeg produced an empty spectrogram for {audio}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    config_root = args.config_root.resolve()
    review_root = config_root / "train-review"
    candidates = {item["id"]: item for item in load_json(review_root / "candidates.json")}
    labels = load_json(review_root / "labels.json")
    selected = []
    for candidate_id, label in labels.items():
        if label.get("label") != "train":
            continue
        start = label.get("start_seconds")
        end = label.get("end_seconds")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
            continue
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"label has no candidate: {candidate_id}")
        science_name = candidate.get("species", "Machina ferriviaria")
        entity = ENTITIES.get(science_name)
        if entity is None:
            raise ValueError(f"unsupported train-review species: {science_name}")
        selected.append((candidate_id, candidate, float(start), float(end), science_name, entity))

    selected.sort(key=lambda row: row[1]["recorded_at"])
    selected_counts = {
        science_name: sum(row[4] == science_name for row in selected)
        for science_name in ENTITIES
    }
    print(
        f"selected {len(selected)} precise train annotations "
        + " ".join(f"{name}={count}" for name, count in selected_counts.items())
    )
    lat, lon = read_location(config_root)
    db_path = config_root / "birds.db"
    db = None if args.dry_run else sqlite3.connect(db_path, timeout=10)
    if db is not None:
        db.execute("PRAGMA busy_timeout = 10000")

    imported = skipped = 0
    try:
        for candidate_id, candidate, start, end, science_name, entity in selected:
            at = event_time(candidate, start, end)
            filename = output_name(at, entity["species_dir"])
            target_dir = config_root / "BirdSongs" / "By_Date" / at.strftime("%Y-%m-%d") / entity["species_dir"]
            audio_target = target_dir / filename
            spectrum_target = target_dir / f"{filename}.png"
            source = source_path(config_root, candidate)
            if args.dry_run:
                print(f"PLAN {at.isoformat()} {start:.3f}-{end:.3f}s {candidate_id} -> {filename}")
                continue

            exists = db.execute(
                "SELECT 1 FROM detections WHERE Sci_Name = ? AND File_Name = ? LIMIT 1",
                (science_name, filename),
            ).fetchone()
            if exists:
                skipped += 1
                continue

            if not audio_target.is_file():
                render_audio(source, audio_target, start, end)
            if not spectrum_target.is_file():
                render_spectrogram(audio_target, spectrum_target)

            db.execute(
                """INSERT INTO detections
                   (Date, Time, Sci_Name, Com_Name, Confidence, Lat, Lon,
                    Cutoff, Week, Sens, Overlap, File_Name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    at.strftime("%Y-%m-%d"), at.strftime("%H:%M:%S"), science_name,
                    entity["common_name"], 1.0, lat, lon, 0.0, int(at.strftime("%V")),
                    1.0, 0.0, filename,
                ),
            )
            imported += 1
        if db is not None:
            db.commit()
    except Exception:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()

    if not args.dry_run:
        print(f"imported {imported}; already present {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
