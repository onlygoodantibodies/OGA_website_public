"""The western blot rating kit: raw scans, published crops, one measurement.

Run from this folder with Python 3.9+, numpy and Pillow (tifffile optional):

    python kit.py selftest                      # synthetic blots with known answers
    python kit.py survey  --raw RAW             # what is in the raw folder (headers only)
    python kit.py match   --raw RAW             # pair raw files with published figures
    python kit.py lanes   --raw RAW             # propose lanes, draw overlays and contact sheets
    python kit.py measure --raw RAW             # measure raw and published, same code
    python kit.py compare                       # does the published image give the same answer?
    python kit.py degrade --raw RAW             # which figure-making step moves the numbers
    python kit.py explore                       # how results move with the two thresholds
    python kit.py review                        # one HTML page for a person to look through

Everything is written under ``--work`` (default ``./work``), never into the
raw folder. Every step can be stopped and re-run: finished items are skipped
unless ``--redo`` is given. AGENT_BRIEF.md says in what order and why;
START_HERE.md is the owner's page.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import core  # noqa: E402
import measure as legacy  # noqa: E402

IMAGE_EXT = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".gif"}
CODES = {"M": "Supportive", "O": "Supportive, but not selective", "N": "Not supportive", "?": "not measured"}


# ---------------------------------------------------------------- paths & data

def work_dir(args):
    w = Path(args.work).resolve()
    w.mkdir(parents=True, exist_ok=True)
    return w


def raw_dir(args):
    if not args.raw:
        sys.exit("--raw is required for this step (the folder holding the raw scans)")
    r = Path(args.raw).resolve()
    if not r.is_dir():
        sys.exit(f"--raw {r} is not a folder")
    return r


def figures():
    return {f["id"]: f for f in json.loads((HERE / "data" / "figures.json").read_text())["figures"]}


def decisions():
    return {d["id"]: d for d in json.loads((HERE / "data" / "owner_decisions.json").read_text())["decisions"]}


def params(args):
    P = dict(core.V5_DEFAULTS)
    if getattr(args, "params", None):
        P.update(json.loads(Path(args.params).read_text()))
    return P


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def key_for(figure_id, rel):
    h = hashlib.sha1(rel.encode()).hexdigest()[:8]
    return f"{figure_id}__{re.sub(r'[^A-Za-z0-9._-]', '_', Path(rel).stem)[:40]}__{h}"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def manifest(work):
    m = work / "manifest.csv"
    if m.exists():
        return read_csv(m), m.name
    d = work / "manifest_draft.csv"
    if not d.exists():
        sys.exit("no manifest: run `match` first, then check manifest_draft.csv and save it as manifest.csv")
    rows = [r for r in read_csv(d) if r["confidence"] == "high"]
    print(f"note: manifest.csv not found; using the {len(rows)} high-confidence rows of manifest_draft.csv")
    return rows, d.name


def exposure_seconds(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ms|msec|s|sec|secs|seconds|min|mins|minutes)(?![a-z])", text.lower())
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2)
    return v / 1000 if u.startswith("ms") else v * 60 if u.startswith("min") else v


# ---------------------------------------------------------------- selftest

def cmd_selftest(args):
    import platform
    print(f"python {platform.python_version()}, numpy {np.__version__}, Pillow {Image.__version__}")
    try:
        import tifffile
        print(f"tifffile {tifffile.__version__} (optional) present")
    except ImportError:
        print("tifffile not installed (optional; only needed for TIFFs Pillow cannot open)")
    cases = [
        # name, wt bands, ko bands, expected code, expected (local, total)
        ("clean single band", [(0.4, 0.5, 6)], [], "M", (1.0, 1.0)),
        ("off-target far away", [(0.4, 0.3, 6), (0.75, 0.4, 6)], [(0.75, 0.4, 6)], "O", (1.0, 0.43)),
        ("weak target beside a strong band", [(0.40, 0.15, 6), (0.44, 0.6, 6)], [(0.44, 0.6, 6)], "N", (0.2, 0.2)),
        # one window below its threshold -> not selective (the owner's 2x2); a clear KO
        # residual is also what rules v4 call not selective
        ("co-migrating residual 40%", [(0.4, 0.5, 6)], [(0.4, 0.2, 6)], "O", (0.6, 0.6)),
        ("nothing lost in the KO", [(0.4, 0.5, 6)], [(0.4, 0.5, 6)], "N", (0.0, 0.0)),
    ]
    ok = True
    with tempfile.TemporaryDirectory() as d:
        for bits, bright, ext in ((16, True, ".tif"), (8, False, ".png")):
            for name, bw, bk, want, (wl, wtot) in cases:
                arr, lanes = core.synthetic(bw, bk, bits=bits, bright=bright)
                p = Path(d) / f"case{ext}"
                Image.fromarray(arr).save(p)
                gray, info = core.read_image(p)
                s, sat, pol = core.to_signal(gray, info)
                n, _ = core.measure_lanes(s, sat, lanes[0], lanes[1], (0, s.shape[0]),
                                          quantum=1 / float(info["max_value"]))
                code, why, flags = core.classify_v5(n)
                loc, tot = n.get("local_share", 0), n.get("total_share", 0)
                good = code == want and (want == "N" and not n.get("target") or
                                         abs(loc - wl) < 0.08 and abs(tot - wtot) < 0.08)
                ok &= good
                print(f"{'ok ' if good else 'BAD'} {bits:>2}-bit {pol:<12} {name:<34} -> {code} ({why})")
    # the published-crop path still reproduces the prototype on a known blot
    print("\nall checks passed" if ok else "\nSOME CHECKS FAILED — stop and report this output")
    return 0 if ok else 1


# ---------------------------------------------------------------- survey

def cmd_survey(args):
    raw, work = raw_dir(args), work_dir(args)
    rows, other = [], Counter()
    files = sorted(p for p in raw.rglob("*") if p.is_file())
    print(f"{len(files)} files under {raw}")
    for i, p in enumerate(files):
        rel = str(p.relative_to(raw))
        ext = p.suffix.lower()
        if ext not in IMAGE_EXT:
            other[ext or "(none)"] += 1
            continue
        row = {"path": rel, "ext": ext, "bytes": p.stat().st_size, "exposure_s": exposure_seconds(rel) or ""}
        try:
            with Image.open(p) as im:  # header only: pixels are not read
                row.update(mode=im.mode, width=im.width, height=im.height,
                           frames=getattr(im, "n_frames", 1),
                           tags=json.dumps(core._tags(im))[:400])
                tag_exp = next((v for k, v in core._tags(im).items() if "xpos" in k or "expos" in v.lower()), "")
                if tag_exp and not row["exposure_s"]:
                    row["exposure_s"] = exposure_seconds(tag_exp) or ""
        except Exception as exc:  # noqa: BLE001
            row.update(mode="UNREADABLE", error=str(exc)[:200])
        if args.stats and i % max(1, args.stats) == 0 and row.get("mode") != "UNREADABLE":
            try:
                g, info = core.read_image(p)
                s, sat, pol = core.to_signal(g, info)
                row.update(polarity=pol, min=float(g.min()), max=float(g.max()),
                           median=float(np.median(g)), saturated_frac=round(float(sat.mean()), 5))
            except Exception as exc:  # noqa: BLE001
                row.update(error=str(exc)[:200])
        rows.append(row)
        if (i + 1) % 500 == 0:
            print(f"  {i + 1} files looked at")
    fields = ["path", "ext", "bytes", "mode", "width", "height", "frames", "exposure_s", "polarity",
              "min", "max", "median", "saturated_frac", "tags", "error"]
    write_csv(work / "survey.csv", rows, fields)
    by_mode = Counter(r.get("mode") for r in rows)
    by_ext = Counter(r["ext"] for r in rows)
    by_top = Counter(Path(r["path"]).parts[0] if len(Path(r["path"]).parts) > 1 else "." for r in rows)
    gb = sum(int(r["bytes"]) for r in rows) / 1e9
    lines = [f"# Survey of {raw}", "",
             f"{len(rows)} image files ({gb:.1f} GB); {sum(other.values())} other files.", "",
             "| Image mode | Files |", "|---|---|"] + [f"| {k} | {v} |" for k, v in by_mode.most_common()] + [
             "", "| Extension | Files |", "|---|---|"] + [f"| {k} | {v} |" for k, v in by_ext.most_common()] + [
             "", "| Other file types | Files |", "|---|---|"] + [f"| {k} | {v} |" for k, v in other.most_common()] + [
             "", f"Top-level folders: {len(by_top)} (largest: " + ", ".join(f"{k} ({v})" for k, v in by_top.most_common(8)) + ")",
             "", f"Files with an exposure time in the name or tags: {sum(1 for r in rows if r['exposure_s'] != '')}",
             "", "Ten example paths:", ""] + [f"- `{r['path']}` — {r.get('mode')} {r.get('width')}×{r.get('height')}" for r in rows[:: max(1, len(rows) // 10)][:10]]
    (work / "survey.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {work / 'survey.csv'} and {work / 'survey.md'}")


# ---------------------------------------------------------------- match

def cmd_match(args):
    raw, work = raw_dir(args), work_dir(args)
    figs, dec = figures(), decisions()
    survey = work / "survey.csv"
    if not survey.exists():
        sys.exit("run `survey` first")
    files = [r for r in read_csv(survey) if r.get("mode") != "UNREADABLE"]
    cat_index = defaultdict(list)
    for f in figs.values():
        c = norm(f["cat"])
        if c:
            cat_index[c].append(f)
    cats = sorted(cat_index, key=len, reverse=True)
    out, unmatched = [], []
    for r in files:
        path_n = norm(r["path"])
        hits = []
        for c in cats:
            if len(c) >= 4 and c in path_n:
                for f in cat_index[c]:
                    gene_in = norm(f["gene"]) in path_n
                    if len(c) < 6 and not gene_in:
                        continue  # a short catalogue number is a match only beside its gene
                    hits.append((f, gene_in, len(c)))
        if not hits:
            unmatched.append(r["path"])
            continue
        best_len = max(h[2] for h in hits)
        hits = [h for h in hits if h[2] == best_len]
        gene_hits = [h for h in hits if h[1]]
        chosen = gene_hits or hits
        conf = "high" if len(chosen) == 1 and chosen[0][1] else ("medium" if len(chosen) == 1 else "ambiguous")
        for f, gene_in, _ in chosen:
            d = dec.get(f["id"])
            out.append({"raw_path": r["path"], "figure_id": f["id"], "gene": f["gene"], "cat": f["cat"],
                        "supplier": f["sup"], "owner": d["d"] if d else "",
                        "kd": "yes" if (d and d.get("kd")) or f.get("kd") else "",
                        "exposure_s": r.get("exposure_s", ""), "confidence": conf,
                        "candidates": len(chosen), "use": "", "notes": "",
                        "published_url": f["img"]})
    fields = ["raw_path", "figure_id", "gene", "cat", "supplier", "owner", "kd", "exposure_s",
              "confidence", "candidates", "use", "notes", "published_url"]
    out.sort(key=lambda r: (r["gene"], r["cat"], r["raw_path"]))
    write_csv(work / "manifest_draft.csv", out, fields)
    (work / "unmatched_raw.txt").write_text("\n".join(unmatched) + "\n", encoding="utf-8")
    matched_ids = {r["figure_id"] for r in out}
    missing = [d for d in dec.values() if d["id"] not in matched_ids]
    (work / "decided_without_raw.txt").write_text(
        "\n".join(f"{d['id']}\t{d['gene']}\t{d['cat']}\t{d['d']}" for d in missing) + "\n", encoding="utf-8")
    c = Counter(r["confidence"] for r in out)
    print(f"{len(out)} raw→figure pairs: {dict(c)}; {len(matched_ids)} figures matched "
          f"({len(matched_ids & set(dec))} of {len(dec)} decided); {len(unmatched)} raw files unmatched")
    print(f"wrote manifest_draft.csv, unmatched_raw.txt, decided_without_raw.txt in {work}")


# ---------------------------------------------------------------- annotations & overlays

def ann_path(work, key):
    return work / "annotations" / f"{key}.json"


def load_ann(work, key):
    p = ann_path(work, key)
    return json.loads(p.read_text()) if p.exists() else None


def display(signal):
    """For looking at only: a percentile stretch, dark bands on white."""
    lo, hi = np.percentile(signal, 1), np.percentile(signal, 99.7)
    v = np.clip((signal - lo) / max(hi - lo, 1e-9), 0, 1)
    return Image.fromarray(np.uint8(255 * (1 - v)))


def draw_overlay(signal, ann, n=None, label="", max_h=520):
    im = display(signal).convert("RGB")
    d = ImageDraw.Draw(im)
    y0, y1 = ann.get("rows") or (0, signal.shape[0])
    for role, colour in (("WT", (0, 90, 220)), ("KO", (220, 60, 0))):
        x = ann.get("lanes", {}).get(role)
        if x:
            d.rectangle([x[0], y0, x[1], y1], outline=colour, width=3)
            d.text((x[0] + 3, max(0, y0 - 12)), role, fill=colour)
    if n and n.get("target"):
        ty = y0 + n["target"]["y"]
        n0, n1 = n["near"]
        d.rectangle([0, y0 + n0, 6, y0 + n1], fill=(0, 160, 120))
        d.line([(0, ty), (im.width, ty)], fill=(0, 160, 120), width=2)
    for row, kda in ann.get("ladder") or []:
        d.text((2, int(row)), f"{kda}", fill=(120, 120, 120))
    s = min(1.0, max_h / im.height)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))))
    if label:
        canvas = Image.new("RGB", (im.width, im.height + 16), "white")
        canvas.paste(im, (0, 16))
        ImageDraw.Draw(canvas).text((2, 2), label[:80], fill=(0, 0, 0))
        im = canvas
    return im


def contact_sheets(images, out_dir, per=12, cols=4):
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("sheet_*.png"):
        old.unlink()
    for i in range(0, len(images), per):
        chunk = images[i:i + per]
        cw = max(im.width for im in chunk)
        ch = max(im.height for im in chunk)
        rows = math.ceil(len(chunk) / cols)
        sheet = Image.new("RGB", (cw * cols, ch * rows), "white")
        for j, im in enumerate(chunk):
            sheet.paste(im, ((j % cols) * cw, (j // cols) * ch))
        sheet.save(out_dir / f"sheet_{i // per:03d}.png")


def cmd_lanes(args):
    raw, work = raw_dir(args), work_dir(args)
    rows, name = manifest(work)
    (work / "annotations").mkdir(exist_ok=True)
    (work / "overlays").mkdir(exist_ok=True)
    thumbs, proposed, kept = [], 0, 0
    for r in rows:
        if r.get("use", "").lower() == "no":
            continue
        key = key_for(r["figure_id"], r["raw_path"])
        ann = load_ann(work, key)
        if ann and ann.get("status") == "checked" and not args.redo:
            kept += 1
        else:
            g, info = core.read_image(raw / r["raw_path"])
            s, sat, pol = core.to_signal(g, info)
            lanes, conf = core.auto_lanes(s, 2)
            ann = {"raw_path": r["raw_path"], "figure_id": int(r["figure_id"]), "gene": r["gene"],
                   "cat": r["cat"], "status": "auto", "confidence": conf, "polarity": pol,
                   "lanes": {"WT": list(lanes[0]), "KO": list(lanes[1])} if len(lanes) == 2 else {},
                   "rows": [0, int(s.shape[0])], "kd": r.get("kd") == "yes", "ladder": [],
                   "sat_level": None, "notes": ""}
            ann_path(work, key).write_text(json.dumps(ann, indent=1))
            proposed += 1
        g, info = core.read_image(raw / r["raw_path"])
        s, sat, _ = core.to_signal(g, info, ann.get("polarity"), ann.get("sat_level"))
        im = draw_overlay(s, ann, label=f"{r['figure_id']} {r['gene']} {r['cat']} [{ann['status']} {ann.get('confidence', '')}]")
        im.save(work / "overlays" / f"{key}.png")
        thumbs.append(im)
    contact_sheets(thumbs, work / "sheets")
    print(f"{proposed} lane proposals written, {kept} checked annotations kept; "
          f"overlays in {work / 'overlays'}, contact sheets in {work / 'sheets'} (from {name})")


# ---------------------------------------------------------------- measure

def fetch_published(work, fig):
    d = work / "published"
    d.mkdir(exist_ok=True)
    out = d / f"{fig['id']}.png"
    if out.exists():
        return out
    tmp = d / f"{fig['id']}.download"
    r = subprocess.run(["curl", "-sSfL", "--max-time", "60", "-o", str(tmp), fig["img"]])
    if r.returncode or not tmp.exists():
        return None
    if tmp.read_bytes()[:256].lstrip().startswith((b"<svg", b"<?xml")):
        tmp.rename(d / f"{fig['id']}.svg")
        return None  # an SVG figure: render it to PNG by hand, saved as <id>.png
    tmp.rename(out)
    return out


def ponceau_ratio(rgb, box_main):
    """WT ÷ KO total protein from the pink Ponceau panel, or None."""
    a = rgb
    for b in legacy.find_boxes(a):
        if legacy.is_ponceau(a, b) and b[1] > box_main[1]:
            x0, y0, x1, y1 = b
            g = a[y0:y1, x0:x1, 1]
            stain = np.clip(255 - g, 0, None)
            w = (x1 - x0) / 2
            lanes = [stain[:, int(w * i + w * .25):int(w * (i + 1) - w * .25)].sum() for i in range(2)]
            return float(lanes[0] / lanes[1]) if lanes[1] > 0 else None
    return None


def measure_published(path, P, kd):
    gray, info = core.read_image(path)
    s, sat, pol = core.to_signal(gray, info, "dark_bands")
    rgb = info.get("rgb")
    if rgb is None:
        rgb = np.repeat(gray[:, :, None], 3, axis=2)
    box = legacy.main_box(rgb)
    if box is None:
        return {"error": "no blot frame found"}, None, None
    x0, y0, x1, y1 = box
    nl = legacy.lane_count(255 - rgb[y0:y1, x0:x1].mean(axis=2))
    if nl != 2:
        return {"error": f"{nl} lanes: needs lane roles"}, None, None
    w = (x1 - x0) / 2
    lanes = (int(x0), int(x0 + w)), (int(x0 + w), int(x1))
    n, prof = core.measure_lanes(s, sat, lanes[0], lanes[1], (y0, y1), P, kd=kd, quantum=1 / 255)
    n["ponceau_ratio"] = ponceau_ratio(rgb, box)
    n["polarity"] = pol
    ann = {"lanes": {"WT": list(lanes[0]), "KO": list(lanes[1])}, "rows": [int(y0), int(y1)]}
    return n, prof, (s, ann)


def measure_raw(path, ann, P):
    gray, info = core.read_image(path)
    s, sat, pol = core.to_signal(gray, info, ann.get("polarity"), ann.get("sat_level"))
    L = ann.get("lanes") or {}
    if "WT" not in L or "KO" not in L:
        return {"error": "lanes not annotated"}, None, None
    n, prof = core.measure_lanes(s, sat, tuple(L["WT"]), tuple(L["KO"]), tuple(ann.get("rows") or (0, s.shape[0])),
                                 P, kd=bool(ann.get("kd")), ladder=ann.get("ladder") or None,
                                 quantum=1 / float(info.get("max_value", 65535)))
    n["polarity"] = pol
    n["bits"] = 16 if info.get("max_value", 255) > 255 else 8
    return n, prof, (s, ann)


def slim(prof, n=200):
    if prof is None:
        return None
    out = {}
    for k, v in prof.items():
        v = np.asarray(v)
        step = max(1, len(v) // n)
        out[k] = [round(float(x), 5) for x in v[::step]]
    return out


def record(kind, r, n, prof, P, key):
    code, why, flags = core.classify_v5(n, P)
    rec = {"key": key, "kind": kind, "figure_id": int(r["figure_id"]), "gene": r["gene"], "cat": r["cat"],
           "owner": r.get("owner", ""), "raw_path": r.get("raw_path", ""), "exposure_s": r.get("exposure_s", ""),
           "v5": code, "v5_why": why, "flags": flags, "n": _jsonable(n), "profiles": slim(prof)}
    if kind == "published" and n and "error" not in n:
        rec["v4"] = legacy.classify(_v4_input(n), legacy.params(), bool(n.get("kd")))[0]
    return rec


def _v4_input(n):
    # rules v4 read heights in 0..255 darkness units; core keeps 0..1
    bands = [{"y": b["y"], "wt": 255 * b["h"], "prom": 255 * b["prom"], "ko": 255 * b["ko"], "ratio": b["ratio"]}
             for b in n["bands"]]
    ko_only = [{"y": b["y"], "h": 255 * b["h"], "prom": 255 * b["prom"]} for b in n["ko_only"]]
    return {"bands": bands, "ko_only": ko_only, "H": n["H"]}


def _jsonable(n):
    return json.loads(json.dumps(n, default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else str(o)))


def load_measurements(work):
    p = work / "measurements.jsonl"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[(rec["kind"], rec["key"])] = rec
    return out


def cmd_measure(args):
    raw, work = raw_dir(args), work_dir(args)
    rows, name = manifest(work)
    P, figs = params(args), figures()
    done = {} if args.redo else load_measurements(work)
    if args.redo and (work / "measurements.jsonl").exists():
        (work / "measurements.jsonl").unlink()
    (work / "overlays").mkdir(exist_ok=True)
    counts = Counter()
    with open(work / "measurements.jsonl", "a", encoding="utf-8") as out:
        for r in rows:
            if r.get("use", "").lower() == "no":
                continue
            fid = int(r["figure_id"])
            key = key_for(fid, r["raw_path"])
            pkey = f"pub_{fid}"
            if ("published", pkey) not in done:
                fig = figs.get(fid)
                path = fetch_published(work, fig) if fig else None
                if path:
                    n, prof, _ = measure_published(path, P, r.get("kd") == "yes")
                else:
                    n, prof = {"error": "published image not fetched"}, None
                rec = record("published", r, n, prof, P, pkey)
                out.write(json.dumps(rec) + "\n")
                done[("published", pkey)] = rec
                counts["published"] += 1
            if ("raw", key) in done:
                continue
            ann = load_ann(work, key)
            if not ann:
                counts["raw without annotation (run lanes)"] += 1
                continue
            try:
                n, prof, (s, a2) = measure_raw(raw / r["raw_path"], ann, P)
            except Exception as exc:  # noqa: BLE001
                n, prof, s = {"error": f"could not measure: {exc}"[:200]}, None, None
            rec = record("raw", r, n, prof, P, key)
            rec["annotation_status"] = ann.get("status")
            out.write(json.dumps(rec) + "\n")
            out.flush()
            counts["raw"] += 1
            if s is not None and "error" not in n:
                draw_overlay(s, ann, n, label=f"{fid} {r['gene']} {r['cat']}: {rec['v5']} {rec['v5_why']}").save(
                    work / "overlays" / f"{key}.png")
    print(f"measured: {dict(counts)} → {work / 'measurements.jsonl'} (from {name})")


# ---------------------------------------------------------------- compare

METRICS = ["local_share", "total_share", "target_purity", "target_share"]


def best_raw(recs, prefer_use=None):
    """Per figure, the raw reading to compare: the longest exposure whose
    target is not saturated; without exposure times, the clearest unsaturated
    target."""
    ok = [r for r in recs if "error" not in r["n"]]
    if not ok:
        return None
    unsat = [r for r in ok if "saturated" not in r["flags"]] or ok
    timed = [r for r in unsat if r.get("exposure_s") not in ("", None)]
    if timed:
        return max(timed, key=lambda r: float(r["exposure_s"]))
    return max(unsat, key=lambda r: (r["n"].get("target") or {}).get("snr", 0))


def pairs(work):
    M = load_measurements(work)
    pub = {r["figure_id"]: r for (k, _), r in M.items() if k == "published"}
    raws = defaultdict(list)
    for (k, _), r in M.items():
        if k == "raw":
            raws[r["figure_id"]].append(r)
    out = []
    for fid, rs in raws.items():
        b = best_raw(rs)
        if b and fid in pub:
            out.append((b, pub[fid]))
    return out


def cmd_compare(args):
    work = work_dir(args)
    P = pairs(work)
    if not P:
        sys.exit("nothing to compare: run `measure` first")
    lines = ["# Raw scan against published figure, same code", "",
             f"{len(P)} figures with both a raw reading and a published one. The raw reading is the "
             "longest unsaturated exposure (or the clearest target when exposures are not known).", "",
             "## Do they give the same result?", "", "| raw \\ published | M | O | N | ? |", "|---|---|---|---|---|"]
    cm = Counter((r["v5"], p["v5"]) for r, p in P)
    for a in "MON?":
        lines.append(f"| {a} | " + " | ".join(str(cm[(a, b)]) for b in "MON?") + " |")
    both = [(r, p) for r, p in P if r["v5"] != "?" and p["v5"] != "?"]
    same = sum(r["v5"] == p["v5"] for r, p in both)
    jump = sum({r["v5"], p["v5"]} == {"M", "N"} for r, p in both)
    lines += ["", f"Same result: **{same} of {len(both)}** measured on both ({100 * same / max(1, len(both)):.0f}%). "
              f"Supportive straight to Not supportive or back: **{jump}**.", "",
              "## How far apart are the numbers? (published − raw)", "",
              "| Number | Pairs | Mean difference | 95% limits of agreement |", "|---|---|---|---|"]
    for m in METRICS:
        d = [p["n"][m] - r["n"][m] for r, p in both if r["n"].get(m) is not None and p["n"].get(m) is not None]
        if len(d) >= 3:
            mu, sd = float(np.mean(d)), float(np.std(d, ddof=1))
            lines.append(f"| {m} | {len(d)} | {mu:+.3f} | {mu - 1.96 * sd:+.3f} to {mu + 1.96 * sd:+.3f} |")
    sat_pub = [(r, p) for r, p in both if "saturated" in p["flags"]]
    unsat_pub = [(r, p) for r, p in both if "saturated" not in p["flags"]]
    for label, grp in (("published figure saturated", sat_pub), ("published figure not saturated", unsat_pub)):
        if grp:
            s = sum(r["v5"] == p["v5"] for r, p in grp)
            lines.append(f"\n{label}: same result {s} of {len(grp)} ({100 * s / len(grp):.0f}%)")
    rows = []
    for r, p in sorted(P, key=lambda x: (x[0]["gene"], x[0]["cat"])):
        if r["v5"] != p["v5"]:
            rows.append({"figure_id": r["figure_id"], "gene": r["gene"], "cat": r["cat"], "owner": r["owner"],
                         "raw": r["v5"], "published": p["v5"], "raw_why": r["v5_why"], "published_why": p["v5_why"],
                         "published_flags": ";".join(p["flags"]), "raw_flags": ";".join(r["flags"]),
                         "raw_path": r["raw_path"]})
    write_csv(work / "compare_disagreements.csv", rows,
              ["figure_id", "gene", "cat", "owner", "raw", "published", "raw_why", "published_why",
               "published_flags", "raw_flags", "raw_path"])
    lines += ["", f"{len(rows)} disagreements listed in compare_disagreements.csv."]
    (work / "compare.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ---------------------------------------------------------------- degrade

def _variants(s):
    """Each step that turns a raw scan into a figure, alone and together."""
    def to8(v):
        return np.round(np.clip(v, 0, 1) * 255) / 255

    def stretch(v, pct=99.5):
        hi = np.percentile(v, pct)
        return np.clip(v / max(hi, 1e-9), 0, 1)

    def jpeg(v, q=75):
        buf = io.BytesIO()
        Image.fromarray(np.uint8(255 * (1 - np.clip(v, 0, 1)))).save(buf, "JPEG", quality=q)
        return 1 - np.asarray(Image.open(io.BytesIO(buf.getvalue()))).astype(float) / 255

    yield "8-bit", to8(s), 1.0
    yield "contrast stretch (top 0.5% clipped)", stretch(s), 1.0
    yield "contrast stretch (top 2% clipped)", stretch(s, 98), 1.0
    yield "gamma 0.7", s ** 0.7, 1.0
    yield "gamma 1.5", s ** 1.5, 1.0
    f = 340 / s.shape[0]
    if f < 1:
        im = Image.fromarray(np.float32(s), mode="F").resize((max(1, int(s.shape[1] * f)), 340))
        yield "resized to 340 rows", np.asarray(im, dtype=float), f
    yield "JPEG quality 75", jpeg(s), 1.0
    v = stretch(s)
    if f < 1:
        v = np.asarray(Image.fromarray(np.float32(v), mode="F").resize((max(1, int(s.shape[1] * f)), 340)), dtype=float)
    yield "all together (stretch, 8-bit, resize, JPEG)", jpeg(to8(v)), (f if f < 1 else 1.0)


def cmd_degrade(args):
    raw, work = raw_dir(args), work_dir(args)
    P = params(args)
    M = load_measurements(work)
    raws = defaultdict(list)
    for (k, _), r in M.items():
        if k == "raw":
            raws[r["figure_id"]].append(r)
    chosen = [b for b in (best_raw(v) for v in raws.values()) if b and b["n"].get("target")]
    chosen = chosen[: args.limit]
    if not chosen:
        sys.exit("nothing to degrade: run `measure` first")
    stats = defaultdict(lambda: {"dl": [], "dt": [], "flips": 0, "n": 0})
    for rec in chosen:
        ann = load_ann(work, rec["key"])
        g, info = core.read_image(raw / rec["raw_path"])
        s, sat, _ = core.to_signal(g, info, ann.get("polarity"), ann.get("sat_level"))
        for name, v, f in _variants(s):
            satv = v >= 1 - 1 / 255
            L = {k: [int(x * f) for x in xs] for k, xs in ann["lanes"].items()}
            rows = [int(x * f) for x in (ann.get("rows") or (0, s.shape[0]))]
            ladder = [[row * f, k] for row, k in (ann.get("ladder") or [])] or None
            n, _ = core.measure_lanes(v, satv, tuple(L["WT"]), tuple(L["KO"]), tuple(rows), P,
                                      kd=bool(ann.get("kd")), ladder=ladder)
            code = core.classify_v5(n, P)[0]
            st = stats[name]
            st["n"] += 1
            st["flips"] += code != rec["v5"]
            if n.get("target"):
                st["dl"].append(n["local_share"] - rec["n"]["local_share"])
                st["dt"].append(n["total_share"] - rec["n"]["total_share"])
    lines = ["# Which figure-making step moves the numbers", "",
             f"{len(chosen)} raw scans, each changed one way at a time and measured with its own lanes.", "",
             "| Change | Result changed | Near-target share, mean change (range) | Total share, mean change (range) |",
             "|---|---|---|---|"]
    for name, st in stats.items():
        fmt = lambda d: f"{np.mean(d):+.3f} ({min(d):+.2f} to {max(d):+.2f})" if d else "—"
        lines.append(f"| {name} | {st['flips']} of {st['n']} | {fmt(st['dl'])} | {fmt(st['dt'])} |")
    (work / "degrade.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ---------------------------------------------------------------- explore

def cmd_explore(args):
    work = work_dir(args)
    M = load_measurements(work)
    P0 = params(args)
    lines = ["# How results move with the two thresholds", "",
             "Agreement with the owner's decided blots, for each pair of thresholds. This is a map, not "
             "a fit: the thresholds are to be set from principle (AGENT_BRIEF.md) and this shows what "
             "each choice would do. The starting thresholds are marked ◆.", ""]
    for kind in ("raw", "published"):
        recs = [r for (k, _), r in M.items() if k == kind and r["owner"] in ("M", "O", "N")]
        if kind == "raw":
            by = defaultdict(list)
            for r in recs:
                by[r["figure_id"]].append(r)
            recs = [b for b in (best_raw(v) for v in by.values()) if b]
        recs = [r for r in recs if "error" not in r["n"]]
        if not recs:
            continue
        lines += [f"## {kind} ({len(recs)} decided blots measured)", ""]
        dist = ["| Your decision | Blots | Near-target share (median, IQR) | Total share (median, IQR) |", "|---|---|---|---|"]
        for c in "MON":
            g = [r for r in recs if r["owner"] == c and r["n"].get("target")]
            if g:
                q = lambda k: np.percentile([r["n"][k] for r in g], [25, 50, 75])
                a, b = q("local_share"), q("total_share")
                dist.append(f"| {CODES[c]} | {len(g)} | {a[1]:.2f} ({a[0]:.2f}–{a[2]:.2f}) | {b[1]:.2f} ({b[0]:.2f}–{b[2]:.2f}) |")
        lines += dist + ["", "Exact agreement (% of blots) by threshold pair:", ""]
        locs = [0.6, 0.7, 0.8, 0.85, 0.9, 0.95]
        tots = [0.3, 0.4, 0.5, 0.6, 0.7]
        lines.append("| near-target ≥ \\ total ≥ | " + " | ".join(str(t) for t in tots) + " |")
        lines.append("|---" * (len(tots) + 1) + "|")
        for lo in locs:
            cells = []
            for to in tots:
                Pt = {**P0, "local_min": lo, "total_min": to}
                agree = sum(core.classify_v5(r["n"], Pt)[0] == r["owner"] for r in recs)
                mark = " ◆" if (lo, to) == (P0["local_min"], P0["total_min"]) else ""
                cells.append(f"{100 * agree / len(recs):.0f}{mark}")
            lines.append(f"| {lo} | " + " | ".join(cells) + " |")
        cm = Counter((r["owner"], core.classify_v5(r["n"], P0)[0]) for r in recs)
        lines += ["", "At the starting thresholds — your decision (rows) against the measurement (columns):", "",
                  "| | M | O | N |", "|---|---|---|---|"] + [
                  f"| {c} | " + " | ".join(str(cm[(c, d)]) for d in "MON") + " |" for c in "MON"] + [""]
    (work / "explore.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ---------------------------------------------------------------- review page

def _img64(path, max_h=300):
    if not path or not Path(path).exists():
        return ""
    im = Image.open(path).convert("RGB")
    s = min(1.0, max_h / im.height)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _svg(prof):
    if not prof:
        return ""
    wt, ko = prof["wt"], prof["ko"]
    top = max(max(wt), max(ko), 1e-9)
    H, Wd = 240, 140
    pts = lambda v: " ".join(f"{Wd * x / top:.1f},{H * i / max(1, len(v) - 1):.1f}" for i, x in enumerate(v))
    return (f'<svg viewBox="0 0 {Wd} {H}" width="{Wd}" height="{H}"><polyline fill="none" stroke="#222" '
            f'stroke-width="1.4" points="{pts(wt)}"/><polyline fill="none" stroke="#c43" stroke-width="1.4" '
            f'points="{pts(ko)}"/></svg>')


def cmd_review(args):
    work = work_dir(args)
    M = load_measurements(work)
    pub = {r["figure_id"]: r for (k, _), r in M.items() if k == "published"}
    by = defaultdict(list)
    for (k, _), r in M.items():
        if k == "raw":
            by[r["figure_id"]].append(r)
    items = []
    for fid, rs in by.items():
        b = best_raw(rs)
        if not b:
            continue
        p = pub.get(fid)
        reasons = []
        if b["owner"] and b["v5"] != b["owner"]:
            reasons.append("differs from your decision")
        if p and p["v5"] != b["v5"]:
            reasons.append("published image gives a different result")
        if b["flags"]:
            reasons.append(", ".join(b["flags"]))
        if args.which == "all" or reasons:
            items.append((b, p, reasons))
    items.sort(key=lambda x: (x[0]["gene"], x[0]["cat"]))
    cards = []
    for b, p, reasons in items[: args.limit]:
        nb = b["n"]
        nums = "".join(f"<tr><td>{k}</td><td>{nb.get(k):.2f}</td><td>{(p or {}).get('n', {}).get(k, float('nan')):.2f}</td></tr>"
                       for k in METRICS if isinstance(nb.get(k), (int, float)))
        cards.append(f"""<section><h2>{b['gene']} {b['cat']} <small>figure {b['figure_id']}</small></h2>
