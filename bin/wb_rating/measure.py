"""Measure published western blot crops and rate them by the owner's rules.

A prototype (3 Oct 2026), kept so the work can carry on in a new session.
The skill `.claude/skills/wb-rating/SKILL.md` says where it stands and what
to try next; the rules it implements are in `guide.html` beside this file.

    python bin/wb_rating/measure.py fetch            # images of the decided figures
    python bin/wb_rating/measure.py fetch --all      # every published WB figure
    python bin/wb_rating/measure.py score            # agreement with the owner, using data/params.json
    python bin/wb_rating/measure.py fit              # refit the thresholds, split-half check
    python bin/wb_rating/measure.py show 4420 1883   # one line per figure: owner, measured, why
    python bin/wb_rating/measure.py draw 4420        # annotated lane profiles -> <cache>/draw/4420.png
    python bin/wb_rating/measure.py rate --all       # measured result for every figure -> <cache>/rated.json

Needs numpy and Pillow only (both in requirements.txt). Images are fetched
with curl, because Cloudflare refuses Python's urllib, into a cache outside
the repo (`--cache`, default `$OGA_WB_CACHE` or `/tmp/oga_wb_rating`). Three
published figures are SVG; `fetch` renders them with Playwright's Chromium
when it is installed and otherwise names them.

**What a measurement is.** The blot is the largest dark-framed box in the top
part of the crop that is not pink (Ponceau). The lanes are equal divisions of
the box, the count chosen from {2, 3, 4, 6} by the contrast between lane
centres and the gaps between them. Each lane is the median darkness across its
middle half, row by row, with a rolling-minimum background taken off. A peak in
the WT lane is a band; its KO value is the KO lane's maximum within 3 px; the
ratio KO/WT says whether it was lost. Only two-lane figures are rated — more
lanes need to know which lane is which (`lane roles needed`).

**What is not measured**: molecular weight (the ladder is not read), lane
labels (KO or KD is a person's flag in the data, `kd`), and anything outside
the main box. Nothing here writes to the site; `rate` writes a file a person
reads, and a judgement reaches the site only through
`manage.py apply_wb_judgements` after the owner has decided it.
"""
from __future__ import annotations

import argparse
import collections
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CODES = {"M": "Supportive", "O": "Supportive, but not selective", "N": "Not supportive"}


# --------------------------------------------------------------------- data

def figures():
    return {f["id"]: f for f in json.loads((DATA / "figures.json").read_text())["figures"]}


def decisions():
    return {d["id"]: d for d in json.loads((DATA / "owner_decisions.json").read_text())["decisions"]}


def params():
    return json.loads((DATA / "params.json").read_text())


def cache_dir(args):
    d = Path(args.cache or os.environ.get("OGA_WB_CACHE") or "/tmp/oga_wb_rating")
    (d / "img").mkdir(parents=True, exist_ok=True)
    return d


def image_path(cache, fid):
    return cache / "img" / f"{fid}.png"


# ------------------------------------------------------------ image reading

def load(path):
    return np.asarray(Image.open(path).convert("RGB")).astype(float)


def runs(mask, minlen):
    """(start, end) of runs of True at least ``minlen`` long."""
    out, n, i = [], len(mask), 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= minlen:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


