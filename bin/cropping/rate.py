#!/usr/bin/env python3
"""Press proposed ratings on the review queue, checking each press.

    python bin/cropping/rate.py PLAN.json RATINGS.json

RATINGS.json:

    {"answers":   [["9546", "WB", "Detects the target?", "Yes"], ...],
     "recommend": {"WB": ["ab32378", ...], "IP": [...], "ICC-IF": [...], "FC": [...]},
     "fc_background": {"ab32071": "Yes", "ab32138": "No"}}

The review meeting keeps or changes all of it, and this never presses Release
(the account could: it is a superuser by the owner's decision, 8 Oct 2026).
Four refusals, each stopping the run before the press, not after:
  * an answer only where the card's note says "nothing recorded yet";
  * never on a card not staged by this account, or one already public;
  * never a recommend press on a card already recommended (the button toggles);
  * a press the page did not register (checked on the redrawn card).
Flow's background question only exists on a recommended card, so it is
answered straight after that card's recommend press.
"""
import argparse
import json
import os
import sys

from playwright.sync_api import sync_playwright

from common import SITE, load_plan, start

APPS = ("ICC-IF", "WB", "IP", "FC", "IHC")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("ratings")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    ratings = json.loads(open(opts.ratings).read())
    user = os.environ.get("OGA_TEST_USERNAME", "")
    log = []
    with sync_playwright() as pw:
        browser, page, dialogs, errors = start(pw)
        page.goto(f"{SITE}/pipeline/review/?gene={plan['gene']}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)
        ids = page.evaluate("""() => [...document.querySelectorAll('#grid [data-card]')]
            .map(el => [el.dataset.card, el.innerText.split('\\n').slice(0, 4).join(' | ')])""")
        index = {}
        for cid, text in ids:
            cat = text.split(" | ")[0].strip()
            app = next((a for a in APPS if f" {a} " in f" {text} "), None)
            index[(cat, app)] = cid

        def card(cid):
            return page.locator(f'#grid [data-card="{cid}"]')

        def settled(cid):
            page.wait_for_function(
                'id => { const el = document.querySelector(`#grid [data-card="${id}"]`);'
                ' return el && ![...el.querySelectorAll("button")].some(b => b.disabled)'
                ' && !/saving…/.test(el.innerText); }', arg=cid, timeout=30000)
            page.wait_for_timeout(300)

        def find(cat, app):
            cid = index.get((cat, app))
            if cid is None:
                sys.exit(f"No card for {cat} {app}")
            text = card(cid).inner_text()
            if f"staged by {user}" not in text:
                sys.exit(f"{cat} {app} is not staged by {user} — not pressing")
            if "already public" in text:
                sys.exit(f"{cat} {app} is already public — not pressing")
            return cid

        def question(cid, label):
            return (card(cid).locator("div.mb-2").filter(has=page.locator("button.judge"))
                    .filter(has_text=label).first)

        def answer(cat, app, label, value):
            cid = find(cat, app)
            q = question(cid, label)
            note = q.locator("span.text-right").inner_text().strip()
            if not note.startswith("nothing recorded yet"):
                sys.exit(f"{cat} {app} “{label}”: note is “{note}” — not pressing")
            buttons = q.locator("button.judge")
            match = [buttons.nth(i) for i in range(buttons.count())
                     if buttons.nth(i).inner_text().strip() == value]
            if not match:
                sys.exit(f"{cat} {app} “{label}”: no button “{value}”")
            match[0].click()
            settled(cid)
            q = question(cid, label)
            on = [q.locator("button.judge").nth(i).inner_text().strip()
                  for i in range(q.locator("button.judge").count())
                  if q.locator("button.judge").nth(i).get_attribute("aria-pressed") == "true"]
            if on != [value]:
                sys.exit(f"{cat} {app} “{label}”: page shows {on} after pressing {value}")
            log.append(f"{cat} {app}: {label} → {value}")

        def recommend(cat, app):
            cid = find(cat, app)
            toggle = card(cid).locator(".rec-toggle")
            if toggle.get_attribute("data-on") != "0":
                sys.exit(f"{cat} {app} is already recommended — not pressing")
            toggle.click()
            settled(cid)
            if card(cid).locator(".rec-toggle").get_attribute("data-on") != "1":
                sys.exit(f"{cat} {app}: the recommend press did not register")
            log.append(f"{cat} {app}: recommended")

        for cat, app, label, value in ratings.get("answers", []):
            answer(cat, app, label, value)
        for app, cats in ratings.get("recommend", {}).items():
            for cat in cats:
                recommend(cat, app)
                if app == "FC" and cat in ratings.get("fc_background", {}):
                    answer(cat, "FC", "Non-specific background", ratings["fc_background"][cat])
        browser.close()
    print("\n".join(log))
    print(f"{len(log)} presses, each checked on the page.")
    if dialogs or errors:
        print("Page dialogs:", dialogs, "errors:", errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
