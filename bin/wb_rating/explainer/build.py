"""Build the measurement explainer page (published 3 Oct 2026 as
https://claude.ai/artifact/DvM1jv5UKCPR7nhfTuvmij).

    python bin/wb_rating/measure.py fetch          # the decided blots' images first
    python bin/wb_rating/explainer/build.py out.html

For every decided blot the prototype can read, it records each step of the
measurement (raw lane darkness, background floor, the subtracted profile, the
bands) and the signal-and-noise numbers below, and embeds them with a
downscaled image in ``template.html``. The page re-implements ``classify`` in
JavaScript to draw each blot's path through the rules, so ``build`` refuses
to write a page whose copy gives a different result from ``classify`` for any
blot; nothing else would notice the two drifting apart.

The signal-and-noise numbers are not used by the rules. They are candidates,
scored on the page against the owner's decisions:
  T_area_share      target window (±3% of box height) ÷ all WT lane signal
  lane_lost_share   Σ max(WT − KO, 0) ÷ Σ WT, whole lane
  local_lost_share  the same within ±10% of box height around the target
  local_T_share     target window ÷ WT signal within ±10%
  T_vs_max_kept     target height ÷ strongest band not lost (99 = none stays)
  n_kept_stronger / n_kept_clear, T_over_bg (target ÷ floor under it),
  bg_level (mean WT floor), nbands
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import measure as W  # noqa: E402

CACHE = Path(__import__("os").environ.get("OGA_WB_CACHE") or "/tmp/oga_wb_rating") / "img"


def full(path):
    """Every intermediate of ``measure`` for a two-lane blot, or None."""
    a = W.load(path)
    b = W.main_box(a)
    if b is None:
        return None
    x0, y0, x1, y1 = b
    dk = 255 - a[y0:y1, x0:x1].mean(axis=2)
    if W.lane_count(dk) != 2:
        return None
    w = (x1 - x0) / 2
    raw = np.array([np.median(dk[:, int(w * i + w * .25):max(int(w * (i + 1) - w * .25), int(w * i + w * .25) + 1)], axis=1)
                    for i in range(2)])
    k = max(15, int(0.08 * (y1 - y0)))
    bg = []
    for p in raw:
        pad = np.pad(p, (k, k), mode="edge")
        bg.append(W.smooth(np.array([pad[i:i + 2 * k + 1].min() for i in range(len(p))]), k))
    bg = np.array(bg)
    return dict(box=b, raw=raw, bg=bg, pr=np.clip(raw - bg, 0, None), m=W.measure(path))


def signal_numbers(f, P, kd):
    m, pr, bg = f["m"], f["pr"], f["bg"]
    wt, ko = W.smooth(pr[0], 3), W.smooth(pr[1], 3)
    H = len(wt)
    B = [b for b in m["bands"] if b["wt"] >= P["minh"]]
    A = max(float(wt.sum()), 1e-6)
    out = dict(H=H, nbands=len(B), lane_lost_share=float(np.clip(wt - ko, 0, None).sum() / A),
               bg_level=float(bg[0].mean()))
    lost = [b for b in B if b["ratio"] <= P["lost"]]
    if not lost:
        return out
    T = max(lost, key=lambda b: b["wt"])
    kept = [b for b in B if b not in lost]
    mx = max([b["wt"] for b in kept], default=0)
    win, lw = max(4, int(0.03 * H)), int(0.10 * H)
    lo, hi = max(0, T["y"] - win), min(H, T["y"] + win + 1)
    llo, lhi = max(0, T["y"] - lw), min(H, T["y"] + lw + 1)
    loc = wt[llo:lhi]
    out.update(T_y=T["y"], T_h=T["wt"], T_ratio=T["ratio"], T_prom_frac=T["prom"] / T["wt"],
               T_vs_max_kept=T["wt"] / mx if mx else 99,
               n_kept_stronger=sum(b["wt"] > T["wt"] for b in kept),
               n_kept_clear=sum(b["wt"] >= P["faint"] * T["wt"] for b in kept),
               T_area_share=float(wt[lo:hi].sum() / A),
               local_lost_share=float(np.clip(loc - ko[llo:lhi], 0, None).sum() / max(loc.sum(), 1e-6)),
               local_T_share=float(wt[lo:hi].sum() / max(loc.sum(), 1e-6)),
               T_over_bg=T["wt"] / max(float(bg[0][T["y"]]), 1))
    return out


def shifted(m, P):
    B = [b for b in m["bands"] if b["wt"] >= P["minh"]]
    return [b["y"] for b in B if P["lost"] < b["ratio"] <= P["kept"] and any(
        0 < k["y"] - b["y"] <= P["adj_px"] * m["H"] * 2 for k in m["ko_only"])]


def build(dest):
    dec, figs, P = W.decisions(), W.figures(), W.params()
    r1 = lambda v, d=1: [round(float(x), d) for x in v]
    blots = []
    for fid, d in sorted(dec.items()):
        p = CACHE / f"{fid}.png"
        f = full(p) if p.exists() else None
        if f is None:
            continue
        m = f["m"]
        code, rule, why = W.classify(m, P, d["kd"])
        im = Image.open(p).convert("RGB")
        s = min(1.0, 380 / im.width)
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        blots.append(dict(
            id=fid, gene=figs[fid]["gene"], cat=figs[fid]["cat"], sup=figs[fid]["sup"], kd=d["kd"],
            owner=d["d"], measured=code, rule=rule, why=why,
            img="data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
            iw=im.width, ih=im.height, s=s, box=list(f["box"]),
            raw=[r1(f["raw"][0]), r1(f["raw"][1])], bg=[r1(f["bg"][0]), r1(f["bg"][1])],
            pr=[r1(W.smooth(f["pr"][0], 3)), r1(W.smooth(f["pr"][1], 3))],
            # full precision: a ratio of 0.10006 must stay above a 0.10 threshold
            bands=[{k: round(v, 5) if isinstance(v, float) else v for k, v in b.items()} for b in m["bands"]],
            ko_only=[{k: round(v, 3) if isinstance(v, float) else v for k, v in b.items()} for b in m["ko_only"]],
            shifted=shifted(m, P),
            mt={k: round(v, 4) if isinstance(v, float) else v for k, v in signal_numbers(f, P, d["kd"]).items()}))
    data = json.dumps(dict(params=P, blots=blots), separators=(",", ":"))
    _check_rules_agree(data)
    page = (HERE / "template.html").read_text().replace("__DATA__", data.replace("</", "<\\/"))
    Path(dest).write_text(page)
    print(f"{len(blots)} blots -> {dest} ({len(page) // 1024} KB)")


def _check_rules_agree(data):
    """The page's JavaScript copy of the rules must give classify's answer for every blot."""
    import re
    import tempfile
    js = re.search(r"<script>\n(.*)</script>", (HERE / "template.html").read_text(), re.S).group(1)
    code = js[js.index("function lostBands"):js.index("function renderPath")]
    with tempfile.TemporaryDirectory() as d:
        Path(d, "data.json").write_text(data)
        Path(d, "chk.js").write_text(
            "const D=JSON.parse(require('fs').readFileSync(__dirname+'/data.json','utf8'));const P=D.params;\n"
            + code + "\nconst bad=D.blots.filter(b=>rulePath(b).code!==b.measured).map(b=>b.id);"
            "if(bad.length){console.log('page rules disagree with classify on '+bad.join(', '));process.exit(1)}")
        r = subprocess.run(["node", str(Path(d, "chk.js"))], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(r.stdout or r.stderr)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "blot-measurement.html")
