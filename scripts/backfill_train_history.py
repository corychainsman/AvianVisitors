#!/usr/bin/env python3
"""Backfill operational train passages during the BirdNET observation period.

This writes a provenance-rich ledger, not BirdNET acoustic detections.  The
ledger can be used to locate audio and to distinguish MBTA from Amtrak after a
train sound has been confirmed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


LOCAL = ZoneInfo("America/New_York")
TRANSITMATTERS_API = "https://dashboard-api.labs.transitmatters.org/api/traveltimes/{date}"
TRANSITDOCS_API = "https://asm-backend.transitdocs.com/train/{year}/{month}/{day}/Amtrak/{train}"
MBTA_DIRECTIONS = {
    0: {
        "from": ["CR-Worcester_0_WML-0135-01", "CR-Worcester_0_WML-0135-02"],
        "to": ["CR-Worcester_0_WML-0147-01", "CR-Worcester_0_WML-0147-02"],
        "label": "Wellesley Hills to Wellesley Square",
    },
    1: {
        "from": ["CR-Worcester_1_WML-0147-01", "CR-Worcester_1_WML-0147-02"],
        "to": ["CR-Worcester_1_WML-0135-01", "CR-Worcester_1_WML-0135-02"],
        "label": "Wellesley Square to Wellesley Hills",
    },
}
# Amtrak's archive places Back Bay and Framingham at route miles 1 and 21.
# The microphone is approximately midway between Wellesley Hills and
# Wellesley Square, around route mile 13.25.
AMTRAK_MIC_MILE = 13.25
ENTITIES = {
    "Machina ferriviaria": "MBTA_Commuter_Rail",
    "Machina ferriviaria amtrak": "Amtrak_Lake_Shore_Limited",
}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch_json(url: str, cache: Path, refresh: bool) -> object:
    if cache.is_file() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "AvianVisitors train-history backfill/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    atomic_json(cache, value)
    time.sleep(1)
    return value


def database_bounds(db_path: Path) -> tuple[dt.datetime, dt.datetime]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        first, last = db.execute(
            "SELECT MIN(Date || 'T' || Time), MAX(Date || 'T' || Time) FROM detections"
        ).fetchone()
    if not first or not last:
        raise RuntimeError("birds.db has no detections")
    return dt.datetime.fromisoformat(first).replace(tzinfo=LOCAL), dt.datetime.fromisoformat(last).replace(tzinfo=LOCAL)


def mark_existing_audio_confirmations(db_path: Path, events: list[dict]) -> None:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        detections = db.execute(
            """SELECT Date, Time, Sci_Name, File_Name FROM detections
               WHERE Sci_Name IN ('Machina ferriviaria', 'Machina ferriviaria amtrak')
                 AND File_Name NOT LIKE '%-no-audio-%'"""
        ).fetchall()
    unused = [
        {
            "at": dt.datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=LOCAL),
            "species": species,
            "file": filename,
        }
        for date, clock, species, filename in detections
    ]
    for event in events:
        passage = dt.datetime.fromisoformat(event["passage_at"])
        matches = [
            (abs((passage - row["at"]).total_seconds()), index, row)
            for index, row in enumerate(unused)
            if row["species"] == event["species"]
        ]
        if not matches:
            continue
        delta, index, row = min(matches, key=lambda match: match[0])
        if delta <= 90:
            event["acoustically_confirmed"] = True
            event["detection_file"] = row["file"]
            event["detection_offset_seconds"] = round(delta, 3)
            unused.pop(index)


def read_location(config_root: Path) -> tuple[float | None, float | None]:
    values: dict[str, str] = {}
    path = config_root / "birdnet.conf"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    try:
        return float(values["LATITUDE"]), float(values["LONGITUDE"])
    except (KeyError, ValueError):
        return None, None


def no_audio_filename(event: dict) -> str:
    passage = dt.datetime.fromisoformat(event["passage_at"])
    digest = hashlib.sha256(event["event_id"].encode("utf-8")).hexdigest()[:8]
    species_dir = ENTITIES[event["species"]]
    return f"{species_dir}-transit-{passage:%Y-%m-%d}-birdnet-{passage:%H:%M:%S}-no-audio-{digest}.mp3"


def import_no_audio_events(config_root: Path, events: list[dict]) -> tuple[int, int]:
    """Insert operational passages without pretending that audio exists.

    The deterministic sentinel filename makes the import idempotent and lets
    the web UI suppress playback controls for these rows.
    """
    lat, lon = read_location(config_root)
    imported = skipped = 0
    with sqlite3.connect(config_root / "birds.db", timeout=10) as db:
        db.execute("PRAGMA busy_timeout = 10000")
        try:
            for event in events:
                if event.get("acoustically_confirmed"):
                    event["database_record"] = event.get("detection_file")
                    skipped += 1
                    continue
                passage = dt.datetime.fromisoformat(event["passage_at"])
                filename = no_audio_filename(event)
                exists = db.execute(
                    "SELECT 1 FROM detections WHERE Sci_Name = ? AND File_Name = ? LIMIT 1",
                    (event["species"], filename),
                ).fetchone()
                if not exists:
                    db.execute(
                        """INSERT INTO detections
                           (Date, Time, Sci_Name, Com_Name, Confidence, Lat, Lon,
                            Cutoff, Week, Sens, Overlap, File_Name)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            passage.strftime("%Y-%m-%d"), passage.strftime("%H:%M:%S"),
                            event["species"], event["common_name"], 1.0, lat, lon,
                            0.0, int(passage.strftime("%V")), 0.0, 0.0, filename,
                        ),
                    )
                    imported += 1
                else:
                    skipped += 1
                event["database_record"] = filename
                event["audio_available"] = False
            db.commit()
        except Exception:
            db.rollback()
            raise
    return imported, skipped


