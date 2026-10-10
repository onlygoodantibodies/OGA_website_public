#!/usr/bin/env python3
"""Replace published western-blot figures through **Replace a figure**.

    python bin/cropping/replace.py PLAN.json          # cut, render, compare — writes nothing
    python bin/cropping/replace.py PLAN.json --go     # upload each to the review queue

For the case a reader reports one published figure as wrong (cut off ladder
labels, the wrong panel) and the fix is a fresh cut from the report's own
figure. The plan is ``measure.py``'s, with ``assign`` naming only the cells
being replaced and ``order`` their antibodies:

    {"gene": "LRRK2", "antibodies": ["36408"],
     "figures": [{"name": "WB1", "file": "LRRK2_WB_Figure1.png", "app": "WB",
                  "bands": [...], "assign": ["1_2"], "order": ["36408"]}]}

**Why this door and not the cropper.** Replace a figure
(``/pipeline/figures/replace/``, ``pipeline/services/figure_replace.py``) is
the app's one-antibody, one-application door: it shows the figure on the site
beside the upload, carries the antibody's recommendation and the figure's
control kind over unchanged, and says in its receipt whether the live page
changed now or changes at release. A cropper session would also do it, but
its save treats a replaced figure as a warning to be ticked past, and leaves
a session behind for a job that is one panel.

**Western blot only, because only there is the result exact.** The page puts
an upload on the cropper's canvas with ``engine.fit_to_canvas`` — trim, scale,
paste — which is what ``render_cell`` does for WB, where the cropper draws
nothing of its own. So the cell is cut from the figure (a crop of the file's
own pixels, nothing else) and uploaded as it is, and the server's stored
image is the cropper's to the pixel; ``replace/after_<file>`` is rendered
locally with the same engine so it can be checked first. IP, ICC-IF, FC and
IHC crops carry a legend or labels the cropper draws, which this page does
not — those go through ``build.py`` and ``save.py --replaces``.

Refuses, before writing anything: a catalogue the page finds no single
antibody for on that gene, and an antibody with no published figure in that
application (that is an addition, not a replacement — use the cropper).
Nothing here presses Release: releasing is the review queue's. The account
could (a superuser, by the owner's decision of 8 Oct 2026), so that is a rule
of this script, not a permission.
"""
import argparse
import sys
from pathlib import Path
from urllib.parse import urlencode

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

from common import SITE, fetch, load_plan, start, txt, work_dir

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.services.cropper import engine  # noqa: E402  (pure PIL, no Django)


def cells(plan):
    """``[(fig, cat, (l, t, r, b)), ...]`` — the rectangle the cropper's lines
    enclose for each assigned cell, in the source file's pixels."""
    out = []
    for fig in plan["figures"]:
        if "fit" not in fig:
            sys.exit(f"Run measure.py first — no fit for {fig['name']}")
        if fig["app"] != "WB":
            sys.exit(f"{fig['name']} is {fig['app']}: only a western blot can be replaced "
                     f"exactly through Replace a figure. Use build.py and save.py --replaces.")
        if "assign" not in fig:
            sys.exit(f"{fig['name']}: name the cells being replaced in 'assign'.")
        fit = fig["fit"]
        ys = [fit["top"]] + fit["dividers"] + [fit["bottom"]]
        for key, cat in zip(sorted(fig["assign"]), fig["order"]):
            r, c = (int(v) for v in key.split("_"))
            row = fit["rows"][r]
            xs = [row["left"]] + row["splits"] + [row["right"]]
            out.append((fig, cat, (xs[c], ys[r], xs[c + 1], ys[r + 1])))
    return out


