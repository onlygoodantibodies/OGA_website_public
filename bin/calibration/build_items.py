"""Build the western blot calibration set: synthetic blots and anonymised real ones.

Run once from the repo root; the outputs are committed, so the site never runs
this. Writes

* ``academy/static/academy/calibration/items/<id>.png`` — one image per item,
  named by an opaque id so a filename says nothing about the answer;
* ``academy/data/calibration_items.json`` — the server-side manifest: kind,
  the expected answer and the parameters that drew it. Never served to a rater.

Synthetic blots carry a known answer and the parameters behind it, so the
results page can show where raters put the line (the off-target ratio at which
"main signal" turns into "strong or numerous other bands"). Real blots are
published OGA figures with the antibody name above the blot cropped off and the
lane labels redrawn; their expected answer is a proposal, not a truth.

    python3 bin/calibration/build_items.py
"""
from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT_IMG = ROOT / 'academy/static/academy/calibration/items'
OUT_JSON = ROOT / 'academy/data/calibration_items.json'
MEDIA = 'https://media.onlygoodantibodies.co.uk/experiments/'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

rng = random.Random(20261003)


def font(size):
    return ImageFont.truetype(FONT, size)


# ── synthetic ────────────────────────────────────────────────────────────

W, H = 460, 600
BOX = (150, 70, 390, 560)          # x0, y0, x1, y1 of the membrane
LANES = (210, 330)                  # WT, KO centres
LANE_W = 92
LADDER = (250, 130, 100, 70, 55, 35, 25, 15, 10)


def y_of(kda):
    """Log-linear migration: 250 kDa near the top, 10 kDa near the bottom."""
    lo, hi = np.log10(10), np.log10(250)
    t = (hi - np.log10(kda)) / (hi - lo)
    return BOX[1] + 22 + t * (BOX[3] - BOX[1] - 70)


def band(sig, cx, y, strength, sigma, smile):
    """Add one band to the signal array: a soft-edged bar with a slight smile."""
    ys, xs = np.mgrid[0:H, 0:W]
    dx = (xs - cx) / (LANE_W / 2)
    across = np.exp(-np.abs(dx) ** 6)
    yc = y + smile * dx ** 2
    sig += strength * across * np.exp(-0.5 * ((ys - yc) / sigma) ** 2)


def render(bands_wt, bands_ko, seed, dye=0.5):
    r = np.random.default_rng(seed)
    sig = np.zeros((H, W))
    smile = r.uniform(-2.5, 3.5)
    load = {LANES[0]: r.uniform(0.92, 1.08), LANES[1]: r.uniform(0.92, 1.08)}
    for cx, bands in ((LANES[0], bands_wt), (LANES[1], bands_ko)):
        for kda, s in bands:
            sigma = 2.6 + 2.2 * min(s, 1.6)
            band(sig, cx, y_of(kda), s * load[cx], sigma, smile)
        band(sig, cx, BOX[3] - 16, dye * r.uniform(0.7, 1.2), 4.5, 0)  # dye front
    dark = 1 - np.exp(-1.6 * sig)                       # film-like saturation
    bg = r.uniform(0.03, 0.10) + 0.03 * np.linspace(0, 1, H)[:, None]
    dark = np.clip(dark + bg + r.normal(0, 0.018, (H, W)), 0, 1)
    img = Image.fromarray((255 * (1 - dark)).astype('uint8'))
    img = img.filter(ImageFilter.GaussianBlur(0.9))
    # outside the membrane is paper
    canvas = Image.new('L', (W, H), 255)
    canvas.paste(img.crop(BOX), BOX[:2])
    d = ImageDraw.Draw(canvas)
    d.rectangle(BOX, outline=0, width=2)
    for kda in LADDER:
        y = y_of(kda)
        d.text((BOX[0] - 10, y), f'{kda}-', font=font(20), fill=0, anchor='rm')
    d.text((LANES[0], BOX[1] - 16), 'WT', font=font(24), fill=0, anchor='mm')
    d.text((LANES[1], BOX[1] - 16), 'KO', font=font(24), fill=0, anchor='mm')
    return canvas.convert('RGB')


def shared(n, lo, hi, avoid, ratio_lo, ratio_hi, target):
    """n bands present in both lanes, clear of the target's position."""
    out = []
    while len(out) < n:
        k = round(10 ** rng.uniform(np.log10(lo), np.log10(hi)), 1)
        if all(abs(y_of(k) - y_of(a)) > 14 for a in avoid + [b for b, _ in out]):
            out.append((k, target * rng.uniform(ratio_lo, ratio_hi)))
    return out