def find_boxes(a):
    """Rectangles drawn as dark frames, as interior (x0, y0, x1, y1), largest first.

    Two vertical lines of matching extent plus a top *or* bottom line between
    them: requiring both missed 46 of 212 crops whose frame is cut by a label.
    """
    g = a.mean(axis=2)
    H, W = g.shape
    dark = g < 120
    hl = [(y, x0, x1) for y in range(H) for x0, x1 in runs(dark[y], max(20, int(0.08 * W)))]
    vl = [(x, y0, y1) for x in range(W) for y0, y1 in runs(dark[:, x], max(30, int(0.12 * H)))]
    boxes = []
    for x, y0, y1 in vl:
        for x2, y20, y21 in vl:
            if x2 <= x + 15 or abs(y20 - y0) > 8 or abs(y21 - y1) > 8:
                continue
            spans = lambda yy: any(abs(h[0] - yy) <= 4 and h[1] <= x + 4 and h[2] >= x2 - 4 for h in hl)
            if spans(y0) or spans(y1):
                boxes.append((x + 2, max(y0, y20) + 2, x2 - 2, min(y1, y21) - 2))
    uniq = []
    for b in sorted(boxes, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
        if all(max(abs(b[k] - u[k]) for k in range(4)) > 6 for u in uniq):
            uniq.append(b)
    return uniq


def is_ponceau(a, box):
    x0, y0, x1, y1 = box
    r = a[y0:y1, x0:x1]
    return float((r[..., 0] - r[..., 1]).mean()) > 25


def main_box(a):
    bx = [b for b in find_boxes(a)
          if not is_ponceau(a, b) and (b[3] - b[1]) > 40 and (b[2] - b[0]) > 30]
    if not bx:
        return None
    top = [b for b in bx if b[1] < 0.6 * a.shape[0]] or bx
    return max(top, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))


