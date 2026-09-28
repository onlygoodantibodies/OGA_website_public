#!/usr/bin/env python3
"""Build the gene's cropper session on the live site and preview every crop.

    python bin/cropping/build.py PLAN.json

Needs ``measure.py`` run first (each figure's ``fit``). Starts a **new**
cropper session, fills box 2 (gene, cell line, genotype, secondary-only line)
and box 3 (the plan's ``antibodies``), then per figure: sets box 4's counts,
adds the file, types every line, ticks every panel and applies the figure's
``order``. Each panel's antibody is read back from its own picker, so a wrong
assignment stops the run rather than reaching a crop.

Then it saves the **session** (private working state, resumable from
"— resume session —"), presses Generate crops, and writes every preview crop
to ``preview/`` with ``preview_<APP>.png`` contact sheets. It never presses
"Save to review queue" — that is ``save.py``.

A figure that must be cut twice (two parts of one page at different heights)
names ``upload_as``: a byte-for-byte copy under that name is uploaded, since
the cropper keeps one grid per figure and the pixels must not change.
"""
import argparse
import base64
import re
import shutil
import sys

from playwright.sync_api import sync_playwright

from common import (SITE, crop_files, load_plan, save_plan, sheets, start, txt,
                    whole_figures, work_dir)


def typeline(page, lid, value):
    """One line position. The change event is sent explicitly: after a refusal
    the box keeps the typed number, so typing it again changes nothing and
    the page would never hear the retry."""
    loc = page.locator("#" + lid)
    loc.wait_for(state="attached", timeout=10000)
    loc.fill(str(value))
    loc.dispatch_event("change")
    page.wait_for_timeout(120)
    why = txt(page, f"#{lid}-why").strip()
    return not (why and ("out of range" in why or "Type a number" in why)), why


def settle(page, pairs, label):
    """Type lines until all are accepted. A split cannot pass an edge that has
    not moved yet (nor the reverse), so the order that works depends on the
    seed; retrying the refused ones after the rest have moved always does."""
    pending, last = list(pairs), ""
    for _ in range(6):
        left = []
        for lid, v in pending:
            ok, why = typeline(page, lid, v)
            if not ok:
                left.append((lid, v))
                last = why
        if not left:
            return
        if len(left) == len(pending):
            break
        pending = left
    sys.exit(f"{label}: could not set {pending}: {last}")


def set_value(page, sel, value):
    """A text box, select or tick in box 5/6 — through the events the page listens for."""
    loc = page.locator(sel)
    loc.wait_for(state="attached", timeout=10000)
    kind = loc.evaluate("e => e.tagName + (e.type ? ':' + e.type : '')")
    if kind == "INPUT:checkbox":
        if loc.is_checked() != bool(value):
            loc.click()
    elif kind.startswith("SELECT"):
        loc.select_option(str(value))
        loc.dispatch_event("change")
    else:
        loc.fill(str(value))
        loc.dispatch_event("input")
        loc.dispatch_event("change")
    page.wait_for_timeout(150)


def set_ihc(page, fig):
    """Box 5's IHC settings: layout first (it redraws the box), then labels and scale."""
    page.wait_for_selector("#ihclayout", state="attached", timeout=20000)
    set_value(page, "#ihclayout", fig.get("ihc_layout", "row"))
    if fig.get("ihc_layout", "row") == "row" and "ihc_labels" in fig:
        set_value(page, "#ihclabels", fig["ihc_labels"])
    set_value(page, "#ihcscale", fig.get("ihc_scale", ""))


WHOLE_FIELDS = {"label": "#ihcpage-label", "legend": "#ihcpage-legend",
                "hap1_pellets": "#ihcpage-hap1", "other_lines": "#ihcpage-otherlines",
                "tissue": "#ihcpage-tissue", "species": "#ihcpage-species",
                "species_other": "#ihcpage-species-other", "organs": "#ihcpage-organs",
                "supplier_key": "#ihcpage-supplierkey", "source": "#ihcpage-source"}
CONTROLS = {"he": "#ihcpage-he", "rabbit_secondary": "#ihcpage-rbsec",
            "mouse_secondary": "#ihcpage-mssec"}


