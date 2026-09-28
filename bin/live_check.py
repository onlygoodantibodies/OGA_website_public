#!/usr/bin/env python3
"""Sign in to the live pipeline as the field-test account and read its pages.

Run from a cloud session after the startup hook (.claude/hooks/session-start.sh):

    python bin/live_check.py                  # the live site
    python bin/live_check.py --shots DIR      # also save a screenshot per page
    python bin/live_check.py --gene SOD1      # which gene page to open

It is a **reader**. It presses Sign in and Sign out and nothing else — no
board cell, no Add, no Save — so it can run against live data at any time.
Every page it opens is a GET, and every board's rows come from the board's
own fetch, not one this script makes.

What counts as a failure, per page: an HTTP error on the page or on any
request the page itself makes to the site; being sent back to the sign-in
page; the pipeline's "Access Denied"; a JavaScript error; a page with no
<title>; and, on a board, a rows request that did not answer JSON with a
count, or the red "these rows could not be loaded" row. A board that loads
and shows **zero** rows is reported, not failed — an empty filter is a real
answer, but it is worth a human look.

The account comes from ``OGA_TEST_USERNAME`` / ``OGA_TEST_PASSWORD`` (the
cloud environment's settings). It must be a field-test member, never a
superuser. The password is never printed. Exit status is 0 only if every
page passed, so this can gate something rather than be read hopefully.

``/pipeline/`` is never edge-cached (``cache_headers.NEVER_CACHED_PREFIXES``),
so what this reads is what the origin serves now — unlike the public pages,
which need a purge after a deploy before a check means anything.
"""

import argparse
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

SITE = "https://onlygoodantibodies.co.uk"

# (label, path, rows endpoint or None). The four boards the lab works on,
# plus the hub, the guide, Overview and the review queue.
PAGES = [
    ("Hub", "/pipeline/start/", None),
    ("Targets board", "/pipeline/targets/board/", "/pipeline/targets/board/rows/"),
    ("Antibodies board", "/pipeline/antibodies/board/", "/pipeline/antibodies/board/rows/"),
    ("Cell lines board", "/pipeline/cell-lines/board/", "/pipeline/cell-lines/board/rows/"),
    ("Sessions board", "/pipeline/sessions/board/", "/pipeline/sessions/board/rows/"),
    ("Overview", "/pipeline/overview/", None),
    ("Review queue", "/pipeline/review/", None),
    ("Guide", "/pipeline/guide/", None),
]

LOAD_FAILED = "could not be loaded"
TIMEOUT_MS = 30_000


class Page:
    """What one page visit found."""

    def __init__(self, label):
        self.label = label
        self.problems = []
        self.notes = []

    @property
    def ok(self):
        return not self.problems


def launch(pw):
    chrome = os.environ.get("OGA_CHROME") or None
    args = shlex.split(os.environ.get("OGA_CHROME_ARGS", ""))
    return pw.chromium.launch(executable_path=chrome, args=args, headless=True)


