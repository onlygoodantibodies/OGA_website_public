#!/usr/bin/env python3
"""Save the built session's crops to the review queue — the one step that writes.

    python bin/cropping/save.py PLAN.json

Resumes the session ``build.py`` recorded, presses "Save to review queue →"
and reads the consent panel. It presses the panel's save button only if that
button reads "Save N crops [and M whole IHC figures]" for exactly the planned
numbers and the panel names
nothing unforeseen: a new antibody, a replaced published figure, another
gene, or a refusal. Otherwise it presses nothing and prints the panel.

**A crop that replaces a published figure is a stop unless it was asked
for**: ``--replaces N`` says N of these crops are meant to replace published
figures (a reader reported them wrong). Then it ticks "I understand these
crops replace published figures" and requires the panel to say exactly N
replace one, and none is written over the file the live page shows now — add
``--live-now`` only when that too is meant. A western blot is better replaced
on Replace a figure with ``replace.py``, which needs no session.

It then counts the gene's cards staged by this account on the review queue
instead of trusting a receipt's wording (the first run waited five minutes
for a phrase the page did not print, with the save already done).
Nothing here presses Release. The field-test account could — it is a
superuser by the owner's decision (8 Oct 2026) — so that is a rule of this
script, not a permission.
"""
import argparse
import os
import sys

from playwright.sync_api import sync_playwright

from common import SITE, crop_files, load_plan, pick_session, start, txt, whole_figures

UNFORESEEN = ("Not saved", "will be added", "replace a figure", "older name", "new —")
#: The panel's words when a crop lands on the live page's own file (commitSummaryHtml).
LIVE_NOW = "written over the file the live gene page shows"


def queue_count(page, gene):
    page.goto(f"{SITE}/pipeline/review/?gene={gene}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    page.select_option("#show", "mine")
    lines = page.locator("#queue-text-body").text_content().splitlines()
    return len([l for l in lines if " · " in l])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("--replaces", type=int, default=0, metavar="N",
                    help="N of these crops are meant to replace published figures")
    ap.add_argument("--live-now", action="store_true",
                    help="and replacing the file a live page shows, at the save, is meant too")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    expected = len(crop_files(plan))
    wholes = whole_figures(plan)
    # The page's own wording (cropper.html, commitFlow): "Save 5 crops and 1
    # whole IHC figure to the review queue".
    plural = lambda n, w: f"{n} {w}" + ("" if n == 1 else "s")
    label = " and ".join(p for p in (plural(expected, "crop") if expected else "",
                                     plural(wholes, "whole IHC figure") if wholes else "") if p)
    with sync_playwright() as pw:
        browser, page, dialogs, errors = start(pw)
        before = queue_count(page, plan["gene"])
        page.goto(f"{SITE}/pipeline/cropper/?gene={plan['gene']}")
        page.wait_for_load_state("networkidle")
        pick_session(page, plan)
        page.wait_for_function("() => /panels? assigned across/.test("
                               "document.querySelector('#cropper-status').textContent)", timeout=120000)
        if opts.replaces:
            page.wait_for_timeout(1500)
            ack = page.locator("#overwriteack")
            if not ack.is_visible():
                sys.exit("--replaces given, but the page asks for no acknowledgement — "
                         "nothing here replaces a published figure.")
            if not ack.is_checked():
                ack.click()
        page.click("#commit")
        page.wait_for_selector("#commit-yes, #commitstatus:has-text('Not saved')", timeout=180000)
        panel = txt(page, "#commitstatus")
        button = txt(page, "#commit-yes")
        print("Panel:\n" + panel)
        meant = (f"{opts.replaces} replace{'s' if opts.replaces == 1 else ''} "
                 f"a figure already published") if opts.replaces else ""
        bad = [w for w in UNFORESEEN if w in panel and not (meant and w == "replace a figure")]
        if meant and meant not in panel:
            bad.append(f"not “{meant}”")
        if LIVE_NOW in panel and not opts.live_now:
            bad.append(LIVE_NOW)
        if button.strip() != f"Save {label} to the review queue" or bad:
            print(f"STOP — pressed nothing. Button “{button}”, expected “Save {label} to the review "
                  f"queue”; unforeseen: {bad or 'none'}")
            browser.close()
            return 1
        page.click("#commit-yes")
        page.wait_for_function("() => !/Save .* to the review queue\\?/.test("
                               "document.querySelector('#commitstatus').textContent)", timeout=300000)
        page.wait_for_timeout(3000)
        receipt = txt(page, "#commitstatus")
        after = queue_count(page, plan["gene"])
        browser.close()
    print("Receipt:\n" + receipt)
    print(f"Cards staged by {os.environ.get('OGA_TEST_USERNAME')} for {plan['gene']}: "
          f"{before} before, {after} after; {expected} planned.")
    if dialogs or errors:
        print("Page dialogs:", dialogs, "errors:", errors)
    if after < expected:
        print("The queue holds fewer cards than planned — read the receipt above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