def set_whole(page, fig):
    """Box 6: this figure whole on the gene's IHC page. The first tick fills
    the antibodies from the mapped cells and ticks Tissue, so every field is
    set explicitly afterwards and read back from the page's own state."""
    w = fig["whole"]
    set_value(page, "#ihcpage-on", True)
    page.wait_for_selector("#ihcpage-fields #ihcpage-label, #ihcpage-label", timeout=10000)
    set_value(page, "#ihcpage-crop", w.get("crop", False))
    for key, sel in WHOLE_FIELDS.items():
        if key in w:
            set_value(page, sel, w[key])
    for key, sel in CONTROLS.items():
        set_value(page, sel, key in w.get("controls", []))
    want = {c.lower() for c in w["antibodies"]}
    boxes = page.locator(".ihcpage-ab")
    for i in range(boxes.count()):
        b = boxes.nth(i)
        if b.is_checked() != (b.get_attribute("data-cat").lower() in want):
            b.click()
            page.wait_for_timeout(100)
    got = page.evaluate("() => S.ihcPage")
    wrong = []
    if sorted(c.lower() for c in got["antibodies"]) != sorted(want):
        wrong.append(f"antibodies {got['antibodies']}")
    if sorted(got["controls"]) != sorted(w.get("controls", [])):
        wrong.append(f"controls {got['controls']}")
    if got["label"] != w["label"] or got["legend"] != w["legend"]:
        wrong.append("label or legend")
    if bool(got["crop"]) != bool(w.get("crop", False)):
        wrong.append(f"crop {got['crop']}")
    if wrong:
        sys.exit(f"{fig['name']}: box 6 reads back differently — {'; '.join(wrong)}")
    print(f"{fig['name']}: whole figure “{w['label']}” · {len(want)} antibodies · "
          f"controls {got['controls']} · read back OK")


def cells_to_assign(fig):
    """``[(row, panel), ...]`` in reading order — every panel, or ``assign``."""
    if "assign" in fig:
        pairs = [tuple(int(x) for x in k.split("_")) for k in fig["assign"]]
    else:
        pairs = [(b, c) for b, r in enumerate(fig["fit"]["rows"]) for c in range(len(r["splits"]) + 1)]
    return sorted(pairs)