def watch(page, host):
    """Collect JS errors and failed same-site requests while a page loads."""
    seen = {"js": [], "http": []}

    def on_console(msg):
        if msg.type == "error":
            seen["js"].append(msg.text[:200])

    def on_response(resp):
        if urlparse(resp.url).hostname == host and resp.status >= 400:
            seen["http"].append(f"{resp.status} {urlparse(resp.url).path}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: seen["js"].append(str(exc)[:200]))
    page.on("response", on_response)
    return seen


def sign_in(page, site, username, password):
    page.goto(f"{site}/accounts/login/?next=/pipeline/start/", timeout=TIMEOUT_MS)
    page.fill("input[name=login]", username)
    page.fill("input[name=password]", password)
    with page.expect_navigation(timeout=TIMEOUT_MS):
        page.click("form button[type=submit]")
    # Success is arriving where ?next= said, and nothing weaker: "not on the
    # sign-in page" read a refused password as signed in, because the refusal
    # was drawn on /academy/login/ (fixed in templates/account/login.html).
    path = urlparse(page.url).path
    if "Access Denied" in page.content():
        return "signed in, but the account is not a pipeline member (Access Denied)"
    if path != "/pipeline/start/":
        # Quote the page's refusal, never the input.
        err = page.locator(".alert-danger, .errorlist, [role=alert]").first
        text = err.inner_text().strip() if err.count() else "no refusal drawn"
        return f"not signed in: landed on {path} ({text[:160]})"
    return None


def visit(page, site, label, path, rows_path, shots):
    result = Page(label)
    host = urlparse(site).hostname
    seen = watch(page, host)
    rows_resp = None
    try:
        if rows_path:
            with page.expect_response(
                lambda r: urlparse(r.url).path == rows_path, timeout=TIMEOUT_MS
            ) as info:
                resp = page.goto(site + path, timeout=TIMEOUT_MS)
            rows_resp = info.value
        else:
            resp = page.goto(site + path, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PlaywrightError as exc:
        result.problems.append(f"did not load: {str(exc).splitlines()[0][:200]}")
        return result

    final = urlparse(page.url).path
    if resp is None or resp.status >= 400:
        result.problems.append(f"page answered {resp.status if resp else 'nothing'}")
    if final.startswith("/accounts/login"):
        result.problems.append("sent back to the sign-in page")
    body = page.content()
    if "Access Denied" in body:
        result.problems.append("Access Denied")
    title = page.title().strip()
    if not title:
        result.problems.append("no <title>")
    if final != path:
        result.notes.append(f"landed on {final}")

    if rows_resp is not None:
        if rows_resp.status != 200:
            result.problems.append(f"rows request answered {rows_resp.status}")
        else:
            try:
                data = rows_resp.json()
            except Exception:
                result.problems.append("rows request did not answer JSON")
            else:
                count = data.get("count")
                if count is None:
                    result.problems.append("rows answer carries no count")
                else:
                    drawn = len(data.get("rows") or [])
                    result.notes.append(f"{count} rows, {drawn} drawn on page 1")
                    if count == 0:
                        result.notes.append("EMPTY — worth a look")
        if LOAD_FAILED in page.inner_text("body"):
            result.problems.append("board drew 'these rows could not be loaded'")

    for line in seen["http"]:
        result.problems.append(f"request failed: {line}")
    for line in seen["js"]:
        result.problems.append(f"JavaScript error: {line}")

    result.notes.insert(0, f"“{title}”")
    if shots:
        name = label.lower().replace(" ", "-") + ".png"
        page.screenshot(path=str(Path(shots) / name), full_page=False)
    return result


def gene_page(page, site, gene, shots):
    """The search box resolves an exact gene to that gene's page."""
    result = visit(page, site, f"Gene page ({gene})", f"/pipeline/find/?q={gene}", None, shots)
    final = urlparse(page.url).path
    if not final.startswith("/pipeline/target/"):
        result.problems.append(f"search for {gene} did not open its gene page (at {final})")
    else:
        result.notes = [n for n in result.notes if not n.startswith("landed on")]
        result.notes.append(f"search resolved to {final}")
    return result


def sign_out(page, site):
    page.goto(f"{site}/pipeline/start/", timeout=TIMEOUT_MS)
    form = page.locator("form[action*='logout'] button, form[action*='logout'] [type=submit]")
    visible = [form.nth(i) for i in range(form.count()) if form.nth(i).is_visible()]
    if not visible:
        return "no visible Sign out control on the hub"
    with page.expect_navigation(timeout=TIMEOUT_MS):
        visible[0].click()
    page.goto(f"{site}/pipeline/start/", timeout=TIMEOUT_MS)
    if not urlparse(page.url).path.startswith("/accounts/login"):
        return f"after signing out, /pipeline/start/ still opened ({urlparse(page.url).path})"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--site", default=SITE)
    parser.add_argument("--gene", default="SOD1")
    parser.add_argument("--shots", help="directory to save one screenshot per page")
    opts = parser.parse_args()

    username = os.environ.get("OGA_TEST_USERNAME")
    password = os.environ.get("OGA_TEST_PASSWORD")
    if not username or not password:
        print("OGA_TEST_USERNAME and OGA_TEST_PASSWORD are not set. Add them in the "
              "cloud environment's settings and start a new session.")
        return 2
    if opts.shots:
        Path(opts.shots).mkdir(parents=True, exist_ok=True)
    site = opts.site.rstrip("/")

    with sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()

        refusal = sign_in(page, site, username, password)
        if refusal:
            print(f"✗ Sign in as {username}: {refusal}")
            browser.close()
            return 1
        print(f"✓ Signed in as {username} → {urlparse(page.url).path}")

        results = [visit(page, site, *p, opts.shots) for p in PAGES]
        results.append(gene_page(page, site, opts.gene, opts.shots))

        out = sign_out(page, site)
        browser.close()

    for r in results:
        print(f"{'✓' if r.ok else '✗'} {r.label}: {' · '.join(r.notes)}")
        for p in r.problems:
            print(f"    ✗ {p}")
    print(f"{'✓' if out is None else '✗'} Sign out{': ' + out if out else ''}")

    failed = sum(not r.ok for r in results) + (out is not None)
    total = len(results) + 2
    print(f"\n{total - failed} of {total} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
