#!/usr/bin/env python
"""Build the store-listing banners from the plain captures.

**Why this exists at all.** `core/views.py::LISTING_SCREENSHOTS` is one copy in
two renderings: `core/static/core/extension/` holds the plain capture, which
`/extension/` prints its words *underneath* as HTML, and
`browser-extension/store/listing/` holds the same picture with the headline, the
sentence and `LISTING_FRAME` drawn into a banner, because a Chrome Web Store
listing has nowhere to put a caption.

Nothing built the second one. It was made by hand outside the repository, so
when the verdict wording changed the website followed and the store set did not
— and on 12 Sep 2026 the five banners still in git read *"Data not supportive in
the conditions tested"* over a card whose own pixels said *"a 'not recommended'
result may still mean…"*: two retired phrases, on the images every new installer
sees first. `core/tests_extension_scope.py::TheScreenshotWordsAreOneCopyTests`
was the only thing that knew, and all it could say was that a file was missing.

So the banner is derived now. Change a headline in `LISTING_SCREENSHOTS`, re-run
this, and the store set says what the page says.

    python bin/build_listing_banners.py            # writes store/listing/
    python bin/build_listing_banners.py --check    # exit 1 if any is stale

**Staleness is compared against a manifest, not against mtimes.** Git does not
preserve modification times, so a fresh clone stamps every file with the
checkout time in no particular order — an mtime comparison would pass or fail by
accident in CI, which is worse than not checking. `banners.json` records the
headline, the sentence, the frame and the capture's SHA-256 that each banner was
built from, so a drift is a diff a reader can see and a check that cannot
flake. Comparing the rendered pixels instead was the other option and is the
wrong one: freetype renders a glyph differently across versions, so CI would
disagree with a laptop about a file neither had changed.

**The typeface is fetched, not committed.** Open Sans is what the site loads and
what these banners are set in; it is pulled from Google Fonts at build time, the
same source the page uses, rather than vendored — a committed font is a binary
nobody updates and a licence question nobody revisits. No network, no build: the
script says so and stops rather than substituting a face that is not the brand's.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "core" / "static" / "core" / "extension"
STORE = ROOT / "browser-extension" / "store" / "listing"
MANIFEST = STORE / "banners.json"

FONT_CSS = ("https://fonts.googleapis.com/css2"
            "?family=Open+Sans:wght@400;600;700;800")
CACHE = Path("/tmp/oga-listing-fonts")

# The canvas a Chrome Web Store screenshot wants, and the banner's share of it.
SIZE = (1280, 800)
BANNER_H = 195
NAVY = "#132a4a"
INK = "#ffffff"
FRAME_INK = "#b9c8db"
RULE = "#2c4468"


def _fonts():
    """Open Sans at the four weights, fetched once into /tmp."""
    CACHE.mkdir(parents=True, exist_ok=True)
    have = {int(p.stem.split("-")[1]): p for p in CACHE.glob("os-*.ttf")}
    if len(have) < 4:
        try:
            req = urllib.request.Request(
                FONT_CSS, headers={"User-Agent": "Mozilla/5.0"})
            css = urllib.request.urlopen(req, timeout=20).read().decode()
        except Exception as exc:                       # noqa: BLE001
            sys.exit(f"Could not fetch Open Sans ({exc}).\n"
                     "These banners are set in the site's own typeface and this "
                     "script will not substitute another one. Run it somewhere "
                     "with network access, or drop os-400/600/700/800.ttf into "
                     f"{CACHE}.")
        for block in re.split(r"@font-face\s*\{", css):
            weight = re.search(r"font-weight:\s*(\d+)", block)
            url = re.search(r"url\((https://fonts\.gstatic\.com[^)]+\.ttf)\)",
                            block)
            if weight and url:
                path = CACHE / f"os-{weight.group(1)}.ttf"
                urllib.request.urlretrieve(url.group(1), path)
                have[int(weight.group(1))] = path
    missing = {400, 600, 700, 800} - set(have)
    if missing:
        sys.exit(f"Open Sans weights missing: {sorted(missing)}")
    return have


def _wrap(draw, text, font, width):
    """Greedy wrap. `textlength` measures the real face, not a guess at it."""
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _banner(capture: Path, title: str, note: str, frame: str, fonts) -> Image.Image:
    canvas = Image.new("RGB", SIZE, NAVY)
    draw = ImageDraw.Draw(canvas)
    pad = 40
    inner = SIZE[0] - pad * 2

    f_title = ImageFont.truetype(str(fonts[800]), 30)
    f_note = ImageFont.truetype(str(fonts[400]), 17)
    f_frame = ImageFont.truetype(str(fonts[400]), 12)

    y = 22
    for line in _wrap(draw, title, f_title, inner):
        draw.text((pad, y), line, font=f_title, fill=INK)
        y += 40
    y += 2
    for line in _wrap(draw, note, f_note, inner):
        draw.text((pad, y), line, font=f_note, fill=INK)
        y += 24

    rule_y = BANNER_H - 62
    draw.line([(pad, rule_y), (SIZE[0] - pad, rule_y)], fill=RULE, width=1)
    y = rule_y + 12
    for line in _wrap(draw, frame, f_frame, inner):
        draw.text((pad, y), line, font=f_frame, fill=FRAME_INK)
        y += 18

    # The capture fills the rest, from its top edge: the marked antibody and the
    # card it opens are up there, and a centred crop loses one of them.
    shot = Image.open(capture).convert("RGB")
    room = SIZE[1] - BANNER_H
    if shot.width != SIZE[0]:
        h = round(shot.height * SIZE[0] / shot.width)
        shot = shot.resize((SIZE[0], h), Image.LANCZOS)
    canvas.paste(shot.crop((0, 0, SIZE[0], min(room, shot.height))),
                 (0, BANNER_H))
    return canvas


def _record(name, title, note, frame, capture: Path):
    """What a banner was built from, in a form a diff can show."""
    return {
        "file": name.replace(".webp", ".png"),
        "title": title,
        "note": note,
        "frame": frame,
        "capture": name,
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any banner is missing or out of date")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    import os

    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OGA_website.settings")
    django.setup()
    from core.views import LISTING_FRAME, LISTING_SCREENSHOTS

    wanted = []
    for name, title, note, _alt in LISTING_SCREENSHOTS:
        capture = CAPTURES / name
        if not capture.exists():
            sys.exit(f"No capture at {capture}")
        wanted.append((name, title, note, capture,
                       _record(name, title, note, LISTING_FRAME, capture)))

    if args.check:
        try:
            have = json.loads(MANIFEST.read_text())
        except (OSError, ValueError) as exc:
            print(f"no readable {MANIFEST.name} ({exc}) — run "
                  "bin/build_listing_banners.py", file=sys.stderr)
            return 1
        stale = []
        by_file = {row.get("file"): row for row in have}
        for name, _t, _n, _cap, record in wanted:
            row = by_file.get(record["file"])
            if row is None:
                stale.append(f"{record['file']}: no banner built")
            elif row != record:
                changed = [k for k in record if row.get(k) != record[k]]
                stale.append(f"{record['file']}: {', '.join(changed)} changed")
            elif not (STORE / record["file"]).exists():
                stale.append(f"{record['file']}: manifest but no file")
        for extra in sorted(set(by_file) - {r[4]["file"] for r in wanted}):
            stale.append(f"{extra}: not in LISTING_SCREENSHOTS any more")
        if stale:
            print("re-run bin/build_listing_banners.py:\n  "
                  + "\n  ".join(stale), file=sys.stderr)
            return 1
        return 0

    fonts = _fonts()
    STORE.mkdir(parents=True, exist_ok=True)
    for name, title, note, capture, _record_ in wanted:
        out = STORE / name.replace(".webp", ".png")
        _banner(capture, title, note, LISTING_FRAME, fonts).save(out, "PNG")
        print(f"{out.name:<44} {out.stat().st_size / 1024:.0f} KB")

    MANIFEST.write_text(json.dumps([r[4] for r in wanted], indent=2) + "\n")
    print(f"{MANIFEST.name:<44} {len(wanted)} banners recorded")

    # A banner for a figure that is no longer listed is one a reviewer still
    # sees in the dashboard, so say so rather than leaving it lying about.
    listed = {r[4]["file"] for r in wanted}
    for extra in sorted(p.name for p in STORE.glob("*.png")):
        if extra not in listed:
            print(f"note: {extra} is not in LISTING_SCREENSHOTS any more",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