def days_between(start: dt.datetime, end: dt.datetime) -> list[dt.date]:
    return [start.date() + dt.timedelta(days=n) for n in range((end.date() - start.date()).days + 1)]


def json_lines(path: Path):
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def local_realtime_events(config_root: Path) -> list[dict]:
    """Read the locally archived realtime feeds (no network requests).

    For MBTA, later snapshots supersede earlier predictions for a trip. For
    Amtrak, the stored closest GPS position is substantially more accurate
    than interpolation between station reports when a train is delayed.
    """
    root = config_root / "BirdSongs" / "TrainRecordings" / "wellesley-train-study"
    mbta_latest: dict[tuple[str, str], dict] = {}
    for payload in json_lines(root / "mbta-v3-predictions.jsonl"):
        for hint in payload.get("passage_hints", []):
            if not isinstance(hint, dict) or not hint.get("estimated_closest_passage"):
                continue
            passage = dt.datetime.fromisoformat(hint["estimated_closest_passage"].replace("Z", "+00:00")).astimezone(LOCAL)
            trip = str(hint.get("trip_id") or "unknown")
            mbta_latest[(passage.date().isoformat(), trip)] = {
                "event_id": f"mbta-realtime:{passage.date()}:{trip}",
                "operator": "MBTA",
                "species": "Machina ferriviaria",
                "common_name": "MBTA Commuter Rail",
                "passage_at": passage.isoformat(),
                "passage_window_start": hint.get("from_time"),
                "passage_window_end": hint.get("to_time"),
                "direction": "Wellesley Hills to Wellesley Square" if int(hint.get("direction_id", 0)) == 0 else "Wellesley Square to Wellesley Hills",
                "train": str(hint.get("vehicle_id") or trip),
                "trip_id": trip,
                "evidence": "locally_archived_realtime_prediction",
                "source": "local MBTA V3 realtime archive",
                "source_url": None,
                "acoustically_confirmed": False,
            }

    amtrak = []
    for payload in json_lines(root / "amtraker-v3-passages.jsonl"):
        position = payload.get("closest_position") or {}
        timestamp = position.get("last_position_at")
        if not timestamp:
            continue
        passage = dt.datetime.fromisoformat(timestamp).astimezone(LOCAL)
        amtrak.append({
            "event_id": f"amtrak-realtime:{payload.get('event_id')}",
            "operator": "Amtrak",
            "species": "Machina ferriviaria amtrak",
            "common_name": "Amtrak Lake Shore Limited",
            "passage_at": passage.isoformat(),
            "passage_window_start": passage.isoformat(),
            "passage_window_end": passage.isoformat(),
            "direction": position.get("heading"),
            "train": str(position.get("train_number") or position.get("train_id") or "unknown"),
            "train_id": position.get("train_id"),
            "distance_to_mic_km": position.get("distance_to_mic_km"),
            "evidence": "locally_archived_realtime_gps_position",
            "source": "local Amtraker v3 realtime archive",
            "source_url": None,
            "acoustically_confirmed": False,
        })
    return list(mbta_latest.values()) + amtrak