def synthetic():
    recipes = [
        ('main_clean', 4), ('main_faint', 6), ('other_strong', 5),
        ('other_many', 4), ('other_gap', 3), ('other_residual', 3),
        ('none_same', 4), ('none_blank', 2), ('none_merged', 3), ('border', 6),
    ]
    items = []
    for recipe, count in recipes:
        for _ in range(count):
            t_kda = round(10 ** rng.uniform(np.log10(18), np.log10(160)), 1)
            t = rng.uniform(0.8, 1.3)
            wt, ko, params = [], [], {'recipe': recipe}
            if recipe == 'main_clean':
                wt = [(t_kda, t)]
                truth = 'main'
            elif recipe == 'main_faint':
                s = shared(rng.randint(1, 3), 15, 220, [t_kda], 0.08, 0.28, t)
                wt, ko = [(t_kda, t)] + s, list(s)
                params['max_ratio'] = round(max(x for _, x in s) / t, 2)
                truth = 'main'
            elif recipe == 'other_strong':
                t *= 0.75
                s = shared(rng.randint(1, 2), 15, 220, [t_kda], 1.1, 1.8, t)
                wt, ko = [(t_kda, t)] + s, list(s)
                params['max_ratio'] = round(max(x for _, x in s) / t, 2)
                truth = 'other'
            elif recipe == 'other_many':
                s = shared(rng.randint(5, 8), 12, 240, [t_kda], 0.5, 0.95, t)
                wt, ko = [(t_kda, t)] + s, list(s)
                params['n_other'] = len(s)
                truth = 'other'
            elif recipe == 'other_gap':
                t *= 0.7
                neighbour = round(t_kda * rng.choice([0.86, 1.16]), 1)
                s = [(neighbour, t * rng.uniform(1.3, 1.7))]
                wt, ko = [(t_kda, t)] + s, list(s)
                truth = 'other'
            elif recipe == 'other_residual':
                s = shared(1, 15, 220, [t_kda], 0.8, 1.2, t)
                wt = [(t_kda, t)] + s
                ko = [(t_kda, t * rng.uniform(0.25, 0.4))] + s
                truth = 'other'
            elif recipe == 'none_same':
                s = shared(rng.randint(1, 4), 15, 220, [], 0.6, 1.2, 1.0)
                wt, ko = list(s), list(s)
                truth = 'none'
            elif recipe == 'none_blank':
                s = shared(1, 15, 220, [], 0.08, 0.15, 1.0)
                wt, ko = list(s), list(s)
                truth = 'none'
            elif recipe == 'none_merged':
                s = [(round(t_kda * 0.95, 1), t)]
                wt = [(t_kda, t * 0.6)] + s
                ko = [(t_kda, t * 0.45)] + s
                truth = 'none'
            else:  # border: off-target bands of intermediate strength
                ratio = rng.uniform(0.45, 0.8)
                s = shared(rng.randint(1, 3), 15, 220, [t_kda], ratio, ratio, t)
                wt, ko = [(t_kda, t)] + s, list(s)
                params['max_ratio'] = round(ratio, 2)
                truth = 'border'
            params['target_kda'] = t_kda
            item_id = f'{rng.getrandbits(32):08x}'
            render(wt, ko, rng.getrandbits(32)).save(OUT_IMG / f'{item_id}.png')
            items.append({'id': item_id, 'kind': 'synthetic', 'expected': truth,
                          'params': params})
    return items


# ── real, anonymised ─────────────────────────────────────────────────────

#: Published OGA figures, two-lane only, with the proposed answer from the
#: criteria doc's anchors. Identity stays in the manifest, never on screen.
REAL = [
    ('BECN1_ab207612_WB_', 'main'), ('TARDBP_', 'main', '89789'),
    ('CHMP2B_', 'main', 'MA5-36184'), ('VAPB_', 'main', '66191-1-Ig'),
    ('CTSB_31399-1-AP_WB_', 'main'), ('ATXN2_', 'main', 'ab254362'),
    ('FUS_', 'main', '11570-1-AP'),
    ('BECN1_ZRB1222_WB_', 'other'), ('CTSB_ZRB1635_WB_', 'other'),
    ('KIF5A_ab5628_WB_', 'other'), ('VAPB_', 'other', 'GTX131631'),
    ('HNRNPA2B1_', 'other', 'GTX127928'), ('CTSB_GTX134724_WB_', 'other'),
    ('NEK1_', 'other', 'PA5-54271'), ('ATXN2_', 'other', 'GTX130329'),
    ('CHMP2B_', 'none', 'MAB7509'), ('VAPB_', 'none', 'A5363'),
    ('KIF5A_67009-1-Ig_WB_', 'none'), ('NEK1_', 'none', 'sc-398813'),
    ('CTSB_ab125067_WB_', 'none'),
]


