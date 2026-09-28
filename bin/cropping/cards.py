#!/usr/bin/env python3
"""Read the gene's review-queue cards — every answer and where it came from.

    python bin/cropping/cards.py PLAN.json

Read-only. Writes ``cards.json`` beside the plan (one entry per card staged
by this account: catalogue, application, recommended or not, each question
with its note and current answer, any amber warning) and prints the
questions a proposal may answer — the ones whose note says "nothing recorded
yet". Every other note ("recorded at the bench", "from the measured WT/KO
ratio", "set here", "the runs disagree") is an answer somebody gave; rate.py
refuses to press over it.
"""
import argparse
import json
import os
import sys

from playwright.sync_api import sync_playwright

from common import SITE, load_plan, start

READ = """() => [...document.querySelectorAll('#grid [data-card]')].map(el => {
  const head = el.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
  const qs = [...el.querySelectorAll('div.mb-2')].filter(d => d.querySelector('button.judge')).map(d => ({
    label: (d.querySelector('span.font-semibold') || {}).textContent.trim(),
    note: (d.querySelector('span.text-right') || {}).textContent.trim(),
    answer: ([...d.querySelectorAll('button.judge')].find(b => b.getAttribute('aria-pressed') === 'true') || {}).textContent || null,
    options: [...d.querySelectorAll('button.judge')].map(b => b.textContent.trim())}));
  const rec = el.querySelector('.rec-toggle');
  return {id: el.dataset.card, text: head.slice(0, 4).join(' | '),
          recommended: rec ? rec.dataset.on === '1' : null, questions: qs,
          warnings: [...el.querySelectorAll('.border-amber-400')].map(x => x.innerText.trim()),
          already_public: /already public/.test(el.innerText),
          staged_by_me: el.innerText.includes('staged by ' + USER)}; })"""

APPS = ("ICC-IF", "WB", "IP", "FC", "IHC")


def key(card):
    cat = card["text"].split(" | ")[0].strip()
    app = next((a for a in APPS if f" {a} " in f" {card['text']} "), None)
    return cat, app


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    user = os.environ.get("OGA_TEST_USERNAME", "")
    with sync_playwright() as pw:
        browser, page, _, _ = start(pw)
        page.goto(f"{SITE}/pipeline/review/?gene={plan['gene']}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)
        cards = page.evaluate(READ.replace("USER", json.dumps(user)))
        browser.close()
    for c in cards:
        c["catalogue"], c["application"] = key(c)
    mine = [c for c in cards if c["staged_by_me"]]
    (plan["_dir"] / "cards.json").write_text(json.dumps(mine, indent=1))
    print(f"{len(mine)} cards staged by {user} ({len(cards)} on the page). Written to cards.json.")
    open_q = [(c["catalogue"], c["application"], q["label"]) for c in mine for q in c["questions"]
              if q["note"].startswith("nothing recorded yet")]
    print(f"{len(open_q)} questions with nothing recorded:")
    for row in open_q:
        print("  ", *row)
    public = [c["catalogue"] + " " + c["application"] for c in mine if c["already_public"]]
    if public:
        print("Already public — press nothing on these:", public)
    return 0


if __name__ == "__main__":
    sys.exit(main())
