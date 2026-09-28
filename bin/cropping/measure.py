#!/usr/bin/env python3
"""Fit each figure's grid from its white gaps, and draw it to be checked.

    python bin/cropping/measure.py PLAN.json            # fit every figure
    python bin/cropping/measure.py PLAN.json --gaps WB1 # print one figure's gaps

Reads the figure files only (never writes them) and writes each figure's
``fit`` back into the plan, plus ``overlays/<name>.png`` and ``overlays.png``
(all of them tiled) in the plan's folder. Look at every overlay before
building: a line through a title or a band is a wrong crop.

A figure in the plan:

    {"name": "WB2", "file": "PARP1/PARP1_WB_HAP1 KO - v2-02.png", "app": "WB",
     "bands": [{"x0": 0, "x1": 3300, "n": 7}, {"x0": 0, "x1": 3300, "n": 8}],
     "order": ["9546", "AFFN-PARP1-17B10", ...]}

``bands`` is one entry per row of panels: the x-range the row may use and how
many panels it holds. Anything the fit gets wrong is set by hand and wins:
``top``, ``bottom``, ``dividers`` (image pixels), ``y_range`` (where the rows
may be looked for — above a caption, below a part heading), or a row's own
``left`` / ``right`` / ``splits``. ``assign`` (``["0_2", "1_1"]``, row_panel
from zero) ticks only those panels — the rest, an IHC figure's H&E and
secondary-only columns, stay unassigned — and ``whole_only`` marks a figure
that goes on the IHC page uncropped, so it has no grid at all.

Two rules, each chosen because the other failed on PARP1:
  * columns on WB, IP and ICC-IF are cut at the **widest** gaps — the nearest
    gap to an even spacing cut through a wide title and an "<IP" marker;
  * columns on FC, and every row divider, go at the gap **nearest an even
    spacing** — a histogram's rotated axis label has a wider gap beside it
    than the gap between two histograms.
"""
import argparse
import sys

import numpy as np
from PIL import Image, ImageDraw

from common import load_plan, save_plan, work_dir

WHITE = 235      # a pixel darker than this is content
MARGIN = 15      # slack kept beyond the content; the cropper trims white anyway


def mask(path):
    return np.asarray(Image.open(path).convert("L")) < WHITE


def runs(profile, minlen):
    out, start = [], None
    for i, v in enumerate(list(profile) + [1]):
        if v == 0 and start is None:
            start = i
        elif v != 0 and start is not None:
            if i - start >= minlen:
                out.append((start, i - 1))
            start = None
    return out


def gaps(m, x0, x1, y0, y1, axis, minlen=5):
    """White runs along y (axis 'y') or x (axis 'x') inside the box."""
    box = m[y0:y1, x0:x1]
    prof = box.sum(1) if axis == "y" else box.sum(0)
    off = y0 if axis == "y" else x0
    return [(a + off, b + off) for a, b in runs(prof, minlen)]


def extent(m, x0, x1, y0, y1, axis):
    box = m[y0:y1, x0:x1]
    nz = np.nonzero(box.sum(1) if axis == "y" else box.sum(0))[0]
    if not len(nz):
        return None
    off = y0 if axis == "y" else x0
    return nz[0] + off, nz[-1] + off


def nearest_even(gs, lo, hi, n, what):
    pitch = (hi - lo) / n
    cuts = []
    for k in range(1, n):
        p = lo + k * pitch
        best = min(gs, key=lambda g: abs((g[0] + g[1]) / 2 - p) - 0.5 * (g[1] - g[0]))
        if abs((best[0] + best[1]) / 2 - p) > 0.35 * pitch:
            raise SystemExit(f"{what}: no white gap near {p:.0f} — set it by hand")
        cuts.append(round((best[0] + best[1]) / 2))
    return cuts


def widest(gs, n):
    return sorted(round((a + b) / 2) for a, b in sorted(gs, key=lambda g: g[1] - g[0], reverse=True)[:n - 1])


