#!/usr/bin/env python3
"""Extract a generated subject from a variable chroma background.

Only key-colored pixels connected to the canvas border are removed. This
preserves pale or slightly key-tinted pixels enclosed by the subject's ink
outline, avoiding the holes that global color keys and semantic matting can
create in silver vehicles.
"""

from __future__ import annotations

import argparse
import statistics
from collections import deque
from pathlib import Path

from PIL import Image


def estimate_border_color(image: Image.Image) -> tuple[int, int, int]:
    pixels = image.load()
    width, height = image.size
    samples = []
    for x in range(width):
        samples.extend((pixels[x, 0][:3], pixels[x, height - 1][:3]))
    for y in range(1, height - 1):
        samples.extend((pixels[0, y][:3], pixels[width - 1, y][:3]))
    return tuple(round(statistics.median(color[channel] for color in samples)) for channel in range(3))


def remove_connected_background(image: Image.Image, tolerance: int) -> Image.Image:
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()
    target = estimate_border_color(image)
    limit = tolerance * tolerance
    background = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()

    def enqueue(x: int, y: int) -> None:
        index = y * width + x
        if background[index]:
            return
        red, green, blue, _ = pixels[x, y]
        distance = (red - target[0]) ** 2 + (green - target[1]) ** 2 + (blue - target[2]) ** 2
        if distance <= limit:
            background[index] = 1
            queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(1, height - 1):
        enqueue(0, y)
        enqueue(width - 1, y)
    while queue:
        x, y = queue.popleft()
        if x:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)

    alpha = Image.new("L", image.size, 255)
    alpha.putdata([0 if value else 255 for value in background])
    image.putalpha(alpha)
    return image


def keep_largest_component(image: Image.Image) -> Image.Image:
    width, height = image.size
    alpha = image.getchannel("A")
    pixels = alpha.load()
    seen = bytearray(width * height)
    components: list[list[tuple[int, int]]] = []
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if seen[index] or pixels[x, y] == 0:
                continue
            seen[index] = 1
            queue = deque([(x, y)])
            component = []
            while queue:
                cx, cy = queue.popleft()
                component.append((cx, cy))
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < width and 0 <= ny < height:
                        neighbor = ny * width + nx
                        if not seen[neighbor] and pixels[nx, ny] > 0:
                            seen[neighbor] = 1
                            queue.append((nx, ny))
            components.append(component)
    if not components:
        raise RuntimeError("segmentation produced no foreground")
    keep = {y * width + x for x, y in max(components, key=len)}
    alpha.putdata([255 if index in keep else 0 for index in range(width * height)])
    image.putalpha(alpha)
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tolerance", type=int, default=78)
    parser.add_argument("--crop-right", type=int, help="discard pixels at and beyond this x coordinate")
    parser.add_argument("--margin", type=int, default=12)
    args = parser.parse_args()

    image = Image.open(args.input).convert("RGBA")
    if args.crop_right is not None:
        image = image.crop((0, 0, min(args.crop_right, image.width), image.height))
    image = keep_largest_component(remove_connected_background(image, args.tolerance))
    bbox = image.getchannel("A").getbbox()
    if not bbox:
        raise RuntimeError("segmentation produced an empty foreground")
    margin = max(0, args.margin)
    crop = (
        max(0, bbox[0] - margin), max(0, bbox[1] - margin),
        min(image.width, bbox[2] + margin), min(image.height, bbox[3] + margin),
    )
    image.crop(crop).save(args.out, optimize=True)
    print(f"segmented {args.input.name} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