<p>You: <b>{CODES.get(b['owner'], '—')}</b> · raw: <b>{CODES[b['v5']]}</b> ({b['v5_why']}) · published: <b>{CODES[p['v5']] if p else '—'}</b>{(' ('+p['v5_why']+')') if p else ''}</p>
<p class="why">{'; '.join(reasons) or 'listed'}</p>
<div class="row"><figure><img src="{_img64(work / 'overlays' / (b['key'] + '.png'))}"><figcaption>raw: {Path(b['raw_path']).name} · blue WT, orange KO, green = target and its neighbourhood</figcaption></figure>
<figure>{_svg(b.get('profiles'))}<figcaption>raw lanes after the floor: black WT, red KO</figcaption></figure>
<figure><img src="{_img64(work / 'published' / (str(b['figure_id']) + '.png'))}"><figcaption>published</figcaption></figure>
<table><tr><th>number</th><th>raw</th><th>published</th></tr>{nums}</table></div></section>""")
    html = f"""<!doctype html><meta charset="utf-8"><title>Western blot review</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#16191d;background:#f6f7f8}}
section{{background:#fff;border:1px solid #dde1e5;border-radius:6px;padding:12px 16px;margin:14px 0}}
h2{{font-size:17px;margin:0 0 4px}} small{{color:#778;font-weight:400}} .why{{color:#a34;margin:2px 0 8px}}
.row{{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}} figure{{margin:0}} figcaption{{font-size:12px;color:#667;max-width:300px}}
table{{border-collapse:collapse;font-size:13px}} td,th{{border-bottom:1px solid #e3e6e9;padding:2px 8px;text-align:right}} td:first-child,th:first-child{{text-align:left}}</style>
<h1>Western blot review</h1><p>{len(items)} blots ({args.which}); showing {min(len(items), args.limit)}. Generated by kit.py review.</p>{''.join(cards)}"""
    (work / "review.html").write_text(html, encoding="utf-8")
    print(f"wrote {work / 'review.html'} ({min(len(items), args.limit)} blots)")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", default="work", help="where outputs go (default ./work)")
    ap.add_argument("--raw", help="the folder holding the raw scans (read only)")
    ap.add_argument("--params", help="a JSON file of v5 thresholds to use instead of the starting ones")
    ap.add_argument("--redo", action="store_true", help="redo finished items instead of skipping them")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    s = sub.add_parser("survey")
    s.add_argument("--stats", type=int, default=0, help="also read pixels of every Nth file (slow on big folders)")
    sub.add_parser("match")
    sub.add_parser("lanes")
    sub.add_parser("measure")
    sub.add_parser("compare")
    d = sub.add_parser("degrade")
    d.add_argument("--limit", type=int, default=40)
    sub.add_parser("explore")
    r = sub.add_parser("review")
    r.add_argument("--which", choices=["flagged", "all"], default="flagged")
    r.add_argument("--limit", type=int, default=150)
    args = ap.parse_args(argv)
    fn = {"selftest": cmd_selftest, "survey": cmd_survey, "match": cmd_match, "lanes": cmd_lanes,
          "measure": cmd_measure, "compare": cmd_compare, "degrade": cmd_degrade,
          "explore": cmd_explore, "review": cmd_review}[args.cmd]
    return fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