def fit_figure(plan, fig):
    path = plan["_dir"] / fig["file"]
    m = mask(path)
    H, W = m.shape
    bands = fig["bands"]
    xs0 = min(b.get("x0", 0) for b in bands)
    xs1 = max(b.get("x1", W) for b in bands)
    y0, y1 = fig.get("y_range", [0, H])
    span = extent(m, xs0, xs1, y0, y1, "y")
    if span is None:
        raise SystemExit(f"{fig['name']}: nothing drawn in y_range {y0}–{y1}")
    top = fig.get("top", max(y0, span[0] - MARGIN))
    bottom = fig.get("bottom", min(y1 - 1, span[1] + MARGIN))
    if "dividers" in fig:
        dividers = fig["dividers"]
    elif len(bands) > 1:
        dividers = nearest_even(gaps(m, xs0, xs1, span[0], span[1] + 1, "y"),
                                span[0], span[1], len(bands), f"{fig['name']} rows")
    else:
        dividers = []
    ys = [top] + dividers + [bottom]
    rows = []
    for i, b in enumerate(bands):
        x0, x1 = b.get("x0", 0), b.get("x1", W)
        ra, rb = int(ys[i]), int(ys[i + 1])
        cs, ce = extent(m, x0, x1, ra, rb, "x")
        left = b.get("left", max(x0, cs - MARGIN))
        right = b.get("right", min(x1 - 1 if x1 < W else W - 1, ce + MARGIN))
        if "splits" in b:
            splits = b["splits"]
        elif b["n"] == 1:
            splits = []
        else:
            gs = gaps(m, cs, ce + 1, ra, rb, "x")
            rule = b.get("split_rule", "even" if fig["app"] == "FC" else "widest")
            splits = (nearest_even(gs, cs, ce, b["n"], f"{fig['name']} row {i + 1}")
                      if rule == "even" else widest(gs, b["n"]))
        if len(splits) != b["n"] - 1:
            raise SystemExit(f"{fig['name']} row {i + 1}: found {len(splits) + 1} panels, "
                             f"plan says {b['n']}")
        rows.append({"left": int(left), "right": int(right), "splits": [int(s) for s in splits]})
    ticked = len(fig["assign"]) if "assign" in fig else sum(b["n"] for b in bands)
    if ticked != len(fig["order"]):
        raise SystemExit(f"{fig['name']}: {ticked} panels to assign but "
                         f"{len(fig['order'])} antibodies in its order")
    fig["fit"] = {"top": int(top), "bottom": int(bottom),
                  "dividers": [int(d) for d in dividers], "rows": rows}
    return path, ys


def overlay(plan, fig, path, ys):
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)
    for i, r in enumerate(fig["fit"]["rows"]):
        a, b = ys[i], ys[i + 1]
        for x in [r["left"]] + r["splits"] + [r["right"]]:
            d.line([(x, a), (x, b)], fill=(0, 0, 255), width=6)
        for y in (a, b):
            d.line([(r["left"], y), (r["right"], y)], fill=(255, 120, 0), width=6)
    im.thumbnail((1650, 1275))
    out = work_dir(plan, "overlays") / f"{fig['name']}.png"
    im.save(out)
    return out


def tile(paths, dest):
    ims = [Image.open(p) for p in paths]
    w = max(i.size[0] for i in ims)
    h = max(i.size[1] for i in ims)
    cols = 2
    sheet = Image.new("RGB", (w * cols, h * (-(-len(ims) // cols))), "white")
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % cols) * w, (i // cols) * h))
    sheet.thumbnail((2000, 2600))
    sheet.save(dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("--gaps", metavar="FIGURE", help="print this figure's white gaps and stop")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    if opts.gaps:
        fig = next(f for f in plan["figures"] if f["name"] == opts.gaps)
        m = mask(plan["_dir"] / fig["file"])
        H, W = m.shape
        print(f"{fig['file']} is {W}×{H}")
        print("rows (y):", gaps(m, 0, W, 0, H, "y", 8))
        print("columns (x):", gaps(m, 0, W, 0, H, "x", 8))
        return
    made = []
    for fig in plan["figures"]:
        if fig.get("whole_only"):
            print(f"{fig['name']:6} whole figure only — no grid")
            continue
        path, ys = fit_figure(plan, fig)
        made.append(overlay(plan, fig, path, ys))
        f = fig["fit"]
        print(f"{fig['name']:6} top {f['top']} bottom {f['bottom']} dividers {f['dividers']} "
              + " | ".join(f"L{r['left']} R{r['right']} {r['splits']}" for r in f["rows"]))
    tile(made, plan["_dir"] / "overlays.png")
    save_plan(plan)
    print(f"{sum(len(f['order']) for f in plan['figures'])} panels; overlays in "
          f"{plan['_dir'] / 'overlays'} — look at every one before building.")


if __name__ == "__main__":
    sys.exit(main())