def overlay_local_realtime(events: list[dict], realtime: list[dict]) -> list[dict]:
    result = list(events)
    for local in sorted(realtime, key=lambda event: event["passage_at"]):
        passage = dt.datetime.fromisoformat(local["passage_at"])
        candidates = []
        for index, existing in enumerate(result):
            if existing["species"] != local["species"]:
                continue
            if local["operator"] == "MBTA" and existing.get("direction") != local.get("direction"):
                continue
            if local["operator"] == "Amtrak" and existing.get("train") != local.get("train"):
                continue
            delta = abs((passage - dt.datetime.fromisoformat(existing["passage_at"])).total_seconds())
            limit = 10 * 60 if local["operator"] == "MBTA" else 18 * 60 * 60
            if delta <= limit:
                candidates.append((delta, index))
        if candidates:
            _, index = min(candidates)
            result[index] = local
        else:
            result.append(local)
    return result


def mbta_events(day: dt.date, cache_root: Path, refresh: bool) -> list[dict]:
    events = []
    for direction, config in MBTA_DIRECTIONS.items():
        query = urllib.parse.urlencode(
            {"from_stop": config["from"], "to_stop": config["to"]}, doseq=True
        )
        payload = fetch_json(
            f"{TRANSITMATTERS_API.format(date=day.isoformat())}?{query}",
            cache_root / f"transitmatters-{day}-direction-{direction}.json",
            refresh,
        )
        if not isinstance(payload, list):
            continue
        for row in payload:
            departed = dt.datetime.fromisoformat(row["dep_dt"]).replace(tzinfo=LOCAL)
            arrived = dt.datetime.fromisoformat(row["arr_dt"]).replace(tzinfo=LOCAL)
            if arrived <= departed:
                continue
            passage = departed + (arrived - departed) / 2
            vehicle = str(row.get("vehicle_label") or row.get("vehicle_consist") or "unknown")
            events.append({
                "event_id": f"mbta:{passage.isoformat()}:{vehicle}:{direction}",
                "operator": "MBTA",
                "species": "Machina ferriviaria",
                "common_name": "MBTA Commuter Rail",
                "passage_at": passage.isoformat(),
                "passage_window_start": departed.isoformat(),
                "passage_window_end": arrived.isoformat(),
                "direction": config["label"],
                "train": vehicle,
                "evidence": "observed_vehicle_travel_time",
                "source": "TransitMatters Data Dashboard API",
                "source_url": TRANSITMATTERS_API.format(date=day.isoformat()),
                "acoustically_confirmed": False,
            })
    return events


def stop_time(stop: dict, action: str) -> tuple[dt.datetime | None, str]:
    scheduled = stop.get(f"sched_{action}")
    if scheduled is None:
        scheduled = stop.get("sched_depart" if action == "arrive" else "sched_arrive")
    status = stop.get("depart" if action == "depart" else "arrive") or {}
    if not isinstance(scheduled, (int, float)):
        return None, "missing"
    when = dt.datetime.fromtimestamp(scheduled, tz=LOCAL)
    variance = status.get("variance")
    if isinstance(variance, (int, float)):
        when += dt.timedelta(seconds=variance)
    return when, str(status.get("type") or "SCHEDULED").lower()


