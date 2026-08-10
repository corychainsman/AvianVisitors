#!/usr/bin/env python3
"""Build and cache an AvianVisitors species manifest for one eBird region.

The eBird API is contacted only when the cached labels file is absent or
--force is explicitly supplied. A refresh performs exactly the two requests
required by AvianVisitors' documented filter: regional species codes and the
taxonomy table. HTTP failures are not retried.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pregen import ebird_filter, parse_species_list, slugify


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def serialize(species: list[tuple[str, str]]) -> str:
    return "".join(f"{scientific}|{common}\n" for scientific, common in species)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--region", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--missing-out", required=True, type=Path)
    parser.add_argument("--illustrations", required=True, type=Path)
    parser.add_argument("--ebird-key", default=os.environ.get("EBIRD_API_KEY", ""))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.out.exists() and not args.force:
        species, skipped = parse_species_list(args.out.read_text().splitlines())
        if skipped or not species:
            print(f"error: invalid cached manifest: {args.out}", file=sys.stderr)
            return 2
        print(f"[cache] using {args.out}; no eBird API requests")
    else:
        if not args.ebird_key:
            print("error: EBIRD_API_KEY required", file=sys.stderr)
            return 2
        candidates, skipped = parse_species_list(args.labels.read_text().splitlines())
        if skipped or not candidates:
            print(
                f"error: labels contained {skipped} malformed entries",
                file=sys.stderr,
            )
            return 2
        print(
            f"[ebird] resolving {args.region} with exactly two requests; "
            "rate-limit responses will not be retried"
        )
        species = ebird_filter(candidates, args.region, args.ebird_key)
        species.sort(key=lambda item: item[1].casefold())
        atomic_write(args.out, serialize(species))

    missing = []
    for scientific, common in species:
        slug = slugify(scientific)
        perched = args.illustrations / f"{slug}.png"
        flight = args.illustrations / f"{slug}-2.png"
        if not perched.is_file() or not flight.is_file():
            missing.append((scientific, common))
    atomic_write(args.missing_out, serialize(missing))

    print(
        f"region species: {len(species)}; complete existing pairs: "
        f"{len(species) - len(missing)}; missing pairs: {len(missing)}"
    )
    print(f"wrote {args.out} + {args.missing_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
