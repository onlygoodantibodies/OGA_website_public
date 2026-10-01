"""
Deterministic crop engine for the OGA figure cropper (Phase 2).

NO AI. The only operations performed on *source* pixels are: crop, LANCZOS
scale, and paste onto a white canvas (image-integrity rule, spec §8). All drawn
text/legends live on the white canvas only — never over blot/microscopy content.

This module is framework-free (pure Pillow) so it can be unit-tested in
isolation and reused by the pipeline views at commit time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

CANVAS = 490  # px, square output canvas

# ── How big a source figure may be ───────────────────────────────────────────
#
# Cropping decodes a whole figure, and a decoded figure is far bigger than the
# file it came from: a 3 MB PNG at 7003×4961 is 99 MB of pixels in memory. On
# 14 Aug 2026 a session holding six of them at once went over the 512 MB the web
# service has and the container was killed mid-commit — see
# `commit.py::_by_figure`, which made the peak the *largest* figure rather than
# the sum of them.
#
# That fix bounds the peak; it does not bound the figure. One enormous scan
# would still take the container down, and the failure mode is the worst kind:
# the request dies, the browser gets the platform's error page, and nothing on
# any screen says why. So the size is refused at the door instead, where the
# person can act on it.
#
# The arithmetic, so the number can be re-derived rather than trusted: the
# service has 512 MB, the app rests at roughly 200 MB, and the crop work either
# side of the decode (the numpy mask in `_trim_white`, the canvas, the PNG
# buffer) wants headroom. That leaves about 240 MB for the decoded figure.
# Four bytes per pixel is assumed rather than three — a PNG with an alpha
# channel decodes to RGBA — which is the conservative direction, and it means
# the limit does not depend on a mode the caller may not have read yet. 240 MB
# at 4 bytes is 60 megapixels.
#
# For scale: the largest figure on file is 34.7 MP, so this is not a limit
# anybody's current work is near.
MAX_MEGAPIXELS = 60.0
_BYTES_PER_PIXEL = 4


def megapixels(width, height) -> float:
    return (int(width or 0) * int(height or 0)) / 1_000_000


def size_refusal(width, height, name: str = "") -> str:
    """Why this figure is too big to crop, or ``""``.

    Says what it is, what the ceiling is and what to do about it — the shape
    ``services/concentration.py`` and ``services/c_number.py`` hold. It names no
    setting and no megabyte figure: this reaches a bench scientist, and "the
    container has 512 MB" is not something they can act on, where "split it in
    two" is.
    """
    mp = megapixels(width, height)
    if mp <= MAX_MEGAPIXELS:
        return ""
    called = f"“{name}” is" if name else "That figure is"
    return (
        f"{called} {int(width)} × {int(height)} pixels ({mp:.0f} megapixels), "
        f"which is too big to crop here — the most one figure may carry is "
        f"{MAX_MEGAPIXELS:.0f} megapixels. Split it into two figures the way a "
        f"wide flow panel usually is, so each part keeps its panels at full "
        f"size, or save it again at a smaller scale. Each finished crop is "
        f"rendered about {CANVAS} pixels square, so detail beyond that in the "
        f"original is discarded anyway.")

# Per-type fill factor (fraction of the available area the crop fills)
FILL = {"WB": 0.90, "IP": 0.95, "ICC-IF": 1.00, "FC": 0.90, "IHC": 0.97}

# Bottom canvas space reserved for the drawn legend, per type (px)
LEGEND_H = {"WB": 0, "IP": 60, "ICC-IF": 84, "FC": 84, "IHC": 0}
# IHC reserves a line only when it has something to say — see `ihc_note`.
IHC_NOTE_H = 28

# Left gutter reserved for rotated labels, per type (px). ICC-IF keeps the
# antibody (top) + DAPI (bottom) panels stacked and labels them in this gutter.
LEFT_GUTTER = {"WB": 0, "IP": 0, "ICC-IF": 46, "FC": 0, "IHC": 46}

# ── IHC: several panels of one row, stacked ──────────────────────────────────
#
# An IHC figure is one row per antibody and one column per cell line, each
# panel about twice as wide as it is tall. The gene page shows the HAP1 pellets
# only (owner, 26 Sep 2026) — wild type, knockout and mosaic — and three such
# panels side by side on a square canvas shrink to a strip nobody can read the
# mosaic in, so they are **stacked**, top to bottom in the order they sit in
# the figure, and labelled in the gutter.
#
# Every panel keeps its own pixels and its own scale bar: each is cropped and
# pasted **at its native size**, and only the finished stack is scaled, once.
# Scaling the panels separately to a common width would make three scale bars
# of one printed length mean three different distances.
#
# The labels are what the person typed for that figure (`ihc_default_labels`
# is only what the box starts with), because which column is which is read off
# the figure's headings, and a figure that orders them differently must not be
# labelled by position. None at all is legitimate: some figures stack the
# panels themselves and print "HAP1 WT" inside each one (PPP2R5D, Figure 4),
# and those are cropped as one cell with no gutter.
IHC_GAP = 6


def ihc_default_labels(gene: str, genotype: str = "KO") -> list:
    """What the panel-label box starts with: wild type, the control, mosaic."""
    word = "KD" if (genotype or "").upper() == "KD" else "KO"
    return ["WT", f"{gene} {word}".strip(), "Mosaic"]


def ihc_note(cell_line: str, scale: str) -> str:
    """The one line under an IHC stack, or ``""`` — never a value nobody typed.

    The scale bar's length is printed once in the figure's key, not on the
    panels, so the crop would otherwise carry a bar with no length. It is taken
    from what the person typed; a blank leaves the bar unlabelled rather than
    guessing the 50 µm most figures use."""
    parts = []
    if (cell_line or "").strip():
        parts.append(f"{cell_line.strip()} cell pellets")
    if (scale or "").strip():
        parts.append(f"scale bar {scale.strip()}")
    return " · ".join(parts)

# Legend colours (configurable per spec §8)
GREEN = (0, 158, 76)
MAGENTA = (200, 30, 140)
GREY = (130, 130, 130)


def _font(size: int, bold: bool = False):
    names = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


@dataclass
class Cell:
    """One assigned grid cell → one crop."""
    left: int
    top: int
    right: int
    bottom: int
    app_type: str          # WB | IP | ICC-IF | FC | IHC
    catalogue: str         # authoritative, from the confirmed table
    gene: str = ""
    cell_line: str = ""
    genotype: str = "KO"   # KO | KD  (KD → "knockdown" wording)
    fc_secondary_only: bool = False
    wt_color: tuple = GREEN
    ko_color: tuple = MAGENTA
    # IHC only: the panels of one row, left to right, each (l, t, r, b), and
    # the label for each. The rectangle above is their bounding box.
    panels: list = field(default_factory=list)
    panel_labels: list = field(default_factory=list)
    note: str = ""


def _trim_white(im: Image.Image, keep: int = 3, thresh: int = 245) -> Image.Image:
    """Trim only the outer white margin, leaving ~keep px. Interior whitespace
    (e.g. the gap between blot and Ponceau) is preserved."""
    import numpy as np
    arr = np.asarray(im.convert("RGB"))
    mask = (arr < thresh).any(axis=2)          # True = non-white content
    if not mask.any():
        return im
    ys, xs = mask.any(axis=1), mask.any(axis=0)
    top, bottom = int(ys.argmax()), len(ys) - int(ys[::-1].argmax())
    left, right = int(xs.argmax()), len(xs) - int(xs[::-1].argmax())
    top = max(0, top - keep); left = max(0, left - keep)
    bottom = min(im.height, bottom + keep); right = min(im.width, right + keep)
    return im.crop((left, top, right, bottom))


def _paste_scaled(canvas: Image.Image, content: Image.Image, fill: float,
                  reserve_bottom: int, left_gutter: int = 0) -> tuple:
    avail_w = int((CANVAS - left_gutter) * fill)
    avail_h = int((CANVAS - reserve_bottom) * fill)
    scale = min(avail_w / content.width, avail_h / content.height)
    w, h = max(1, round(content.width * scale)), max(1, round(content.height * scale))
    scaled = content.resize((w, h), Image.LANCZOS)   # only permitted scale op
    x = left_gutter + (CANVAS - left_gutter - w) // 2
    y = (CANVAS - reserve_bottom - h) // 2
    canvas.paste(scaled, (x, y))
    return (x, y, w, h)   # placed-content box, for label alignment


def _draw_vtext(canvas: Image.Image, text: str, cx: float, cy: float,
                font: ImageFont.ImageFont) -> None:
    """Draw text rotated 90° (reads bottom-to-top), centered on (cx, cy)."""
    tmp = Image.new("RGBA", (240, 32), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((0, 0), text, font=font, fill=(0, 0, 0, 255))
    bbox = tmp.getbbox()
    if not bbox:
        return
    rot = tmp.crop(bbox).rotate(90, expand=True)
    canvas.paste(rot, (int(cx - rot.width / 2), int(cy - rot.height / 2)), rot)


def _draw_ip_legend(draw: ImageDraw.ImageDraw, catalogue: str, top: int) -> None:
    f = _font(14)
    lines = ["SM: Starting material",
             "UB: Unbound fraction",
             f"IP: {catalogue} immunoprecipitate"]
    y = top + 6
    for ln in lines:
        draw.text((14, y), ln, font=f, fill=(0, 0, 0))
        y += 17


def _draw_cellline_legend(draw: ImageDraw.ImageDraw, cell: Cell, top: int) -> None:
    f = _font(19)
    sq = 18
    word = "knockdown" if cell.genotype.upper() == "KD" else "knockout"
    rows = [(cell.wt_color, f"{cell.cell_line} wild-type cell line"),
            (cell.ko_color, f"{cell.cell_line} {cell.gene} {word} cell line")]
    if cell.app_type == "FC" and cell.fc_secondary_only:
        rows.append((GREY, "Secondary only control"))
    y = top + 8
    for color, text in rows:
        draw.rectangle([14, y, 14 + sq, y + sq], fill=color)
        draw.text((14 + sq + 10, y + sq // 2), text, font=f, fill=(0, 0, 0), anchor="lm")
        y += sq + 8


def fit_to_canvas(content: Image.Image, app_type: str) -> Image.Image:
    """A finished panel — one that already carries its own labels — on the same
    490x490 canvas ``render_cell`` produces, so it sits beside the cropper's
    figures at the same size. Same three operations and nothing else: trim the
    outer white margin, LANCZOS scale, paste onto white. No legend is drawn,
    since the panel brought its own."""
    content = _trim_white(content.convert("RGB"))
    canvas = Image.new("RGB", (CANVAS, CANVAS), "white")
    _paste_scaled(canvas, content, FILL.get(app_type, 0.90), 0, 0)
    return canvas


def _stack_panels(source: Image.Image, panels: list) -> tuple:
    """``(stack, pieces)`` — the panels cut from `source` and stacked top to
    bottom on white, each at its native size, centred. Crop and paste only; no
    scaling here."""
    cut = [_trim_white(source.crop(tuple(int(v) for v in p)).convert("RGB"))
           for p in panels]
    w = max(c.width for c in cut)
    h = sum(c.height for c in cut) + IHC_GAP * (len(cut) - 1)
    col = Image.new("RGB", (w, h), "white")
    y = 0
    for c in cut:
        col.paste(c, ((w - c.width) // 2, y))
        y += c.height + IHC_GAP
    return col, cut


def _render_ihc(source: Image.Image, cell: Cell) -> Image.Image:
    col, cut = _stack_panels(source, cell.panels)
    reserve = IHC_NOTE_H if cell.note else 0
    # No labels to draw — the figure's panels carry their own — means no gutter.
    gutter = LEFT_GUTTER["IHC"] if cell.panel_labels else 0
    canvas = Image.new("RGB", (CANVAS, CANVAS), "white")
    x, y, w, h = _paste_scaled(canvas, col, FILL["IHC"], reserve, gutter)
    scale = h / col.height
    f = _font(15)
    top = 0
    for c, label in zip(cut, cell.panel_labels):
        mid = y + (top + c.height / 2) * scale
        _draw_vtext(canvas, label, max(gutter / 2, x - 14), mid, f)
        top += c.height + IHC_GAP
    if cell.note:
        draw = ImageDraw.Draw(canvas)
        draw.text((CANVAS / 2, CANVAS - reserve / 2), cell.note, font=_font(15),
                  fill=(0, 0, 0), anchor="mm")
    return canvas


def render_cell(source: Image.Image, cell: Cell) -> Image.Image:
    """Crop one cell from `source` and return its finished 490x490 canvas."""
    if cell.app_type == "IHC":
        return _render_ihc(source, cell)
    content = source.crop((cell.left, cell.top, cell.right, cell.bottom))
    content = _trim_white(content)

    reserve = LEGEND_H[cell.app_type]
    gutter = LEFT_GUTTER[cell.app_type]
    canvas = Image.new("RGB", (CANVAS, CANVAS), "white")
    box = _paste_scaled(canvas, content, FILL[cell.app_type], reserve, gutter)

    # ICC-IF: label the stacked antibody (top half) and DAPI (bottom half) panels
    if cell.app_type == "ICC-IF":
        _, by, _, bh = box
        f = _font(15)
        _draw_vtext(canvas, "antibody", gutter / 2, by + bh * 0.25, f)
        _draw_vtext(canvas, "DAPI",     gutter / 2, by + bh * 0.75, f)

    if reserve:
        draw = ImageDraw.Draw(canvas)
        legend_top = CANVAS - reserve
        if cell.app_type == "IP":
            _draw_ip_legend(draw, cell.catalogue, legend_top)
        else:  # ICC-IF / FC
            _draw_cellline_legend(draw, cell, legend_top)
    return canvas


def filename_for(gene: str, catalogue: str, app_type: str) -> str:
    """Spec §9 naming. Filename uses IF (DB uses ICC-IF); strip asterisks;
    preserve Aviva suffixes and internal spaces.

    A slash becomes a hyphen: DSHB catalogues are clone names (``N258/5``),
    and storage reads a slash as a folder, so the key would have been
    ``CLCN3_N258/`` + ``5_WB.png`` — a file whose own name says catalogue
    ``5``. Mirrored by ``cropFileName`` in ``cropper.html``."""
    type_tag = "IF" if app_type == "ICC-IF" else app_type
    cat = catalogue.replace("*", "").replace("/", "-").replace("\\", "-").strip()
    return f"{gene}_{cat}_{type_tag}.png"