def add_figure(page, plan, fig):
    src = plan["_dir"] / fig["file"]
    path = src
    if fig.get("upload_as"):
        path = work_dir(plan, "uploads") / fig["upload_as"]
        shutil.copyfile(src, path)
    page.select_option("#app", fig["app"])
    rows = fig["fit"]["rows"] if "fit" in fig else [{"splits": []}]
    counts = [len(r["splits"]) + 1 for r in rows]
    page.locator("#nrows").fill(str(len(rows)))
    page.locator("#nrows").dispatch_event("input")
    same = counts[0] if len(set(counts)) == 1 else ""
    page.locator("#samepanels").fill(str(same))
    page.locator("#samepanels").dispatch_event("input")
    if not same:
        for r, c in enumerate(counts):
            page.locator(f"#rowcount-{r}").fill(str(c))
            page.locator(f"#rowcount-{r}").dispatch_event("input")
    page.set_input_files("#file", str(path))
    page.wait_for_function(
        "n => document.querySelector('#line-top') && "
        "document.querySelector('#cropper-status').textContent.includes(n)",
        arg=path.name, timeout=60000)
    if fig.get("whole_only"):
        set_whole(page, fig)
        return
    fit = fig["fit"]
    settle(page, [("line-top", fit["top"]), ("line-bottom", fit["bottom"])]
           + [(f"line-divider{i}", d) for i, d in enumerate(fit["dividers"], 1)], fig["name"])
    for b, r in enumerate(rows, 1):
        settle(page, [(f"line-row{b}-left", r["left"]), (f"line-row{b}-right", r["right"])]
               + [(f"line-row{b}-split{k}", s) for k, s in enumerate(r["splits"], 1)],
               f"{fig['name']} row {b}")
    got = txt(page, "#line-summary").strip()
    want = "; ".join(
        [f"Row {b}: left {r['left']}, right {r['right']}, "
         + (("splits " + " / ".join(map(str, r["splits"]))) if r["splits"] else "no splits")
         for b, r in enumerate(rows, 1)]
        + [(("dividers " + ", ".join(map(str, fit["dividers"]))) if fit["dividers"] else "no dividers")
           + f"; top {fit['top']}, bottom {fit['bottom']}"])
    if got != want:
        sys.exit(f"{fig['name']}: the page drew other lines than planned\n got  {got}\n want {want}")
    ihc_rows = fig["app"] == "IHC" and fig.get("ihc_layout", "row") == "row"
    if fig["app"] == "IHC":
        set_ihc(page, fig)
    if page.locator("#celllist-wrap").get_attribute("open") is None:
        page.click("#celllist-open")
    cells = cells_to_assign(fig)
    if "assign" in fig:
        for b, c in cells:
            box = page.locator(f"#cell-{b}_{c}-toggle")
            if not box.is_checked():
                box.click()
                page.wait_for_timeout(80)
    else:
        for b in range(len(rows)):
            page.click(f"#cellrow-{b}-all")
            page.wait_for_timeout(100)
    page.locator("#aborder").fill("\n".join(fig["order"]))
    page.locator("#aborder").dispatch_event("input")
    page.click("#aborder-apply")
    page.wait_for_timeout(200)
    applied = txt(page, "#aborder-why")
    if ihc_rows:
        # One antibody per row: the row's own picker says which.
        units = sorted({b for b, _ in cells})
        picked = [page.locator(f"#cellrow-{b}-ab").evaluate(
            "s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : ''") for b in units]
    else:
        picked = [page.locator(f"#cell-{b}_{c}-ab").evaluate(
            "s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : ''") for b, c in cells]
    first = [re.split(r"\s", p.strip())[0] if p.strip() else "" for p in picked]
    if first != fig["order"]:
        sys.exit(f"{fig['name']}: panels read back as {first}, planned {fig['order']}")
    print(f"{fig['name']}: {len(first)} antibodies · {applied} · read back OK")
    if fig.get("whole"):
        set_whole(page, fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    missing = [f["name"] for f in plan["figures"] if "fit" not in f and not f.get("whole_only")]
    if missing:
        sys.exit(f"Run measure.py first — no fit for {missing}")
    unknown = sorted({c for f in plan["figures"] for c in f["order"]} - set(plan["antibodies"]))
    if unknown:
        sys.exit(f"In a figure's order but not in the plan's antibodies: {unknown}")
    out = work_dir(plan, "preview")
    with sync_playwright() as pw:
        browser, page, dialogs, errors = start(pw)
        page.goto(f"{SITE}/pipeline/cropper/?gene={plan['gene']}")
        page.wait_for_load_state("networkidle")
        page.click("#newsession")
        page.wait_for_timeout(500)
        page.locator("#gene").fill(plan["gene"])
        page.locator("#gene").press("Tab")
        page.locator("#cellline").fill(plan["cell_line"])
        page.locator("#cellline").press("Tab")
        page.select_option("#genotype", plan.get("genotype", "KO"))
        if plan.get("fc_secondary") and not page.locator("#fcsec").is_checked():
            page.check("#fcsec")
        page.wait_for_timeout(2000)
        banner = txt(page, "#genestatus")
        print("Gene:", banner)
        if "not in the pipeline yet" in banner or "more than one gene" in banner:
            sys.exit("Stop: the gene is not on file as one gene — see the skill's stop rules.")
        page.locator("#abs").fill("\n".join(plan["antibodies"]))
        page.locator("#abs").dispatch_event("input")
        page.locator("#abs").dispatch_event("change")
        for fig in plan["figures"]:
            add_figure(page, plan, fig)
        print(txt(page, "#cropper-status").splitlines()[-1])
        page.click("#save")
        page.wait_for_function("() => /saved/i.test(document.querySelector('#savestatus').textContent)",
                               timeout=180000)
        page.click("#gen")
        page.wait_for_function("() => !document.querySelector('#preview-summary').hidden", timeout=120000)
        summary = txt(page, "#preview-summary")
        crops = page.evaluate("() => [...document.querySelectorAll('#crops a[download]')]"
                              ".map(a => [a.download, a.href])")
        for name, href in crops:
            (out / name).write_bytes(base64.b64decode(href.split(",", 1)[1]))
        plan["session"] = page.locator("#sessionpick option:checked").inner_text().strip()
        browser.close()
    save_plan(plan)
    expected = len(crop_files(plan))
    print(summary)
    print(f"Session saved as “{plan['session']}”. {len(crops)} crops extracted, {expected} planned"
          f" · {whole_figures(plan)} whole IHC figure(s) set in box 6.")
    for s in sheets(out, crop_files(plan), "preview", plan["gene"]):
        print("sheet:", s)
    if dialogs or errors:
        print("Page dialogs:", dialogs, "errors:", errors)
    if len(crops) != expected:
        sys.exit("The preview does not hold every planned crop.")


if __name__ == "__main__":
    sys.exit(main())
