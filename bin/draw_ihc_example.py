"""Draw core/static/core/IHC-ideal.png, the IHC cell of the gene page's
Example row (26 Sep 2026).

Laid out as IF-Ideal.png is, so the two read alike: WT and KO shown together
on the left (there a cell mosaic, here the two pellet cores on one slide), one
wild-type and one knockout cell under a rule on the right, and the caption
underneath. 500x500, thick black outlines, flat colour; brown is DAB, blue is
the haematoxylin counterstain. Run: python3 bin/draw_ihc_example.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 4                                   # supersample, then downscale
im = Image.new("RGB", (500 * S, 500 * S), "white")
d = ImageDraw.Draw(im)
# Liberation Sans: metric-compatible with the Arial the other examples use.
FONT = ImageFont.truetype(
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 20 * S)
BLACK = (20, 20, 20)
DAB, DAB_CELL = (135, 65, 25), (196, 128, 78)
HAEM, PALE = (80, 105, 190), (236, 238, 246)


def text(x, y, s):
    d.multiline_text((x * S, y * S), s, fill=BLACK, font=FONT, anchor="mm",
                     align="center", spacing=2 * S)


def circle(cx, cy, r, fill, width):
    d.ellipse([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S],
              fill=fill, outline=BLACK, width=width * S)


def cell(cx, cy, r, fill, nucleus):
    """An irregular cell, thick outline, dark nucleus."""
    pts = []
    for i in range(12):
        a = i * math.pi * 2 / 12
        rr = r * (0.82 + 0.26 * ((i * 5) % 4) / 3)
        pts.append(((cx + rr * math.cos(a)) * S, (cy + rr * math.sin(a)) * S))
    d.polygon(pts, fill=fill, outline=BLACK, width=6 * S)
    circle(cx - 3, cy + 2, r * 0.36, nucleus, 3)


def core(cx, cy, r, colour):
    circle(cx, cy, r, "white", 6)
    for i in range(70):                 # sunflower scatter: even, no stripes
        rr = (r - 9) * math.sqrt((i + 0.5) / 70)
        a = i * 2.39996
        x, y = cx + rr * math.cos(a), cy + rr * math.sin(a)
        d.ellipse([(x - 3.2) * S, (y - 3.2) * S, (x + 3.2) * S, (y + 3.2) * S],
                  fill=colour)


# Left: the WT and KO pellet cores, side by side on one slide.
text(125, 78, "WT/KO\npellet cores")
core(82, 190, 40, DAB)
core(168, 190, 40, HAEM)

# Right: one wild-type and one knockout cell, as IF-Ideal.png draws them.
d.line([268 * S, 64 * S, 400 * S, 64 * S], fill=BLACK, width=2 * S)
text(298, 92, "WT")
text(370, 92, "KO")
cell(298, 190, 34, DAB_CELL, DAB)
cell(370, 190, 34, PALE, HAEM)

text(335, 440, "Target protein\ndetected\n(brown staining)")

im = im.resize((500, 500), Image.LANCZOS)
im.save(Path(__file__).resolve().parent.parent / "core/static/core/IHC-ideal.png",
        optimize=True)