def find_slot(page, gene, cat, app):
    """The page's own form for this antibody and application, and its live image."""
    page.goto(f"{SITE}/pipeline/figures/replace/?{urlencode({'q': cat, 'gene': gene})}")
    page.wait_for_load_state("networkidle")
    found = page.evaluate("""([cat, app]) => {
        const norm = s => (s || '').replace(/\\*/g, '').trim().toLowerCase();
        return [...document.querySelectorAll('form[action*="figures/replace/upload"]')]
          .filter(f => f.querySelector('[name=application_type]').value === app)
          .map(f => {
            const card = f.closest('.rounded-lg');
            const slot = f.parentElement;
            const img = slot.querySelector('img[alt^="Published"]');
            return {id: f.querySelector('[name=antibody_id]').value,
                    catalogue: card.querySelector('strong').textContent.trim(),
                    line: card.querySelector('p').textContent.replace(/\\s+/g, ' ').trim(),
                    live: img ? img.src : ''};
          })
          .filter(r => norm(r.catalogue) === norm(cat));
    }""", [cat, app])
    if len(found) != 1:
        sys.exit(f"{gene} {cat}: Replace a figure lists {len(found)} antibodies with that "
                 f"catalogue — expected one. {[r['line'] for r in found]}")
    slot = found[0]
    if not slot["live"]:
        sys.exit(f"{gene} {cat}: no published {app} figure, so this would add one rather "
                 f"than replace it. Crop it with the cropper instead.")
    return slot


def sheet(pairs, dest):
    """Before (the live file) beside after (what will be stored), per antibody."""
    tile = 360
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    out = Image.new("RGB", (2 * tile, len(pairs) * (tile + 28)), "white")
    d = ImageDraw.Draw(out)
    for i, (cat, before, after) in enumerate(pairs):
        y = i * (tile + 28)
        for j, (path, word) in enumerate(((before, "on the site"), (after, "replacement"))):
            im = Image.open(path).convert("RGB")
            im.thumbnail((tile, tile))
            out.paste(im, (j * tile, y))
            d.rectangle([j * tile, y + tile, (j + 1) * tile - 4, y + tile + 26], fill=(255, 235, 120))
            d.text((j * tile + 4, y + tile + 3), f"{cat} — {word}", fill="black", font=font)
    out.save(dest)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("--go", action="store_true", help="upload to the review queue")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    todo = cells(plan)
    out = work_dir(plan, "replace")
    pairs, ready = [], []
    with sync_playwright() as pw:
        browser, page, dialogs, errors = start(pw)
        for fig, cat, box in todo:
            source = Image.open(plan["_dir"] / fig["file"])
            name = engine.filename_for(plan["gene"], cat, fig["app"])
            cut = out / f"cut_{name}"
            source.crop(box).save(cut)                       # the file's own pixels
            after = out / f"after_{name}"
            engine.fit_to_canvas(Image.open(cut), fig["app"]).save(after)
            slot = find_slot(page, plan["gene"], cat, fig["app"])
            before = out / f"before_{name}"
            fetch(slot["live"], before)
            pairs.append((cat, before, after))
            ready.append((cat, fig["app"], cut, slot))
            print(f"{cat}: cell {box} of {fig['file']} · {slot['line']}\n"
                  f"   on the site: {slot['live']}")
        print("sheet:", sheet(pairs, plan["_dir"] / "replace_sheet.png"))
        if not opts.go:
            print("Nothing uploaded. Look at the sheet — every label whole, the right "
                  "antibody's title in each — then run again with --go.")
            browser.close()
            return 0
        failed = 0
        for cat, app, cut, slot in ready:
            find_slot(page, plan["gene"], cat, app)        # fresh page, same checks
            form = page.locator(f'form:has(input[name=antibody_id][value="{slot["id"]}"])'
                                f':has(input[name=application_type][value="{app}"])')
            form.locator("input[type=file]").set_input_files(str(cut))
            with page.expect_navigation():
                form.locator("button[type=submit]").click()
            page.wait_for_load_state("networkidle")
            said = txt(page, "#receipt") or txt(page, "#refusal") or "(no receipt drawn)"
            said = " ".join(said.split())
            ok = said.startswith(f"Queued: {plan['gene']} · {cat}")
            failed += not ok
            print(f"{cat} {app}: {'queued' if ok else 'NOT queued'} — {said}")
        browser.close()
    if dialogs or errors:
        print("Page dialogs:", dialogs, "errors:", errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
