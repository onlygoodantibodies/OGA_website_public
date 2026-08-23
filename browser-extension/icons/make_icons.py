#!/usr/bin/env python3
"""Generate the extension icons.

Pure standard library so this runs anywhere, with no image dependency. The mark
is a traffic light: the fastest way to say "this tells you whether to trust the
reagent" at 16 pixels.

    python browser-extension/icons/make_icons.py
"""

import os
import struct
import zlib

BG = (22, 32, 43, 255)
GREEN = (28, 143, 75, 255)
AMBER = (217, 130, 23, 255)
RED = (198, 47, 47, 255)

HERE = os.path.dirname(os.path.abspath(__file__))


def _blend(dst, src, alpha):
    """Composite src over dst at the given coverage (0..1)."""
    return tuple(round(d + (s - d) * alpha) for d, s in zip(dst[:3], src[:3])) + (255,)


def _coverage_disc(px, py, cx, cy, r, samples=4):
    """Fractional pixel coverage of a disc, sampled for smooth edges."""
    inside = 0
    step = 1.0 / samples
    for sy in range(samples):
        for sx in range(samples):
            x = px + (sx + 0.5) * step
            y = py + (sy + 0.5) * step
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                inside += 1
    return inside / (samples * samples)


def _coverage_round_rect(px, py, w, h, radius, samples=4):
    inside = 0
    step = 1.0 / samples
    for sy in range(samples):
        for sx in range(samples):
            x = px + (sx + 0.5) * step
            y = py + (sy + 0.5) * step
            # Distance to the rounded-rectangle body.
            dx = max(radius - x, x - (w - radius), 0.0)
            dy = max(radius - y, y - (h - radius), 0.0)
            if dx * dx + dy * dy <= radius * radius:
                inside += 1
    return inside / (samples * samples)


def render(size):
    radius = size * 0.22
    pixels = [[(0, 0, 0, 0)] * size for _ in range(size)]

    # Body.
    for y in range(size):
        for x in range(size):
            cov = _coverage_round_rect(x, y, size, size, radius)
            if cov > 0:
                pixels[y][x] = tuple(BG[:3]) + (round(255 * cov),)

    # Three lamps down the centre.
    lamp_r = size * 0.115
    cx = size / 2
    for i, colour in enumerate((GREEN, AMBER, RED)):
        cy = size * (0.255 + i * 0.245)
        lo_y = max(0, int(cy - lamp_r - 1))
        hi_y = min(size, int(cy + lamp_r + 2))
        lo_x = max(0, int(cx - lamp_r - 1))
        hi_x = min(size, int(cx + lamp_r + 2))
        for y in range(lo_y, hi_y):
            for x in range(lo_x, hi_x):
                cov = _coverage_disc(x, y, cx, cy, lamp_r)
                if cov > 0:
                    base = pixels[y][x]
                    if base[3] == 0:
                        base = BG
                    pixels[y][x] = _blend(base, colour, cov)

    return pixels


def write_png(path, pixels):
    size = len(pixels)
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")

    with open(path, "wb") as fh:
        fh.write(png)


def main():
    for size in (16, 48, 128):
        path = os.path.join(HERE, f"icon-{size}.png")
        write_png(path, render(size))
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")


if __name__ == "__main__":
    main()
