#!/usr/bin/env python3
"""Delete the cropper session this plan built — the end of a rehearsal.

    python bin/cropping/discard.py PLAN.json

For a run that was never meant to be saved: a gene already public, used to
test the tools (PPP2R5D, 28 Sep 2026 — a save there would have written over
the files its live page shows). Deletes only the session ``build.py``
recorded in the plan, through the page's own Delete and its on-page
consent, which removes the session's grid, mapping and uploaded figures and
touches nothing in the review queue. A session somebody means to keep is how
a crop is revised later (``cowork-cropping`` §6), so never run this on one
that was saved to the queue.
"""
import argparse
import sys

from playwright.sync_api import sync_playwright

from common import SITE, load_plan, pick_session, save_plan, start, txt


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    name = plan.get("session")
    if not name:
        sys.exit("No session recorded in the plan — nothing to discard.")
    with sync_playwright() as pw:
        browser, page, dialogs, errors = start(pw)
        page.goto(f"{SITE}/pipeline/cropper/?gene={plan['gene']}")
        page.wait_for_load_state("networkidle")
        label = pick_session(page, plan)
        sid = page.locator("#sessionpick").input_value()
        page.wait_for_timeout(1500)
        page.click("#delsession")
        page.wait_for_selector("#session-delete-yes", timeout=10000)
        print("Asked:", txt(page, "#sessionnotice"))
        page.click("#session-delete-yes")
        page.wait_for_function("() => /Session deleted|not deleted|did not complete/.test("
                               "document.querySelector('#sessionnotice').textContent)", timeout=60000)
        said = txt(page, "#sessionnotice")
        page.reload()
        page.wait_for_load_state("networkidle")
        left = page.locator("#sessionpick option").evaluate_all("os => os.map(o => o.value)")
        browser.close()
    print("Page:", said)
    if sid in left:
        print(f"“{label}” (session {name}) is still in the list.")
        return 1
    plan.pop("session")
    save_plan(plan)
    print(f"“{label}” (session {name}) deleted; the review queue was not touched.")
    if dialogs or errors:
        print("Page dialogs:", dialogs, "errors:", errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