def smooth(v, k):
    if k <= 1:
        return v
    return np.convolve(np.pad(v, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")


def lane_count(dk):
    col = smooth(dk.mean(axis=0), 3)
    W = len(col)
    best = (0, 2)
    for n in (2, 3, 4, 6):
        w = W / n
        if w < 12:
            continue
        centres = [col[int(w * (i + .5)) - 2:int(w * (i + .5)) + 3].mean() for i in range(n)]
        bounds = [col[max(0, int(w * i) - 2):int(w * i) + 3].mean() for i in range(1, n)]
        score = (np.mean(centres) - np.mean(bounds)) / (np.mean(centres) + 1e-6)
        if score > best[0] + 0.08:
            best = (score, n)
    return best[1]


def profiles(a, box, n=None):
    """Background-subtracted darkness per lane, top to bottom."""
    x0, y0, x1, y1 = box
    g = a[y0:y1, x0:x1].mean(axis=2)
    dk = 255 - g
    n = n or lane_count(dk)
    w = (x1 - x0) / n
    prof = []
    for i in range(n):
        c0, c1 = int(w * i + w * .25), int(w * (i + 1) - w * .25)
        prof.append(np.median(dk[:, c0:max(c1, c0 + 1)], axis=1))
    prof = np.array(prof)
    k = max(15, int(0.08 * (y1 - y0)))
    bg = []
    for p in prof:
        pad = np.pad(p, (k, k), mode="edge")
        bg.append(smooth(np.array([pad[i:i + 2 * k + 1].min() for i in range(len(p))]), k))
    return np.clip(prof - np.array(bg), 0, None), n, float((g < 12).mean())


def peaks(p, minh=6.0):
    """Local maxima with height and prominence; peaks within 3 px merged."""
    s = smooth(p, 3)
    out = []
    for i in range(2, len(s) - 2):
        if s[i] >= s[i - 1] and s[i] > s[i + 1] and s[i] >= minh:
            l, lmin = i, s[i]
            while l > 0 and s[l - 1] <= s[i]:
                l -= 1
                lmin = min(lmin, s[l])
            r, rmin = i, s[i]
            while r < len(s) - 1 and s[r + 1] <= s[i]:
                r += 1
                rmin = min(rmin, s[r])
            out.append({"y": i, "h": float(s[i]), "prom": float(s[i] - max(lmin, rmin))})
    merged = []
    for pk in out:
        if merged and pk["y"] - merged[-1]["y"] < 3:
            if pk["h"] > merged[-1]["h"]:
                merged[-1] = pk
        else:
            merged.append(pk)
    return merged


def measure(path):
    a = load(path)
    b = main_box(a)
    if b is None:
        return {"error": "no blot frame found"}
    pr, n, sat = profiles(a, b)
    if n != 2:
        return {"error": f"{n} lanes: lane roles needed", "lanes": n, "box": b}
    wt, ko = smooth(pr[0], 3), smooth(pr[1], 3)
    bands = []
    for p in peaks(pr[0]):
        y = p["y"]
        k = float(ko[max(0, y - 3):y + 4].max())
        bands.append({"y": y, "wt": p["h"], "prom": p["prom"], "ko": k, "ratio": k / max(p["h"], 1e-6)})
    ko_only = [p for p in peaks(pr[1]) if wt[max(0, p["y"] - 3):p["y"] + 4].max() < 0.35 * p["h"]]
    return {"bands": bands, "ko_only": ko_only, "H": len(wt), "sat": sat, "box": list(b)}


# ------------------------------------------------------------- the rules

def classify(m, P, kd=False):
    """``(code, rule, why)`` — code M/O/N, or '?' when the figure was not measured.

    The rules of guide.html, in its order, with each judgement a threshold
    in ``P`` (data/params.json):
      lost        KO/WT at or below this is a lost band
      kept        above this the band stayed; between the two is a partial reduction
      drown_prom  a lost band whose prominence is below this share of its height is drowned (b)
      adj_px      "directly beside" is within this share of the box height (c)
      adj_ratio   ...and "clearly stronger" is at least this many times the lost band (c)
      faint       a band that stays below this share of the lost band is faint (Q5)
      resid       a KO residual above this share of WT is clear (KO only)
      minh        WT peaks below this height are not bands at all
    """
    if "error" in m:
        return ("?", "x", m["error"])
    B = [b for b in m["bands"] if b["wt"] >= P["minh"]]
    if not B:
        return ("N", "a", "no band in WT")
    lost = [b for b in B if b["ratio"] <= P["lost"]]
    # A band that drops to a smaller size in the KO counts as lost (truncated protein).
    for b in B:
        if b not in lost and b["ratio"] <= P["kept"] and any(
                0 < k["y"] - b["y"] <= P["adj_px"] * m["H"] * 2 for k in m["ko_only"]):
            lost.append(b)
    partial = [b for b in B if P["lost"] < b["ratio"] <= P["kept"] and b not in lost]
    if not lost:
        if partial and not kd:
            return ("N", "d", "only an ambiguous reduction")
        if not partial:
            return ("N", "a", "no band lost")
        lost = partial  # a knockdown's reduction is expected to be partial
    T = max(lost, key=lambda b: b["wt"])  # the target: strongest lost band
    if T["prom"] < P["drown_prom"] * T["wt"]:
        return ("N", "b", f"lost band at {T['y']} not resolved (prominence {T['prom']:.0f}/{T['wt']:.0f})")
    kept = [b for b in B if b not in lost]
    adj = [b for b in kept if abs(b["y"] - T["y"]) <= P["adj_px"] * m["H"] and b["wt"] >= P["adj_ratio"] * T["wt"]]
    if adj:
        return ("N", "c", f"stronger band at {adj[0]['y']} beside lost band at {T['y']}")
    clear = [b for b in kept if b["wt"] >= P["faint"] * T["wt"]]
    resid = (not kd) and T["ratio"] > P["resid"]
    if clear or resid:
        return ("O", "O", f"{len(clear)} clear band(s) stay"
                + (f"; KO residual {100 * T['ratio']:.0f}%" if resid else ""))
    return ("M", "M", "only faint bands stay")


# --------------------------------------------------------------- commands

def _ids(args, figs):
    if getattr(args, "all", False):
        return sorted(figs)
    if getattr(args, "ids", None):
        return [int(i) for i in args.ids]
    return sorted(decisions())


def cmd_fetch(args):
    cache, figs = cache_dir(args), figures()
    got = skipped = 0
    svg = []
    for fid in _ids(args, figs):
        out = image_path(cache, fid)
        if out.exists():
            skipped += 1
            continue
        url = figs[fid]["img"]
        raw = cache / "img" / f"{fid}.download"
        r = subprocess.run(["curl", "-sSfL", "--max-time", "60", "-o", str(raw), url])
        if r.returncode:
            print(f"  {fid}: could not fetch {url}")
            continue
        if raw.read_bytes()[:256].lstrip().startswith((b"<svg", b"<?xml")):
            if _render_svg(raw, out):
                raw.unlink()
                got += 1
            else:
                svg.append(fid)
            continue
        raw.rename(out)
        got += 1
    print(f"{got} fetched, {skipped} already in {cache / 'img'}")
    if svg:
        print(f"{len(svg)} SVG not rendered (needs playwright): {svg}")


def _render_svg(src, out):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    svg = src.with_suffix(".svg")
    shutil.copy(src, svg)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.goto(svg.as_uri())
        page.locator("svg").first.screenshot(path=str(out))
        b.close()
    svg.unlink()
    return True


def _measured(args, ids):
    cache = cache_dir(args)
    out = {}
    for fid in ids:
        p = image_path(cache, fid)
        out[fid] = measure(p) if p.exists() else {"error": "image not fetched"}
    return out


def _agreement(P, M, truth, kd, ids):
    pred = {i: classify(M[i], P, kd[i])[0] for i in ids}
    exact = sum(pred[i] == truth[i] for i in ids)
    sup = lambda c: c in "MO"
    either = sum(sup(pred[i]) == sup(truth[i]) for i in ids)
    return exact, either, pred


def cmd_score(args, P=None):
    dec = decisions()
    ids = sorted(dec)
    M = _measured(args, ids)
    ok = [i for i in ids if "error" not in M[i]]
    why = collections.Counter(M[i]["error"] for i in ids if "error" in M[i])
    truth = {i: dec[i]["d"] for i in ids}
    kd = {i: dec[i]["kd"] for i in ids}
    P = P or params()
    exact, either, pred = _agreement(P, M, truth, kd, ok)
    print(f"{len(ids)} owner decisions; {len(ok)} measured; not measured: {dict(why)}")
    print(f"exact agreement            {exact}/{len(ok)} ({100 * exact / max(len(ok), 1):.0f}%)")
    print(f"supportive-or-not          {either}/{len(ok)} ({100 * either / max(len(ok), 1):.0f}%)")
    cm = collections.Counter((truth[i], pred[i]) for i in ok)
    print("\nowner \\ measured    M    O    N")
    for t in "MON":
        print(f"  {t}               " + "".join(f"{cm[(t, p)]:5d}" for p in "MON"))
    return M


def cmd_fit(args):
    dec = decisions()
    ids = sorted(dec)
    M = _measured(args, ids)
    ok = [i for i in ids if "error" not in M[i]]
    truth = {i: dec[i]["d"] for i in ids}
    kd = {i: dec[i]["kd"] for i in ids}
    grid = dict(lost=[.35, .5, .6], kept=[.7, .8, .9], drown_prom=[.15, .25, .35, .5],
                adj_px=[.03, .05, .08], adj_ratio=[1.2, 1.5, 2.0], faint=[.15, .25, .35, .5],
                resid=[.1, .2, .3], minh=[8, 12, 20])
    keys = list(grid)
    combos = [dict(zip(keys, c)) for c in itertools.product(*grid.values())]
    combos = [P for P in combos if P["lost"] < P["kept"]]

    def best(sub):
        return max(((_agreement(P, M, truth, kd, sub)[0], P) for P in combos), key=lambda t: t[0])

    rng = random.Random(1)
    shuffled = ok[:]
    rng.shuffle(shuffled)
    A, B = shuffled[::2], shuffled[1::2]
    print(f"grid of {len(combos)} threshold sets over {len(ok)} measured decisions")
    sA, PA = best(A)
    sB, PB = best(B)
    print(f"fit on half A {sA}/{len(A)}, tested on B {_agreement(PA, M, truth, kd, B)[0]}/{len(B)}")
    print(f"fit on half B {sB}/{len(B)}, tested on A {_agreement(PB, M, truth, kd, A)[0]}/{len(A)}")
    s, P = best(ok)
    print(f"fit on all    {s}/{len(ok)}  {P}")
    if args.save:
        (DATA / "params.json").write_text(json.dumps(P, indent=1) + "\n")
        print(f"saved to {DATA / 'params.json'}")
    else:
        print("not saved (add --save to replace data/params.json)")


def cmd_show(args):
    figs, dec, P = figures(), decisions(), params()
    ids = [int(i) for i in args.ids] if args.ids else sorted(dec)
    M = _measured(args, ids)
    for i in ids:
        f = figs[i]
        kd = dec[i]["kd"] if i in dec else bool(f.get("kd"))
        code, rule, why = classify(M[i], P, kd)
        owner = dec[i]["d"] if i in dec else "-"
        if args.disagree and (owner == "-" or owner == code):
            continue
        print(f"{i:>5} {f['gene']:<9} {f['cat']:<20} owner {owner}  measured {code} ({rule}) {why}")


def cmd_draw(args):
    cache, figs = cache_dir(args), figures()
    P = params()
    (cache / "draw").mkdir(exist_ok=True)
    for i in [int(x) for x in args.ids]:
        p = image_path(cache, i)
        if not p.exists():
            print(f"{i}: image not fetched")
            continue
        a = load(p)
        m = measure(p)
        im = Image.open(p).convert("RGB")
        d = ImageDraw.Draw(im)
        if "box" in m:
            x0, y0, x1, y1 = m["box"]
            d.rectangle([x0, y0, x1, y1], outline=(0, 120, 255))
        panel = None
        if "error" not in m:
            pr, n, _ = profiles(a, tuple(m["box"]), 2)
            h = len(pr[0])
            wpx = 220
            panel = Image.new("RGB", (wpx, h), "white")
            pd = ImageDraw.Draw(panel)
            top = max(float(pr.max()), 1.0)
            for lane, colour in ((0, (0, 0, 0)), (1, (220, 0, 0))):
                s = smooth(pr[lane], 3)
                pts = [(int(s[y] / top * (wpx - 10)), y) for y in range(h)]
                pd.line(pts, fill=colour)
            for b in m["bands"]:
                c = (0, 160, 0) if b["ratio"] <= P["lost"] else (150, 150, 150)
                pd.line([(0, b["y"]), (wpx, b["y"])], fill=c)
                pd.text((wpx - 60, max(0, b["y"] - 10)), f"{b['ratio']:.2f}", fill=c)
        code, rule, why = classify(m, P, bool(figs[i].get("kd")))
        W = im.width + (panel.width if panel else 0)
        out = Image.new("RGB", (W, max(im.height, panel.height if panel else 0) + 40), "white")
        out.paste(im, (0, 0))
        if panel:
            out.paste(panel, (im.width, m["box"][1]))
        ImageDraw.Draw(out).text((4, out.height - 34),
                                 f"{figs[i]['gene']} {figs[i]['cat']}: measured {code} ({rule}) {why}",
                                 fill=(0, 0, 0))
        dest = cache / "draw" / f"{i}.png"
        out.save(dest)
        print(f"{i}: {dest}  (black WT, red KO; green line = band read as lost, number = KO/WT)")


def cmd_rate(args):
    figs, dec, P = figures(), decisions(), params()
    ids = _ids(args, figs)
    M = _measured(args, ids)
    out = []
    for i in ids:
        code, rule, why = classify(M[i], P, bool(dec[i]["kd"] if i in dec else figs[i].get("kd")))
        out.append({"id": i, "gene": figs[i]["gene"], "cat": figs[i]["cat"], "now": figs[i]["now"],
                    "owner": dec[i]["d"] if i in dec else None, "measured": code, "rule": rule, "why": why})
    dest = cache_dir(args) / "rated.json"
    dest.write_text(json.dumps(out, indent=1))
    print(f"{len(out)} rated: {dict(collections.Counter(r['measured'] for r in out))} -> {dest}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", help="image cache (default $OGA_WB_CACHE or /tmp/oga_wb_rating)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--all", action="store_true")
    f.add_argument("ids", nargs="*")
    sub.add_parser("score")
    fi = sub.add_parser("fit")
    fi.add_argument("--save", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("--disagree", action="store_true")
    s.add_argument("ids", nargs="*")
    d = sub.add_parser("draw")
    d.add_argument("ids", nargs="+")
    r = sub.add_parser("rate")
    r.add_argument("--all", action="store_true")
    r.add_argument("ids", nargs="*")
    args = ap.parse_args(argv)
    {"fetch": cmd_fetch, "score": cmd_score, "fit": cmd_fit, "show": cmd_show,
     "draw": cmd_draw, "rate": cmd_rate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