def anonymise(src):
    """Crop away everything above the membrane and redraw the lane labels."""
    im = src.convert('RGB')
    bbox = ImageChops.difference(im, Image.new('RGB', im.size, 'white')).getbbox()
    if bbox:
        im = im.crop(bbox)
    g = np.asarray(im.convert('L'))
    top = x0 = x1 = None
    for y in range(g.shape[0]):
        dark = g[y] < 90
        best, run, start, s = 0, 0, 0, 0
        for x, v in enumerate(dark):
            run = run + 1 if v else 0
            if run == 1:
                s = x
            if run > best:
                best, start = run, s
        if best > 0.22 * g.shape[1]:
            top, x0, x1 = y, start, start + best
            break
    if top is None:
        raise ValueError('no membrane border found')
    body = im.crop((0, max(0, top - 4), im.width, im.height))
    out = Image.new('RGB', (body.width, body.height + 44), 'white')
    out.paste(body, (0, 44))
    d = ImageDraw.Draw(out)
    w = x1 - x0
    size = max(16, min(30, int(w / 5)))
    d.text((x0 + w * 0.27, 24), 'WT', font=font(size), fill='black', anchor='mm')
    d.text((x0 + w * 0.73, 24), 'KO', font=font(size), fill='black', anchor='mm')
    scale = 560 / out.height
    if scale > 1:
        out = out.resize((int(out.width * scale), 560), Image.LANCZOS)
    return out


#: The figures that print the antibody's name above the membrane — the only
#: ones cropped. The rest print only the lane labels, and the border detector
#: mistakes a dark full-width band for the membrane edge on a figure with no
#: box, so they are left exactly as published.
TITLED = {'ab207612', '31399-1-AP', 'ZRB1222', 'ZRB1635', 'GTX134724',
          'ab125067'}


def as_published(src):
    """Trim the white margin and scale up to the common height; nothing else."""
    im = src.convert('RGB')
    bbox = ImageChops.difference(im, Image.new('RGB', im.size, 'white')).getbbox()
    if bbox:
        im = im.crop(bbox)
    scale = 560 / im.height
    if scale > 1:
        im = im.resize((int(im.width * scale), 560), Image.LANCZOS)
    return im


def real(index):
    """``index`` maps a gene page's figures to catalogue numbers (fetched)."""
    items = []
    for entry in REAL:
        prefix, expected = entry[0], entry[1]
        cat = entry[2] if len(entry) > 2 else prefix.split('_')[1]
        fname = index[(prefix.split('_')[0], cat)]
        raw = subprocess.run(['curl', '-sS', '-m', '30', MEDIA + fname],
                             capture_output=True, check=True).stdout
        tmp = OUT_IMG / '_tmp.png'
        tmp.write_bytes(raw)
        img = (anonymise if cat in TITLED else as_published)(Image.open(tmp))
        tmp.unlink()
        item_id = f'{rng.getrandbits(32):08x}'
        img.save(OUT_IMG / f'{item_id}.png', optimize=True)
        items.append({'id': item_id, 'kind': 'real', 'expected': expected,
                      'params': {'gene': prefix.split('_')[0], 'catalogue': cat,
                                 'figure': fname}})
    return items


def figure_index(genes):
    """(gene, catalogue) → published WB figure filename, read off gene pages."""
    import re
    out = {}
    for gene in genes:
        html = subprocess.run(
            ['curl', '-sS', '-m', '30', f'https://onlygoodantibodies.co.uk/antibodies/{gene}/'],
            capture_output=True, text=True, check=True).stdout
        for m in re.finditer(r'experiments/([^"]+_WB_[^"]+)"', html):
            row = html[html.rfind('<tr', 0, m.start()):m.start()]
            words = [t.strip() for t in re.sub(r'<[^>]+>', '\n', row).split('\n') if t.strip()]
            if words:
                out[(gene, words[0])] = m.group(1)
    return out


def main():
    OUT_IMG.mkdir(parents=True, exist_ok=True)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    for old in OUT_IMG.glob('*.png'):
        old.unlink()
    items = synthetic()
    genes = sorted({e[0].split('_')[0] for e in REAL})
    items += real(figure_index(genes))
    OUT_JSON.write_text(json.dumps({'version': 1, 'items': items}, indent=1) + '\n')
    print(len(items), 'items')


if __name__ == '__main__':
    main()
