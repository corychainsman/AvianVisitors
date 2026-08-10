#!/usr/bin/env python3
"""Atomically publish a deterministic Inkplate-compatible JPEG and hash."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path

from PIL import Image


def encode_jpeg(source: Path) -> bytes:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
        if image.size != (1200, 1600):
            image = image.resize((1200, 1600), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=92,
            subsampling=0,
            optimize=True,
            progressive=False,
        )
        return output.getvalue()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path)
    args = parser.parse_args()

    jpeg = encode_jpeg(args.input)
    digest = hashlib.sha256(jpeg).hexdigest()
    previous = ""
    try:
        previous = args.signature.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        pass

    if digest != previous or not args.output.is_file():
        atomic_write(args.output, jpeg)
        atomic_write(args.signature, (digest + "\n").encode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
