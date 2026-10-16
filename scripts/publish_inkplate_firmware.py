#!/usr/bin/env python3
"""Build, sign, verify, and atomically publish an Inkplate OTA release."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from prepare_inkplate_timing_library import prepare_timing_library


ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "inkplate" / "AvianVisitorsInkplate"
VERSION_HEADER = SKETCH / "firmware_version.h"
PRIVATE_KEY = ROOT / "secrets" / "inkplate-ota-private.pem"
PUBLIC_KEY = ROOT / "inkplate" / "keys" / "inkplate-ota-public.pem"
OUTPUT_DIR = ROOT / "build" / "inkplate"
PUBLISH_DIR = ROOT / "runtime" / "config" / "InkplateOTA"
FQBN = (
    "soldered-inkplate-boards:esp32:Inkplate13SPECTRA:"
    "PartitionScheme=app13M_data7M_32MB"
)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def firmware_identity() -> tuple[int, str]:
    text = VERSION_HEADER.read_text(encoding="utf-8")
    build_match = re.search(r"AVIAN_FIRMWARE_BUILD\s+(\d+)UL", text)
    version_match = re.search(r'AVIAN_FIRMWARE_VERSION\s+"([^"]+)"', text)
    if not build_match or not version_match:
        raise RuntimeError(f"Could not parse firmware identity from {VERSION_HEADER}")
    return int(build_match.group(1)), version_match.group(1)


def find_application_binary() -> Path:
    candidates = sorted(set(OUTPUT_DIR.glob("*.ino.bin")) | set(OUTPUT_DIR.glob("*.bin")))
    candidates = [
        path for path in candidates
        if not any(part in path.name for part in ("bootloader", "partitions", "merged", "signed"))
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one application binary, found: {candidates}")
    return candidates[0]


def existing_build() -> int:
    manifest = PUBLISH_DIR / "manifest.txt"
    if not manifest.exists():
        return 0
    match = re.search(r"^build=(\d+)$", manifest.read_text(encoding="utf-8"), re.MULTILINE)
    return int(match.group(1)) if match else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="republish an equal build")
    args = parser.parse_args()

    build, version = firmware_identity()
    previous = existing_build()
    if build < previous or (build == previous and not args.force):
        raise RuntimeError(
            f"Build {build} is not newer than published build {previous}; "
            "increment firmware_version.h (or use --force for an identical rebuild)."
        )
    if not PRIVATE_KEY.is_file():
        raise RuntimeError(f"Missing private signing key: {PRIVATE_KEY}")
    if not shutil.which("arduino-cli") or not shutil.which("openssl"):
        raise RuntimeError("arduino-cli and openssl are required")

    timing_library = prepare_timing_library()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run([
        "arduino-cli", "compile", "--fqbn", FQBN,
        "--library", str(timing_library),
        "--output-dir", str(OUTPUT_DIR), str(SKETCH),
    ])
    binary = find_application_binary()

    with tempfile.TemporaryDirectory(prefix="inkplate-ota-") as temp_name:
        temp = Path(temp_name)
        signature = temp / "signature.bin"
        signed = temp / f"firmware-{build}.signed.bin"
        run([
            "openssl", "dgst", "-sha256", "-sign", str(PRIVATE_KEY),
            "-out", str(signature), str(binary),
        ])
        if signature.stat().st_size != 512:
            raise RuntimeError("The signing key must be RSA-4096 (512-byte signature)")
        signed.write_bytes(binary.read_bytes() + signature.read_bytes())
        run([
            "openssl", "dgst", "-sha256", "-verify", str(PUBLIC_KEY),
            "-signature", str(signature), str(binary),
        ])

        payload = signed.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        filename = signed.name
        manifest_text = (
            "AVIAN-INKPLATE-OTA/1\n"
            f"build={build}\n"
            f"version={version}\n"
            f"size={len(payload)}\n"
            f"sha256={digest}\n"
            f"url=/inkplate-ota/{filename}\n"
        )

        PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
        firmware_temp = PUBLISH_DIR / f".{filename}.tmp"
        manifest_temp = PUBLISH_DIR / ".manifest.txt.tmp"
        firmware_temp.write_bytes(payload)
        manifest_temp.write_text(manifest_text, encoding="utf-8")
        os.replace(firmware_temp, PUBLISH_DIR / filename)
        os.replace(manifest_temp, PUBLISH_DIR / "manifest.txt")

    print(f"Published signed Inkplate build {build} ({version})")
    print(f"Manifest: {PUBLISH_DIR / 'manifest.txt'}")
    print(f"Firmware: {PUBLISH_DIR / filename} ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