def amtrak_event(day: dt.date, train: str, cache_root: Path, refresh: bool) -> dict | None:
    url = TRANSITDOCS_API.format(year=day.year, month=day.month, day=day.day, train=train)
    try:
        payload = fetch_json(url, cache_root / f"transitdocs-{day}-train-{train}.json", refresh)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if not isinstance(payload, dict):
        return None
    stops = {row.get("code"): row for row in payload.get("stops", []) if isinstance(row, dict)}
    if train == "449":
        origin, destination = stops.get("BBY"), stops.get("FRA")
        fraction = (AMTRAK_MIC_MILE - 1.0) / 20.0
        direction = "Back Bay to Framingham"
    else:
        origin, destination = stops.get("FRA"), stops.get("BBY")
        fraction = (21.0 - AMTRAK_MIC_MILE) / 20.0
        direction = "Framingham to Back Bay"
    if not origin or not destination or origin.get("canceled") or destination.get("canceled"):
        return None
    departed, departure_kind = stop_time(origin, "depart")
    arrived, arrival_kind = stop_time(destination, "arrive")
    if departed is None or arrived is None or arrived <= departed:
        return None
    passage = departed + (arrived - departed) * fraction
    kinds = {departure_kind, arrival_kind}
    evidence = "archive_actual_station_reports" if kinds == {"actual"} else "archive_estimated_station_reports"
    return {
        "event_id": f"amtrak:{day}:train-{train}",
        "operator": "Amtrak",
        "species": "Machina ferriviaria amtrak",
        "common_name": "Amtrak Lake Shore Limited",
        "passage_at": passage.isoformat(),
        "passage_window_start": departed.isoformat(),
        "passage_window_end": arrived.isoformat(),
        "direction": direction,
        "train": train,
        "evidence": evidence,
        "source": "TransitDocs Amtrak Status Maps Archive",
        "source_url": url,
        "acoustically_confirmed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, default=Path("runtime/config"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--import-no-audio", action="store_true",
        help="add operational passages to birds.db with playback-safe no-audio markers",
    )
    args = parser.parse_args()
    config_root = args.config_root.resolve()
    start, end = database_bounds(config_root / "birds.db")
    output_root = config_root / "BirdSongs" / "TrainRecordings" / "historical-backfill"
    cache_root = output_root / "api-cache"

    events = []
    for day in days_between(start, end):
        events.extend(mbta_events(day, cache_root, args.refresh))
        for train in ("448", "449"):
            event = amtrak_event(day, train, cache_root, args.refresh)
            if event:
                events.append(event)
    events = overlay_local_realtime(events, local_realtime_events(config_root))
    events = sorted(
        (event for event in events if start <= dt.datetime.fromisoformat(event["passage_at"]) <= end),
        key=lambda event: event["passage_at"],
    )
    mark_existing_audio_confirmations(config_root / "birds.db", events)
    imported = skipped = 0
    if args.import_no_audio:
        imported, skipped = import_no_audio_events(config_root, events)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "coverage_start": start.isoformat(),
        "coverage_end": end.isoformat(),
        "note": "Operational passage evidence only; rows are not acoustic detections until audio is confirmed.",
        "events": events,
    }
    output = output_root / "train-history.json"
    atomic_json(output, payload)
    counts = {operator: sum(event["operator"] == operator for event in events) for operator in ("MBTA", "Amtrak")}
    confirmed = sum(bool(event["acoustically_confirmed"]) for event in events)
    print(f"wrote {len(events)} operational passages to {output}")
    print(
        f"MBTA={counts['MBTA']} Amtrak={counts['Amtrak']} acoustic_confirmations={confirmed} "
        f"coverage={start.isoformat()}..{end.isoformat()}"
    )
    if args.import_no_audio:
        print(f"database imported={imported} already_present_or_audio_confirmed={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
