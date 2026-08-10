#!/usr/bin/env python3
"""Persist conservative Amtraker snapshots for trains that traverse Wellesley."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path


API = "https://api.amtraker.com/v3/trains"
REQUIRED_STATIONS = ("BBY", "FRA", "WOR")
UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def request_snapshot() -> tuple[object, dict[str, str]]:
    request = urllib.request.Request(
        API,
        headers={
            "Accept": "application/json",
            "User-Agent": "AvianVisitors Wellesley Amtrak passage recorder/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response), {key.lower(): value for key, value in response.headers.items()}


def matching_trains(payload: object, site_lat: float, site_lon: float, nearby_km: float) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    matches = []
    for trains in payload.values():
        if not isinstance(trains, list):
            continue
        for train in trains:
            if not isinstance(train, dict):
                continue
            if train.get("providerShort") != "AMTK":
                continue
            if str(train.get("routeName", "")).casefold() != "lake shore limited":
                continue
            station_codes = [row.get("code") for row in train.get("stations", []) if isinstance(row, dict)]
            if not all(code in station_codes for code in REQUIRED_STATIONS):
                continue
            try:
                lat, lon = float(train["lat"]), float(train["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            distance = haversine_km(site_lat, site_lon, lat, lon)
            matches.append({
                "train_id": train.get("trainID"),
                "train_number": train.get("trainNumRaw") or train.get("trainNum"),
                "route_name": train.get("routeName"),
                "state": train.get("trainState"),
                "latitude": lat,
                "longitude": lon,
                "distance_to_mic_km": round(distance, 3),
                "near_wellesley": distance <= nearby_km,
                "heading": train.get("heading"),
                "velocity_mph": train.get("velocity"),
                "next_station_code": train.get("eventCode"),
                "next_station_name": train.get("eventName"),
                "origin_code": train.get("origCode"),
                "destination_code": train.get("destCode"),
                "updated_at": train.get("updatedAt"),
                "last_position_at": train.get("lastValTS"),
                "stations": [
                    {
                        "code": row.get("code"),
                        "status": row.get("status"),
                        "scheduled_arrival": row.get("schArr"),
                        "actual_arrival": row.get("arr"),
                        "actual_departure": row.get("dep"),
                    }
                    for row in train.get("stations", [])
                    if isinstance(row, dict) and row.get("code") in ("BOS", "BBY", "FRA", "WOR")
                ],
            })
    return sorted(matches, key=lambda row: (row["distance_to_mic_km"], str(row["train_id"])))


def main() -> int:
    if os.getenv("AMTRAK_REALTIME_ENABLED", "false").lower() != "true":
        print("Amtrak realtime recorder: disabled", flush=True)
        while True:
            time.sleep(3600)

    session_id = os.getenv("TRAIN_RECORD_SESSION_ID", "wellesley-train-study")
    if not session_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise SystemExit("Amtrak realtime recorder: unsafe session id")
    interval = max(60, int(os.getenv("AMTRAK_REALTIME_POLL_SECONDS", "60")))
    # Exact midpoint of Wellesley Hills and Wellesley Square. BirdNET's
    # coordinates may be an IP-derived location and are not precise enough
    # for closest-approach timing along the railway.
    site_lat = float(os.getenv("AMTRAK_SITE_LATITUDE", "42.30186202682135"))
    site_lon = float(os.getenv("AMTRAK_SITE_LONGITUDE", "-71.28787466467018"))
    nearby_km = max(1.0, float(os.getenv("AMTRAK_WELLESLEY_RADIUS_KM", "15")))
    root = Path(os.getenv("TRAIN_RECORD_ROOT", "/config/BirdSongs/TrainRecordings"))
    output = root / session_id / "amtraker-v3-trains.jsonl"
    passages_output = root / session_id / "amtraker-v3-passages.jsonl"
    print(
        f"Amtrak realtime recorder: one all-trains request every {interval}s; "
        f"Lake Shore Limited via BBY/FRA/WOR only; near-site radius={nearby_km:g}km",
        flush=True,
    )

    backoff = interval
    tracked: dict[str, dict] = {}
    emitted: set[str] = set()
    while True:
        fetched_at = utc_now()
        try:
            payload, _headers = request_snapshot()
            trains = matching_trains(payload, site_lat, site_lon, nearby_km)
            append_jsonl(output, {
                "fetched_at": iso(fetched_at),
                "source": "Amtraker v3",
                "source_url": API,
                "attribution": "Realtime train data provided by Amtraker",
                "site": {"latitude": site_lat, "longitude": site_lon},
                "trains": trains,
            })
            present = set()
            for train in trains:
                train_id = str(train.get("train_id") or train.get("train_number") or "unknown")
                present.add(train_id)
                state = tracked.setdefault(train_id, {"misses": 0, "minimum": train})
                state["misses"] = 0
                if train["distance_to_mic_km"] < state["minimum"]["distance_to_mic_km"]:
                    state["minimum"] = train
                minimum = state["minimum"]
                # Amtraker positions update about every three minutes. Wait
                # until the train is clearly moving away so the recorded
                # minimum is a stable audio-review timestamp.
                if (
                    minimum["distance_to_mic_km"] <= 5.0
                    and train["distance_to_mic_km"] >= minimum["distance_to_mic_km"] + 2.0
                ):
                    event_id = f"{train_id}:{minimum.get('last_position_at')}"
                    if event_id not in emitted:
                        append_jsonl(passages_output, {
                            "event_id": event_id,
                            "identified_at": iso(fetched_at),
                            "source": "Amtraker v3",
                            "species": "Machina ferriviaria amtrak",
                            "common_name": "Amtrak Lake Shore Limited",
                            "closest_position": minimum,
                        })
                        emitted.add(event_id)
            for train_id, state in list(tracked.items()):
                if train_id in present:
                    continue
                state["misses"] += 1
                # If the feed drops the train after it passes, finalize the
                # best observed position after ten absent polls.
                minimum = state["minimum"]
                if state["misses"] >= 10 and minimum["distance_to_mic_km"] <= 5.0:
                    event_id = f"{train_id}:{minimum.get('last_position_at')}"
                    if event_id not in emitted:
                        append_jsonl(passages_output, {
                            "event_id": event_id,
                            "identified_at": iso(fetched_at),
                            "source": "Amtraker v3",
                            "species": "Machina ferriviaria amtrak",
                            "common_name": "Amtrak Lake Shore Limited",
                            "closest_position": minimum,
                        })
                        emitted.add(event_id)
                    del tracked[train_id]
            delay = interval
            backoff = interval
        except urllib.error.HTTPError as error:
            retry_after = error.headers.get("retry-after")
            if error.code == 429:
                delay = max(interval, int(retry_after or backoff))
            else:
                delay = backoff
            backoff = min(max(interval, backoff * 2), 3600)
            print(f"Amtrak realtime recorder: HTTP {error.code}; retrying in {delay}s", flush=True)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            delay = backoff
            backoff = min(max(interval, backoff * 2), 3600)
            print(f"Amtrak realtime recorder: {type(error).__name__}; retrying in {delay}s", flush=True)
        time.sleep(delay + random.uniform(0, min(3, interval / 10)))


if __name__ == "__main__":
    raise SystemExit(main())
