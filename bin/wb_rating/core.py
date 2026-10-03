"""Measurement core for raw scans and published crops alike.

`measure.py` was written for the published crops: 8-bit, dark bands on a
light ground, a drawn frame round the blot. A raw chemiluminescence export is
none of those — 16-bit, often bright bands on a dark ground, no frame, and the
lanes wherever the strip sat on the imager. This module reads either, turns it
into one quantity (*signal*: 0 = no antibody signal, 1 = the most the file can
hold), and measures lanes a person or the agent has located (`lanes`), so the
same numbers come out of both and can be compared.

The two-window numbers (owner's proposal, 3 Oct 2026) are here:

  local_share  of the WT signal near the target, the share that disappears
               in the KO (neighbourhood: ±20% of the target's apparent kDa
               when a ladder is annotated, else ±10% of the lane height)
  total_share  the same across the whole lane

Both are "how much of what this antibody shows is target-dependent", read in
two windows. ``classify_v5`` turns them into a result with two thresholds;
``measure.classify`` (rules v4) is kept for comparison.

Nothing here writes anywhere but the paths it is given.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

#: Starting thresholds for v5, each set from a stated principle rather than
#: fitted to anybody's ratings (see AGENT_BRIEF.md §Thresholds). Override with
#: a params file; never edit these to make a table look better.
V5_DEFAULTS = {
    "lost_ratio": 0.5,     # a WT band is lost if the KO keeps at most half of it
    "snr_detect": 3.29,    # Currie (1968): 5% false positives and 5% false negatives
    "local_min": 0.90,     # <=10% of the near-target signal is not target
    "total_min": 0.50,     # the target is most of what the antibody shows
    "near_kda_frac": 0.20, # neighbourhood = ±20% of apparent kDa (HPA's predicted-size window)
    "near_lane_frac": 0.10,# ... or ±10% of lane height when no ladder is annotated
    "margin": 0.05,        # within this of a threshold -> flagged for a person
}


# ---------------------------------------------------------------- reading

def read_image(path):
    """``(gray, info)``: a 2-D float array in the file's own units, and what
    is known about it. Colour images are averaged over RGB; ``info['rgb']``
    keeps the colour array for the Ponceau panel."""
    path = Path(path)
    info = {"path": str(path), "frames": 1}
    try:
        im = Image.open(path)
    except Exception as exc:  # noqa: BLE001 — try tifffile below
        im = None
        err = exc
    if im is not None:
        info.update(mode=im.mode, width=im.width, height=im.height,
                    frames=getattr(im, "n_frames", 1), tags=_tags(im))
        if im.mode in ("I;16", "I;16B", "I;16L", "I;16N"):
            arr = np.asarray(im, dtype=np.uint16).astype(float)
            info["max_value"] = 65535.0
        elif im.mode == "I":
            arr = np.asarray(im).astype(float)
            info["max_value"] = 65535.0 if arr.max() <= 65535 else float(arr.max())
        elif im.mode == "F":
            arr = np.asarray(im).astype(float)
            info["max_value"] = float(max(arr.max(), 1.0))
        elif im.mode == "L":
            arr = np.asarray(im).astype(float)
            info["max_value"] = 255.0
        else:
            rgb = np.asarray(im.convert("RGB")).astype(float)
            info["rgb"] = rgb
            arr = rgb.mean(axis=2)
            info["max_value"] = 255.0
        return arr, info
    try:
        import tifffile  # optional, for TIFFs Pillow cannot open
    except ImportError:
        raise OSError(f"cannot read {path}: {err} (installing tifffile may help)")
    arr = np.asarray(tifffile.imread(str(path))).astype(float)
    while arr.ndim > 2:
        arr = arr[0] if arr.shape[0] < arr.shape[-1] else arr.mean(axis=-1)
    info.update(mode="tifffile", width=arr.shape[1], height=arr.shape[0],
                max_value=65535.0 if arr.max() <= 65535 else float(arr.max()))
    return arr, info


def _tags(im):
    out = {}
    tags = getattr(im, "tag_v2", None)
    if not tags:
        return out
    names = {270: "ImageDescription", 271: "Make", 272: "Model", 305: "Software",
             306: "DateTime", 258: "BitsPerSample", 33434: "ExposureTime"}
    for k, v in dict(tags).items():
        name = names.get(k)
        text = v if isinstance(v, str) else repr(v)
        if name or "expos" in text.lower():
            out[name or str(k)] = text[:300]
    return out


def to_signal(gray, info, polarity=None, sat_level=None):
    """``(signal, sat_mask, polarity)``. Signal is 0..1, 1 = strongest.

    Polarity is read from the image unless given: a ground that is mostly
    light means dark bands (a published figure), mostly dark means bright
    bands (most raw chemiluminescence exports). Saturated pixels are those at
    the end of the scale the signal runs towards — black in a figure, the top
    of the range in a raw export, or ``sat_level`` (file units) if given.
    """
    m = float(info.get("max_value") or 255.0)
    g = np.clip(gray / m, 0, 1)
    if polarity is None:
        polarity = "dark_bands" if np.median(g) > 0.5 else "bright_bands"
    s = 1.0 - g if polarity == "dark_bands" else g
    if sat_level is not None:
        lvl = float(sat_level) / m
        sat = (g <= lvl) if polarity == "dark_bands" else (g >= lvl)
    else:
        eps = 2.5 / 255 if m <= 255 else 1.0 / 1000
        sat = s >= 1.0 - eps
    return s, sat, polarity


# ---------------------------------------------------------------- lanes

def auto_lanes(signal, n=2, rows=None):
    """Propose ``n`` lanes as column ranges ``[(x0, x1), ...]`` left to right,
    with a confidence 0..1. A proposal to be checked by eye, never trusted."""
    y0, y1 = rows or (0, signal.shape[0])
    col = signal[y0:y1].mean(axis=0)
    col = col - np.percentile(col, 5)
    k = max(3, signal.shape[1] // 100)
    col = np.convolve(col, np.ones(k) / k, mode="same")
    if col.max() <= 0:
        return [], 0.0
    best = ([], 0.0)
    # down to 3%: a clean KO lane often shows only its faint lysate background
    for frac in (0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1, 0.06, 0.03):
        on = col > frac * col.max()
        runs, i = [], 0
        while i < len(on):
            if on[i]:
                j = i
                while j < len(on) and on[j]:
                    j += 1
                if j - i >= max(5, signal.shape[1] // 60):
                    runs.append((i, j))
                i = j
            else:
                i += 1
        if len(runs) >= n:
            runs = sorted(sorted(runs, key=lambda r: -col[r[0]:r[1]].sum())[:n])
            widths = [b - a for a, b in runs]
            conf = (min(widths) / max(widths)) * (1.0 if len(runs) == n else 0.7)
            if conf > best[1]:
                best = (runs, conf)
    return best[0], round(best[1], 2)


def lane_profile(signal, x0, x1, y0, y1, frac=0.5):
    """Median signal across the middle ``frac`` of a lane, row by row."""
    w = x1 - x0
    a = int(x0 + w * (1 - frac) / 2)
    b = max(int(x1 - w * (1 - frac) / 2), a + 1)
    return np.median(signal[y0:y1, a:b], axis=1)


def smooth(v, k):
    k = max(1, int(k))
    if k <= 1:
        return v
    return np.convolve(np.pad(v, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")


def _roll(p, k, fn):
    pad = np.pad(p, (k, k), mode="edge")
    return np.array([fn(pad[i:i + 2 * k + 1]) for i in range(len(p))])


def floor(p, k):
    """The background floor: a morphological opening (the lowest value within
    ±k rows, then the highest of those within ±k), smoothed. The opening is
    what a rolling ball does; the lowest value alone sits below a sloping
    background by slope × k, and that pedestal was being counted as signal
    across the whole lane (found by the self-test, 3 Oct 2026)."""
    opened = _roll(_roll(p, k, np.min), k, np.max)
    return np.minimum(smooth(opened, k), p)


def noise_sigma(p, k):
    """Pixel-scale noise: robust SD of the profile minus its own smoothing."""
    r = p - smooth(p, k)
    return float(1.4826 * np.median(np.abs(r - np.median(r))) or 1e-9)


def peaks(s, min_height, merge):
    out = []
    for i in range(1, len(s) - 1):
        if s[i] >= s[i - 1] and s[i] > s[i + 1] and s[i] >= min_height:
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
        if merged and pk["y"] - merged[-1]["y"] < merge:
            if pk["h"] > merged[-1]["h"]:
                merged[-1] = pk
        else:
            merged.append(pk)
    return merged


def fwhm(s, y):
    h = s[y] / 2
    a = y
    while a > 0 and s[a] > h:
        a -= 1
    b = y
    while b < len(s) - 1 and s[b] > h:
        b += 1
    return max(b - a, 2)


# ---------------------------------------------------------------- the numbers

def measure_lanes(signal, sat, wt_x, ko_x, rows, P=None, kd=False, ladder=None,
                  loading=1.0, quantum=1 / 65535):
    """Every number for one WT/KO pair. ``wt_x``/``ko_x`` are column ranges,
    ``rows`` the row range to read; ``ladder`` is ``[[row, kDa], ...]`` in
    image rows (optional); ``loading`` multiplies the KO lane (WT ÷ KO
    total protein) when a Ponceau reading is trusted."""
    P = {**V5_DEFAULTS, **(P or {})}
    y0, y1 = rows
    H = y1 - y0
    sm = max(3, H // 150)
    wt_raw = lane_profile(signal, *wt_x, y0, y1)
    ko_raw = lane_profile(signal, *ko_x, y0, y1) * loading
    k = max(15, int(0.08 * H))
    wt_bg, ko_bg = floor(wt_raw, k), floor(ko_raw, k)
    wt = np.clip(smooth(wt_raw - wt_bg, sm), 0, None)
    ko = np.clip(smooth(ko_raw - ko_bg, sm), 0, None)
    # half a grey level at least: an 8-bit figure's flat ground has no measurable noise
    sigma = max(noise_sigma(wt_raw, sm * 3), quantum / 2)
    bands = []
    for p in peaks(wt, P["snr_detect"] * sigma, max(3, H // 100)):
        y = p["y"]
        win = max(2, sm)
        kv = float(ko[max(0, y - win):y + win + 1].max())
        bands.append({"y": y, "h": p["h"], "prom": p["prom"], "ko": kv,
                      "ratio": kv / max(p["h"], 1e-12), "snr": p["h"] / sigma})
    ko_only = [p for p in peaks(ko, P["snr_detect"] * sigma, max(3, H // 100))
               if wt[max(0, p["y"] - sm):p["y"] + sm + 1].max() < 0.35 * p["h"]]
    lost = [b for b in bands if b["ratio"] <= P["lost_ratio"]]
    # a band that drops to a smaller size in the KO is lost (truncated protein)
    for b in bands:
        if b not in lost and b["ratio"] <= 0.9 and any(0 < q["y"] - b["y"] <= 0.16 * H for q in ko_only):
            b["shifted"] = True
            lost.append(b)
    out = {"H": H, "sigma": sigma, "bands": bands, "ko_only": ko_only, "kd": kd,
           "loading": loading, "n_bands": len(bands)}
    # Shares count only rows where the WT lane is above the detection limit:
    # residue below it is not a band anybody could see, and summed over a
    # whole lane it outweighs a real band.
    det = wt >= P["snr_detect"] * sigma
    wt_d, ko_d = np.where(det, wt, 0.0), np.where(det, ko, 0.0)
    out["detected_rows"] = float(det.mean())
    A = float(wt_d.sum()) or 1e-12
    out["total_share"] = float(np.clip(wt_d - ko_d, 0, None).sum() / A)
    sat_rows = sat[y0:y1]
    out["sat_wt"] = float(sat_rows[:, wt_x[0]:wt_x[1]].mean())
    out["sat_ko"] = float(sat_rows[:, ko_x[0]:ko_x[1]].mean())
    if not lost:
        out["target"] = None
        return out, {"wt": wt, "ko": ko, "wt_raw": wt_raw, "ko_raw": ko_raw, "wt_bg": wt_bg}
    T = max(lost, key=lambda b: b["h"])
    fw = fwhm(wt, T["y"])
    t0, t1 = max(0, T["y"] - fw), min(H, T["y"] + fw + 1)
    near = _near_window(T["y"], H, ladder, y0, P)
    n0, n1 = near
    wt_n, ko_n = wt_d[n0:n1], ko_d[n0:n1]
    out.update(
        target={"y": T["y"], "h": T["h"], "ratio": T["ratio"], "snr": T["snr"],
                "prom_frac": T["prom"] / T["h"], "fwhm": fw,
                "shifted": bool(T.get("shifted")), "kda": _kda(T["y"] + y0, ladder)},
        near=[n0, n1],
        local_share=float(np.clip(wt_n - ko_n, 0, None).sum() / (wt_n.sum() or 1e-12)),
        target_purity=float(np.clip(wt[t0:t1] - ko[t0:t1], 0, None).sum() / (wt[t0:t1].sum() or 1e-12)),
        target_share=float(wt_d[t0:t1].sum() / A),
        sat_target=float(sat_rows[max(0, T["y"] - fw):T["y"] + fw + 1, wt_x[0]:wt_x[1]].mean()),
    )
    kept = [b for b in bands if b not in lost]
    out["strongest_kept_ratio"] = (T["h"] / max(b["h"] for b in kept)) if kept else None
    if kd:
        eff = max(1e-6, 1 - T["ratio"])
        out["local_share_kd"] = min(1.0, out["local_share"] / eff)
        out["total_share_kd"] = min(1.0, out["total_share"] / eff)
    return out, {"wt": wt, "ko": ko, "wt_raw": wt_raw, "ko_raw": ko_raw, "wt_bg": wt_bg}


def _kda_fit(ladder):
    if not ladder or len(ladder) < 2:
        return None
    ys = np.array([float(r) for r, _ in ladder])
    ls = np.log10([float(k) for _, k in ladder])
    b, a = np.polyfit(ys, ls, 1)
    return a, b


def _kda(row, ladder):
    f = _kda_fit(ladder)
    return None if f is None else round(10 ** (f[0] + f[1] * row), 1)


def _near_window(y, H, ladder, y0, P):
    f = _kda_fit(ladder)
    if f is not None and f[1] != 0:
        a, b = f
        kda = 10 ** (a + b * (y + y0))
        r_hi = (math.log10(kda * (1 + P["near_kda_frac"])) - a) / b - y0
        r_lo = (math.log10(kda * (1 - P["near_kda_frac"])) - a) / b - y0
        n0, n1 = sorted((int(r_hi), int(r_lo)))
    else:
        w = int(P["near_lane_frac"] * H)
        n0, n1 = y - w, y + w
    return max(0, n0), min(H, n1 + 1)


# ---------------------------------------------------------------- rules v5

def classify_v5(n, P=None):
    """``(code, reason, flags)``: M Supportive, O Supportive but not selective,
    N Not supportive, ? not measured."""
    P = {**V5_DEFAULTS, **(P or {})}
    if n is None or "error" in (n or {}):
        return "?", (n or {}).get("error", "not measured"), []
    flags = []
    if n.get("sat_target", 0) > 0.001 or n.get("sat_wt", 0) > 0.01:
        flags.append("saturated")
    T = n.get("target")
    if not T:
        return "N", "a: no band lost in the KO above the detection limit", flags
    local = n.get("local_share_kd" if n.get("kd") else "local_share")
    total = n.get("total_share_kd" if n.get("kd") else "total_share")
    lo, to = local >= P["local_min"], total >= P["total_min"]
    for v, t in ((local, P["local_min"]), (total, P["total_min"])):
        if abs(v - t) < P["margin"]:
            flags.append("near threshold")
            break
    why = f"near-target {local:.2f} {'≥' if lo else '<'} {P['local_min']}, total {total:.2f} {'≥' if to else '<'} {P['total_min']}"
    if lo and to:
        return "M", why, flags
    if not lo and not to:
        return "N", why, flags
    return "O", why, flags


# ---------------------------------------------------------------- synthetic

def synthetic(bands_wt, bands_ko, H=600, W=300, noise=0.004, bg=0.05, bits=16,
              bright=True, seed=0):
    """A synthetic two-lane blot with known bands, for the self-test and the
    degradation experiment. Bands are ``(row_fraction, height, width_px)``."""
    rng = np.random.default_rng(seed)
    img = np.full((H, W), bg)
    img += np.linspace(0, bg, H)[:, None]  # uneven background
    lanes = [(int(0.15 * W), int(0.42 * W)), (int(0.58 * W), int(0.85 * W))]
    y = np.arange(H)[:, None]
    for (x0, x1), bands in zip(lanes, (bands_wt, bands_ko)):
        for f, h, wd in bands:
            img[:, x0:x1] += h * np.exp(-0.5 * ((y - f * H) / wd) ** 2)
    img += rng.normal(0, noise, img.shape)
    img = np.clip(img, 0, 1)
    if not bright:
        img = 1 - img
    m = 65535 if bits == 16 else 255
    arr = np.round(img * m).astype(np.uint16 if bits == 16 else np.uint8)
    return arr, lanes
