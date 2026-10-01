"""Draw core/static/core/IHC-ideal.png, the IHC cell of the gene page's
Example row.

The successful-antibody column of the IHC panel on Using the Data
(core/static/core/Edu-table.png), so the two pages draw one picture: cell
pellets stacked WT / KO / mosaic, as the real crops are, with the caption
underneath as the other examples have it. Brown is DAB, blue the haematoxylin
counterstain; in the mosaic, stained and unstained cells share one field.
500x500, fixed seeds so a re-run draws the same image.
Run: python3 bin/draw_ihc_example.py
"""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 4                                   # supersample, then downscale
im = Image.new("RGB", (500 * S, 500 * S), "white")
d = ImageDraw.Draw(im)
# Liberation Sans: metric-compatible with the Arial the other examples use.
FONT = ImageFont.truetype(
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 20 * S)
BLACK = (35, 31, 32)
BROWN = [(120, 72, 30), (140, 88, 40), (158, 104, 52)]
PALE = [(214, 214, 232), (200, 204, 226), (224, 222, 238)]
NUC = [(52, 58, 128), (66, 74, 146), (82, 86, 156)]


def text(x, y, s):
    d.multiline_text((x * S, y * S), s, fill=BLACK, font=FONT, anchor="mm",
                     align="center", spacing=2 * S)


def pellet(cx, cy, r, stained, seed, n=40):
    """A pellet core: packed round cells, each brown (stained) or pale, with a
    blue nucleus. `stained(rnd)` decides each cell."""
    rnd = random.Random(seed)
    cx, cy, r = cx * S, cy * S, r * S
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(246, 243, 238),
              outline=BLACK, width=3 * S)
    cells, tries = [], 0
    while len(cells) < n and tries < 6000:
        tries += 1
        cr = rnd.uniform(8.5, 10.5) * S
        ang = rnd.uniform(0, 2 * math.pi)
        dist = math.sqrt(rnd.random()) * (r - cr - 4 * S)
        x, y = cx + dist * math.cos(ang), cy + dist * math.sin(ang)
        if all(math.hypot(x - a, y - b) > (cr + c) * 0.85 for a, b, c in cells):
            cells.append((x, y, cr))
    for x, y, cr in cells:
        on = stained(rnd)
        ex, ey = cr * rnd.uniform(0.9, 1.1), cr * rnd.uniform(0.9, 1.1)
        d.ellipse([x - ex, y - ey, x + ex, y + ey],
                  fill=rnd.choice(BROWN if on else PALE),
                  outline=(90, 60, 40) if on else (160, 164, 196), width=S)
        nr = cr * rnd.uniform(0.40, 0.5)
        nx, ny = x + rnd.uniform(-2, 2) * S, y + rnd.uniform(-2, 2) * S
        d.ellipse([nx - nr, ny - nr, nx + nr, ny + nr], fill=rnd.choice(NUC))


rows = [("WT", 82, lambda r: True),
        ("KO", 210, lambda r: False),
        ("Mosaic", 338, lambda r: r.random() < 0.5)]
for i, (label, cy, stained) in enumerate(rows):
    text(150, cy, label)
    pellet(285, cy, 60, stained, seed=i)

text(285, 445, "Target protein\ndetected in WT, not KO\n(brown staining)")

im = im.resize((500, 500), Image.LANCZOS)
im.save(Path(__file__).resolve().parent.parent / "core/static/core/IHC-ideal.png",
        optimize=True)
