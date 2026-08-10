#!/usr/bin/env python3
"""Persist conservative MBTA V3 prediction snapshots near the listening site."""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API = "https://api-v3.mbta.com/predictions"
ROUTE = "CR-Worcester"
STOPS = ("place-WML-0135", "place-WML-0147")
PLATFORMS = {
    "WML-0135-01": ("place-WML-0135", "Wellesley Hills"),
    "WML-0135-02": ("place-WML-0135", "Wellesley Hills"),
    "WML-0147-01": ("place-WML-0147", "Wellesley Square"),
    "WML-0147-02": ("place-WML-0147", "Wellesley Square"),
}
UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def relation_id(resource: dict, name: str) -> str | None:
    data = resource.get("relationships", {}).get(name, {}).get("data")
    return data.get("id") if isinstance(data, dict) else None


def normalize_predictions(payload: dict) -> list[dict]:
    rows = []
    for resource in payload.get("data", []):
        attributes = resource.get("attributes", {})
        stop_id = relation_id(resource, "stop")
        trip_id = relation_id(resource, "trip")
        if stop_id not in PLATFORMS or not trip_id:
            continue
        place_id, stop_name = PLATFORMS[stop_id]
        rows.append({
            "prediction_id": resource.get("id"),
            "trip_id": trip_id,
            "vehicle_id": relation_id(resource, "vehicle"),
            "stop_id": stop_id,
            "place_id": place_id,
            "stop_name": stop_name,
            "direction_id": attributes.get("direction_id"),
            "arrival_time": attributes.get("arrival_time"),
            "departure_time": attributes.get("departure_time"),
            "status": attributes.get("status"),
            "stop_sequence": attributes.get("stop_sequence"),
        })
    return rows


def normalize_vehicles(payload: dict, predictions: list[dict]) -> list[dict]:
    wanted = {row["vehicle_id"] for row in predictions if row.get("vehicle_id")}
    vehicles = []
    for resource in payload.get("included", []):
        if resource.get("type") != "vehicle" or resource.get("id") not in wanted:
            continue
        attributes = resource.get("attributes", {})
        vehicles.append({
            "vehicle_id": resource.get("id"),
            "trip_id": relation_id(resource, "trip"),
            "latitude": attributes.get("latitude"),
            "longitude": attributes.get("longitude"),
            "bearing": attributes.get("bearing"),
            "speed": attributes.get("speed"),
            "current_status": attributes.get("current_status"),
            "updated_at": attributes.get("updated_at"),
        })
    return vehicles


def passage_hints(predictions: list[dict]) -> list[dict]:
    by_trip: dict[str, list[dict]] = {}
    for prediction in predictions:
        by_trip.setdefault(prediction["trip_id"], []).append(prediction)

    hints = []
    for trip_id, rows in by_trip.items():
        by_stop = {row["place_id"]: row for row in rows}
        if any(stop not in by_stop for stop in STOPS):
            continue
        ordered = sorted(
            by_stop.values(),
            key=lambda row: row.get("stop_sequence") if row.get("stop_sequence") is not None else 10_000,
        )
        origin, destination = ordered[0], ordered[-1]
        origin_time = parse_time(origin.get("departure_time") or origin.get("arrival_time"))
        destination_time = parse_time(destination.get("arrival_time") or destination.get("departure_time"))
        if origin_time is None or destination_time is None or destination_time <= origin_time:
            continue
        midpoint = origin_time + (destination_time - origin_time) / 2
        hints.append({
            "trip_id": trip_id,
            "vehicle_id": origin.get("vehicle_id") or destination.get("vehicle_id"),
            "direction_id": origin.get("direction_id"),
            "from_stop": origin["stop_name"],
            "to_stop": destination["stop_name"],
            "from_time": iso(origin_time),
            "to_time": iso(destination_time),
            "estimated_closest_passage": iso(midpoint),
        })
    return sorted(hints, key=lambda hint: hint["estimated_closest_passage"])


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def request_snapshot(api_key: str) -> tuple[dict, dict[str, str]]:
    query = urllib.parse.urlencode({
        "filter[route]": ROUTE,
        "filter[stop]": ",".join(STOPS),
        "include": "trip,vehicle,stop",
    })
    headers = {
        "Accept": "application/vnd.api+json",
        "User-Agent": "AvianVisitors MBTA passage recorder/1.0",
    }
    if api_key:
        headers["x-api-key"] = api_key
    request = urllib.request.Request(f"{API}?{query}", headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response), {key.lower(): value for key, value in response.headers.items()}


def main() -> int:
    if os.getenv("MBTA_REALTIME_ENABLED", "false").lower() != "true":
        print("MBTA realtime recorder: disabled", flush=True)
        while True:
            time.sleep(3600)

    session_id = os.getenv("TRAIN_RECORD_SESSION_ID", "wellesley-train-study")
    if not session_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise SystemExit("MBTA realtime recorder: unsafe session id")
    interval = max(60, int(os.getenv("MBTA_REALTIME_POLL_SECONDS", "60")))
    root = Path(os.getenv("TRAIN_RECORD_ROOT", "/config/BirdSongs/TrainRecordings"))
    output = root / session_id / "mbta-v3-predictions.jsonl"
    api_key = os.getenv("MBTA_API_KEY", "").strip()
    print(
        f"MBTA realtime recorder: one combined request every {interval}s; "
        f"authentication={'enabled' if api_key else 'disabled'}",
        flush=True,
    )

    backoff = interval
    while True:
        started = utc_now()
        try:
            payload, headers = request_snapshot(api_key)
            predictions = normalize_predictions(payload)
            record = {
                "fetched_at": iso(started),
                "route_id": ROUTE,
                "source": "MBTA V3 API",
                "predictions": predictions,
                "vehicles": normalize_vehicles(payload, predictions),
                "passage_hints": passage_hints(predictions),
            }
            append_jsonl(output, record)
            remaining = int(headers.get("x-ratelimit-remaining", "999999"))
            reset = int(headers.get("x-ratelimit-reset", "0"))
            if remaining <= 1 and reset > int(time.time()):
                delay = max(interval, reset - int(time.time()) + 2)
                print(f"MBTA realtime recorder: rate reserve reached; waiting {delay}s", flush=True)
            else:
                delay = interval
            backoff = interval
        except urllib.error.HTTPError as error:
            retry_after = error.headers.get("retry-after")
            if error.code == 429:
                delay = max(interval, int(retry_after or backoff))
            else:
                delay = backoff
            backoff = min(max(interval, backoff * 2), 3600)
            print(f"MBTA realtime recorder: HTTP {error.code}; retrying in {delay}s", flush=True)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            delay = backoff
            backoff = min(max(interval, backoff * 2), 3600)
            print(f"MBTA realtime recorder: {type(error).__name__}; retrying in {delay}s", flush=True)
        time.sleep(delay + random.uniform(0, min(3, interval / 10)))


if __name__ == "__main__":
    raise SystemExit(main())
