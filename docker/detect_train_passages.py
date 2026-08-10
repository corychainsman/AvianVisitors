#!/usr/bin/env python3
"""Confirm realtime rail passages in local audio and publish train detections.

The MBTA and Amtraker collectors remain the source of operator identity and a
tight passage-time window. This service consumes their local JSONL output; it
does not make another transit or BirdNET API request. Audio is retained in a
short rolling buffer and a passage enters birds.db only after the locally
trained YAMNet classifier confirms train-like sound.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from tflite_runtime.interpreter import Interpreter


UTC = dt.timezone.utc
LOCAL = dt.timezone(dt.timedelta(0))
SAMPLE_RATE = 16_000
WINDOW_SAMPLES = 15_600
HOP_SAMPLES = 7_800
ROLLING_RADIUS = 4
FEATURE_COLUMNS = 521 * 3 + 1
BUFFER_MINUTES = 45
BEFORE_SECONDS = 90
AFTER_SECONDS = 105
SETTLE_SECONDS = 15
MODEL_THRESHOLD = 0.72
MIN_HITS = 3
HIT_WINDOW_FRAMES = 20  # 9.75 seconds at YAMNet's 0.4875-second hop.

ENTITIES = {
    "mbta": {
        "science_name": "Machina ferriviaria",
        "common_name": "MBTA Commuter Rail",
        "directory": "MBTA_Commuter_Rail",
    },
    "amtrak": {
        "science_name": "Machina ferriviaria amtrak",
        "common_name": "Amtrak Lake Shore Limited",
        "directory": "Amtrak_Lake_Shore_Limited",
    },
}


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def read_jsonl(path: Path, tail_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with path.open("rb") as source:
        size = source.seek(0, os.SEEK_END)
        start = max(0, size - tail_bytes)
        source.seek(start)
        if start:
            source.readline()  # Discard a partial first record.
        for line in source:
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def train_model(features_path: Path, model_path: Path) -> dict:
    data = np.load(features_path, allow_pickle=False)
    x = np.asarray(data["X"], dtype=np.float32)
    y = np.asarray(data["y"], dtype=np.int8)
    if x.ndim != 2 or x.shape[1] != FEATURE_COLUMNS or len(np.unique(y)) != 2:
        raise RuntimeError(f"invalid training feature matrix: {x.shape}")
    model = HistGradientBoostingClassifier(
        max_iter=180,
        max_leaf_nodes=15,
        learning_rate=0.08,
        l2_regularization=2.0,
        class_weight="balanced",
        random_state=491,
    )
    model.fit(x, y)
    artifact = {
        "schema": 1,
        "feature_columns": FEATURE_COLUMNS,
        "threshold": MODEL_THRESHOLD,
        "minimum_hits": MIN_HITS,
        "model": model,
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = model_path.with_suffix(model_path.suffix + ".tmp")
    joblib.dump(artifact, temporary)
    os.replace(temporary, model_path)
    print(
        f"Train detector: trained local classifier from {len(y)} frames "
        f"({int(y.sum())} train, {int((y == 0).sum())} background)",
        flush=True,
    )
    return artifact


class AudioClassifier:
    def __init__(self, yamnet_path: Path, artifact: dict):
        if artifact.get("schema") != 1 or artifact.get("feature_columns") != FEATURE_COLUMNS:
            raise RuntimeError("unsupported train-classifier artifact")
        self.model = artifact["model"]
        self.threshold = float(artifact.get("threshold", MODEL_THRESHOLD))
        self.minimum_hits = int(artifact.get("minimum_hits", MIN_HITS))
        self.interpreter = Interpreter(model_path=str(yamnet_path), num_threads=2)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]
        if tuple(self.input["shape"]) != (WINDOW_SAMPLES,) or tuple(self.output["shape"]) != (1, 521):
            raise RuntimeError("unexpected YAMNet tensor layout")

    @staticmethod
    def decode(path: Path) -> np.ndarray:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "f32le", "pipe:1",
        ]
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE)
        return np.frombuffer(result.stdout, dtype="<f4")

    def features(self, audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if len(audio) < WINDOW_SAMPLES:
            audio = np.pad(audio, (0, WINDOW_SAMPLES - len(audio)))
        starts = np.arange(0, max(1, len(audio) - WINDOW_SAMPLES + 1), HOP_SAMPLES)
        scores = np.empty((len(starts), 521), dtype=np.float32)
        rms = np.empty(len(starts), dtype=np.float32)
        for row, start in enumerate(starts):
            waveform = audio[start : start + WINDOW_SAMPLES]
            self.interpreter.set_tensor(self.input["index"], waveform)
            self.interpreter.invoke()
            scores[row] = self.interpreter.get_tensor(self.output["index"])[0]
            rms[row] = math.log10(float(np.sqrt(np.mean(waveform * waveform))) + 1e-9)
        rolling_mean = np.empty_like(scores)
        rolling_max = np.empty_like(scores)
        for row in range(len(scores)):
            nearby = scores[max(0, row - ROLLING_RADIUS) : row + ROLLING_RADIUS + 1]
            rolling_mean[row] = nearby.mean(axis=0)
            rolling_max[row] = nearby.max(axis=0)
        return np.column_stack((scores, rolling_mean, rolling_max, rms)), starts

    def classify(self, path: Path) -> dict:
        audio = self.decode(path)
        x, starts = self.features(audio)
        probabilities = self.model.predict_proba(x)[:, 1]
        hits = probabilities >= self.threshold
        best_count = 0
        for row in range(len(hits)):
            best_count = max(best_count, int(hits[row : row + HIT_WINDOW_FRAMES].sum()))
        strongest = int(np.argmax(probabilities))
        center_seconds = (int(starts[strongest]) + WINDOW_SAMPLES / 2) / SAMPLE_RATE
        return {
            "confirmed": best_count >= self.minimum_hits,
            "probability": float(probabilities[strongest]),
            "hits_in_10s": best_count,
            "strongest_seconds": center_seconds,
            "duration_seconds": len(audio) / SAMPLE_RATE,
        }


def segment_start(path: Path) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(path.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def start_buffer(stream_url: str, buffer_dir: Path) -> subprocess.Popen:
    buffer_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(buffer_dir / "%Y%m%dT%H%M%SZ.mp3")
    environment = {**os.environ, "TZ": "UTC"}
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-y",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", stream_url, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000",
        "-c:a", "libmp3lame", "-b:a", "128k", "-f", "segment",
        "-segment_time", "15", "-reset_timestamps", "1", "-strftime", "1", pattern,
    ]
    print(f"Train detector: starting {BUFFER_MINUTES}-minute rolling audio buffer", flush=True)
    return subprocess.Popen(command, env=environment)


def cleanup_buffer(buffer_dir: Path, now: dt.datetime) -> None:
    cutoff = now - dt.timedelta(minutes=BUFFER_MINUTES)
    for path in buffer_dir.glob("*.mp3"):
        started = segment_start(path)
        if started is None or started < cutoff:
            path.unlink(missing_ok=True)


def build_candidate(buffer_dir: Path, at: dt.datetime, destination: Path) -> bool:
    begin = at - dt.timedelta(seconds=BEFORE_SECONDS)
    end = at + dt.timedelta(seconds=AFTER_SECONDS)
    selected = []
    for path in buffer_dir.glob("*.mp3"):
        started = segment_start(path)
        # The segment muxer creates the next filename before it has written a
        # complete MP3. Never hand that open/empty tail file to concat.
        try:
            complete = path.stat().st_size > 1000 and time.time() - path.stat().st_mtime > 2
        except FileNotFoundError:
            complete = False
        if complete and started is not None and started <= end and started + dt.timedelta(seconds=20) >= begin:
            selected.append((started, path))
    selected.sort()
    if len(selected) < 4:
        return False
    concat = destination.with_suffix(".concat.txt")
    concat.write_text("".join(f"file '{path}'\n" for _, path in selected), encoding="utf-8")
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                "-safe", "0", "-i", str(concat), "-map", "0:a:0", "-ac", "1",
                "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "160k", str(destination),
            ],
            check=True,
        )
        return destination.is_file() and destination.stat().st_size > 1000
    finally:
        concat.unlink(missing_ok=True)


def read_location(config_root: Path) -> tuple[float | None, float | None]:
    values = {}
    for raw in (config_root / "birdnet.conf").read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip().strip('"\'')
    try:
        return float(values["LATITUDE"]), float(values["LONGITUDE"])
    except (KeyError, ValueError):
        return None, None


def render_detection_audio(candidate: Path, target: Path, strongest: float) -> None:
    start = max(0.0, strongest - 15.0)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
            "-t", "30", "-i", str(candidate), "-map", "0:a:0", "-ac", "1", "-ar", "48000",
            "-c:a", "libmp3lame", "-b:a", "192k", str(target),
        ],
        check=True,
    )


def render_spectrogram(audio: Path, target: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio),
            "-lavfi", "showspectrumpic=s=1600x420:legend=0:color=plasma:scale=log:fscale=lin",
            "-frames:v", "1", str(target),
        ],
        check=True,
    )


def insert_detection(config_root: Path, operator: str, at: dt.datetime, result: dict) -> str:
    entity = ENTITIES[operator]
    local_at = at.astimezone()
    confidence = max(1, min(99, round(result["probability"] * 100)))
    filename = (
        f'{entity["directory"]}-{confidence}-{local_at:%Y-%m-%d}-'
        f'birdnet-{local_at:%H:%M:%S}.mp3'
    )
    output_dir = config_root / "BirdSongs" / "By_Date" / local_at.strftime("%Y-%m-%d") / entity["directory"]
    db = sqlite3.connect(config_root / "birds.db", timeout=15)
    try:
        db.execute("PRAGMA busy_timeout = 15000")
        exists = db.execute(
            "SELECT 1 FROM detections WHERE Sci_Name=? AND File_Name=? LIMIT 1",
            (entity["science_name"], filename),
        ).fetchone()
        if exists:
            return filename
        candidate = Path(result["candidate_path"])
        audio = output_dir / filename
        output_dir.mkdir(parents=True, exist_ok=True)
        render_detection_audio(candidate, audio, result["strongest_seconds"])
        render_spectrogram(audio, output_dir / f"{filename}.png")
        latitude, longitude = read_location(config_root)
        db.execute(
            """INSERT INTO detections
               (Date,Time,Sci_Name,Com_Name,Confidence,Lat,Lon,Cutoff,Week,Sens,Overlap,File_Name)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                local_at.strftime("%Y-%m-%d"), local_at.strftime("%H:%M:%S"),
                entity["science_name"], entity["common_name"], result["probability"],
                latitude, longitude, 0.0, int(local_at.strftime("%V")), 1.0, 0.0, filename,
            ),
        )
        db.commit()
        return filename
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def collect_events(root: Path, now: dt.datetime) -> dict[str, dict]:
    events = {}
    mbta = root / "mbta-v3-predictions.jsonl"
    for snapshot in read_jsonl(mbta):
        for hint in snapshot.get("passage_hints", []):
            try:
                at = parse_time(hint["estimated_closest_passage"])
                trip = str(hint["trip_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if now - dt.timedelta(hours=2) <= at <= now + dt.timedelta(hours=3):
                event_id = f"mbta:{at:%Y-%m-%d}:{trip}"
                events[event_id] = {"operator": "mbta", "at": at, "evidence": hint}
    amtrak = root / "amtraker-v3-passages.jsonl"
    for passage in read_jsonl(amtrak):
        try:
            at = parse_time(passage["closest_position"]["last_position_at"])
            source_id = str(passage["event_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - dt.timedelta(hours=2) <= at <= now + dt.timedelta(minutes=5):
            event_id = f"amtrak:{source_id}"
            events[event_id] = {"operator": "amtrak", "at": at, "evidence": passage}
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, default=Path("/config"))
    parser.add_argument("--yamnet", type=Path, default=Path("/usr/local/share/avian/yamnet.tflite"))
    parser.add_argument("--stream", default="http://127.0.0.1:8000/stream")
    parser.add_argument("--train-only", action="store_true")
    args = parser.parse_args()
    config_root = args.config_root.resolve()
    work = config_root / "BirdSongs" / "TrainDetector"
    features = config_root / "train-review" / "mbta-yamnet-features.npz"
    model_path = work / "train-classifier.joblib"
    artifact = train_model(features, model_path) if not model_path.is_file() else joblib.load(model_path)
    if args.train_only:
        return 0
    classifier = AudioClassifier(args.yamnet, artifact)
    state_path = work / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"processed": {}}
    processed = state.setdefault("processed", {})
    decisions = work / "decisions.jsonl"
    buffer_dir = work / "buffer"
    candidates_dir = work / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    capture = start_buffer(args.stream, buffer_dir)

    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("Train detector: ready; local passage confirmation is active", flush=True)
    try:
        while not stopping:
            now = dt.datetime.now(UTC)
            if capture.poll() is not None:
                time.sleep(5)
                capture = start_buffer(args.stream, buffer_dir)
            cleanup_buffer(buffer_dir, now)
            for event_id, event in collect_events(
                config_root / "BirdSongs" / "TrainRecordings" / "wellesley-train-study", now
            ).items():
                if event_id in processed or now < event["at"] + dt.timedelta(seconds=AFTER_SECONDS + SETTLE_SECONDS):
                    continue
                safe_id = "".join(char if char.isalnum() else "-" for char in event_id)
                candidate = candidates_dir / f"{safe_id}.mp3"
                if not build_candidate(buffer_dir, event["at"], candidate):
                    # It may be an event discovered after the rolling buffer;
                    # mark only after two hours so a temporarily late segment
                    # can still arrive and be considered.
                    if now > event["at"] + dt.timedelta(hours=2):
                        processed[event_id] = {"decision": "missed-audio", "at": now.isoformat()}
                    continue
                result = classifier.classify(candidate)
                result["candidate_path"] = str(candidate)
                record = {
                    "event_id": event_id,
                    "operator": event["operator"],
                    "passage_at": event["at"].isoformat(),
                    "decided_at": now.isoformat(),
                    **{key: value for key, value in result.items() if key != "candidate_path"},
                }
                if result["confirmed"]:
                    record["file"] = insert_detection(config_root, event["operator"], event["at"], result)
                    print(
                        f'Train detector: confirmed {event["operator"].upper()} at '
                        f'{event["at"].isoformat()} (p={result["probability"]:.3f})',
                        flush=True,
                    )
                else:
                    candidate.unlink(missing_ok=True)
                    print(
                        f'Train detector: rejected {event["operator"].upper()} passage candidate '
                        f'(p={result["probability"]:.3f}, hits={result["hits_in_10s"]})',
                        flush=True,
                    )
                append_jsonl(decisions, record)
                processed[event_id] = record
                cutoff = now - dt.timedelta(days=14)
                processed = {
                    key: value for key, value in processed.items()
                    if parse_time(value.get("decided_at", value.get("at", now.isoformat()))) >= cutoff
                }
                state["processed"] = processed
                atomic_json(state_path, state)
            time.sleep(5)
    finally:
        capture.terminate()
        try:
            capture.wait(timeout=5)
        except subprocess.TimeoutExpired:
            capture.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
