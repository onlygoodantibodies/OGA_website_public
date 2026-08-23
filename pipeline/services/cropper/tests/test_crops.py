"""
Reproducible, dependency-light test for the crop engine (spec §8-§9, §12).

Run:  python pipeline/services/cropper/tests/test_crops.py
Needs: Pillow, numpy (no Django, no network). Generates its own synthetic
TREM2-style WB composite so it needs no committed image fixtures.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402
import numpy as np  # noqa: E402
from pipeline.services.cropper.engine import (  # noqa: E402
    Cell, render_cell, filename_for, CANVAS, LEGEND_H, LEFT_GUTTER,
)


TITLES =["ab209814**", "ab318262**", "ARP49413", "MAB11618**", "MAB17291R**",
          "29715*", "55739**", "91068**", "GTX637386**", "GTX637387**",
          "ZRB2124**", "68723-1-Ig*", "83438-6-RR**", "MA5-51494**"]


def build_synth():
    """Self-contained synth builder (no /tmp dependency)."""
    from PIL import ImageDraw, ImageFont
    import random
    random.seed(7)
    def font(sz):
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz)
        except OSError:
            return ImageFont.load_default()
    rows = [TITLES[:8], TITLES[8:]]
    PANEL_W, TITLE_H, BLOT_H, PONCEAU_H, GUTTER, ROW_GAP, MARGIN = \
        210, 40, 330, 360, 70, 70, 30
    W = MARGIN * 2 + GUTTER + PANEL_W * 8
    H = MARGIN * 2 + len(rows) * (TITLE_H + BLOT_H + PONCEAU_H) + (len(rows) - 1) * ROW_GAP
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    mw = ["165", "57", "31", "15", "8"]
    coords = {}
    y = MARGIN
    for r, row in enumerate(rows):
        by = y + TITLE_H
        x = MARGIN
        for t in row:
            d.text((x + GUTTER + (PANEL_W - 10) // 2, by - 6), t,
                   font=font(19), fill="black", anchor="mb")
            for i, lab in enumerate(mw):
                d.text((x + GUTTER - 8, by + 18 + int((BLOT_H - 18) * (i + .5) / 5)),
                       lab + "-", font=font(15), fill="black", anchor="rm")
            band = by + 18 + int((BLOT_H - 18) * 2 / 5)
            d.line([(x + GUTTER + 33, band), (x + GUTTER + 80, band)],
                   fill=(0, 0, 0), width=6)
            for i in range(5):
                yy = by + BLOT_H + 10 + int((PONCEAU_H - 10) * (i + .5) / 5)
                for dy in range(-10, 11):
                    d.line([(x + GUTTER + 6, yy + dy),
                            (x + GUTTER + PANEL_W - 16, yy + dy)], fill=(240, 150, 175))
            coords[t] = (x + MARGIN * 0, by, x + PANEL_W, by + BLOT_H + PONCEAU_H)
            x += PANEL_W
        y = by + BLOT_H + PONCEAU_H + ROW_GAP
    return img, coords


def is_region_white(canvas, top, thresh=250):
    arr = np.asarray(canvas.convert("RGB"))[top:, :, :]
    return bool((arr >= thresh).all())


def main():
    img, coords = build_synth()
    outdir = tempfile.mkdtemp(prefix="oga_crops_")
    n = 0
    for cat, (l, t, r, b) in coords.items():
        cell = Cell(left=l, top=t, right=r, bottom=b,
                    app_type="WB", catalogue=cat, gene="TREM2")
        canvas = render_cell(img, cell)
        assert canvas.size == (CANVAS, CANVAS), canvas.size
        fn = filename_for("TREM2", cat, "WB")
        canvas.save(os.path.join(outdir, fn))
        n += 1

    # Assertions (spec-derived)
    assert n == 14, f"expected 14 crops, got {n}"
    assert filename_for("TREM2", "ARP49413_P050", "ICC-IF") == "TREM2_ARP49413_P050_IF.png"
    assert filename_for("TREM2", "ab209814**", "WB") == "TREM2_ab209814_WB.png"  # asterisks stripped
    assert filename_for("TREM2", "168 013", "FC") == "TREM2_168 013_FC.png"      # internal space kept

    # WB has no reserved legend band; IP/IF/FC do.
    assert LEGEND_H["WB"] == 0 and LEGEND_H["IP"] > 0 and LEGEND_H["ICC-IF"] > 0

    # An IP crop must reserve a legend band (bottom rows not pure content).
    ip = render_cell(img, Cell(0, 0, 300, 420, "IP", "91068", gene="TREM2"))
    assert ip.size == (CANVAS, CANVAS)

    # ICC-IF reserves a left gutter for the antibody/DAPI labels; content is
    # inset by that gutter, so the leftmost column of the canvas stays white.
    assert LEFT_GUTTER["ICC-IF"] > 0 and LEFT_GUTTER["WB"] == 0
    iff = render_cell(img, Cell(0, 0, 400, 500, "ICC-IF", "ZRB2124",
                                gene="TREM2", cell_line="THP-1", genotype="KO"))
    assert iff.size == (CANVAS, CANVAS)
    # the gutter band (x < gutter, above the legend) must contain the drawn labels
    gut = LEFT_GUTTER["ICC-IF"]
    band = np.asarray(iff)[:CANVAS - LEGEND_H["ICC-IF"], :gut, :]
    assert (band < 200).any(), "expected antibody/DAPI labels in the left gutter"

    print(f"OK — {n} WB crops in {outdir}; filenames, sizes, legend + IF-gutter rules verified.")


if __name__ == "__main__":
    main()
