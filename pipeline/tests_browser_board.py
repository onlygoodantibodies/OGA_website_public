"""The boards in a real browser.

CLAUDE.md: **"the server answered correctly and the page did the wrong thing
with it" is the shape you must drive a browser for.** Four of five such findings
across runs 4 and 6 were refuted in a real browser, and the one released defect
that source-level tests let through — a helper declared in one function and
called from another — is valid syntax that only fails when the line runs.

These are the checks for the corrected entry conventions. One of them found a
real bug that no source reading had: a knockout row naming a gene not yet in the
pipeline matched the **wild type of the same name**, because the two genotypes
were separated by `target` and an unknown gene resolves to `target=None`. See
`tests_conventions.py::AKnockoutRowNeverMatchesAWildTypeTests`, which pins it at
the source as well — that is weaker, and it is what runs where there is no
browser.

**Skipped unless playwright and the bundled Chromium are both present**, the
same way `BoardScriptsParseTests` skips where node is absent. `pip install
playwright` (it is deliberately not in requirements.txt — the deploy does not
need it) and run with `DJANGO_ALLOW_ASYNC_UNSAFE=1`, which the sync playwright
API requires because it drives an event loop Django's ORM refuses to be called
from.

Tailwind comes from a CDN, so styling is absent offline: assert on classes and
on text, never on what is visible.
"""
from __future__ import annotations

import glob
import os
import re
import unittest
from decimal import Decimal
from importlib import import_module
from unittest import mock

import requests
from django.conf import settings
from django.contrib.auth import (BACKEND_SESSION_KEY, HASH_SESSION_KEY,
                                 SESSION_KEY)
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings, tag

from pipeline.models import (Antibody, CellLine, CellLineVial, Member, Site,
                             Target)


def _one_pixel_png() -> bytes:
    """A real PNG, small enough to be free. The review queue draws the staged
    crop, so a fixture that is not an image makes the page report a broken
    <img> and the failure reads as the release having gone wrong."""
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()

DB = "pipeline_db"


def _find_chrome() -> str:
    """Where Chromium actually is, across the layouts playwright uses.

    This looked only in `/opt/pw-browsers/chromium-*/chrome-linux/chrome`, which
    is right for the container the field tests run in and wrong everywhere else:
    newer playwright unpacks to `chrome-linux64`, and a plain `playwright
    install` uses `~/.cache/ms-playwright` rather than /opt. Both misses land as
    a *skip*, which is the failure mode this file is least able to afford — CI
    installed Chromium successfully and then reported `OK (skipped=26)`, caught
    only because the workflow greps for the skip and fails on it.

    Searching more places can only ever add a browser, never pick a wrong one.
    """
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "",
             "/opt/pw-browsers",
             os.path.expanduser("~/.cache/ms-playwright")]
    for root in roots:
        if not root:
            continue
        for layout in ("chrome-linux", "chrome-linux64"):
            found = sorted(glob.glob(f"{root}/chromium-*/{layout}/chrome"))
            if found:
                return found[0]
    return ""


CHROME = _find_chrome()

try:
    import playwright.sync_api  # noqa: F401
    HAVE_PLAYWRIGHT = True
except Exception:
    HAVE_PLAYWRIGHT = False

# The sync API drives its own event loop; Django's ORM refuses to be called from
# one unless this is set. Setting it here rather than asking every runner to
# remember keeps the skip the only reason this file does not run.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")


# ── `@tag("commissioning")` ─────────────────────────────────────────────────
#
# Two kinds of test live in this file and only one of them earns a run on every
# edit.
#
# A **regression** test guards something that can go wrong *silently* — a write
# that lands in the wrong place, a count that disagrees with its list, a save
# that reports success and stores nothing. Almost everything here is one, which
# is why almost nothing is tagged.
#
# A **commissioning** test proved a specific thing worked when it was built, and
# its failure would be loud: the reader sees it happen, and no record is
# changed by it. That is worth checking, and not worth checking after every
# edit to an unrelated file. Tagged tests are excluded from the push run and
# run on the nightly one (`.github/workflows/browser.yml`), so the coverage is
# kept and the cost leaves the hot path.
#
# The bar for adding the tag is both halves — **visible when it breaks, and
# nothing written**. If a regression would be discovered later, in a report, it
# is not a commissioning test however narrow it looks.
#
# ── the sentence above was true and the file stopped obeying it ─────────────
#
# "Almost nothing is tagged" was written when there were 26 tests here and
# three were tagged. On 6 Aug 2026 there were **74**, still three tagged, so
# the split that was meant to keep the push path cheap was holding back 4% of
# it — every edit to a template paid for all 71. Measured, the file is flat:
# 66 s of test time, nothing over 3 s, a median of 0.7 s. There is no slow test
# to fix; the cost *is* the count, at roughly 0.9 s each. So the only lever is
# which tests earn a run on every push, and it has to be pulled deliberately
# rather than left to whoever adds the next one.
#
# What stays on the push tier is what fails **silently**: a write that lands in
# the wrong row, a count that disagrees with the list under it, a save that
# reports success and stores nothing, a control the page draws and does not
# send, a gate read once instead of per draw. What moved to the nightly is the
# rest — a picker that offers nothing, a label drawn twice, a refusal that
# fires on the wrong branch. Those are real and they are checked; they are
# also the first thing the reader sees, and none of them writes a record.
#
# Two things that are *not* reasons to tag, because both were tempting here:
# a test being slow (the expensive ones are mostly the end-to-end writes, which
# is exactly what must not move), and a test being narrow (narrow and silent is
# the most valuable kind).
@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class BoardInARealBrowserTests(StaticLiveServerTestCase):
    # A plain LiveServerTestCase does not serve board.js, so the grid never
    # loads and every assertion times out looking like the bug you came for.
    databases = {DB, "academy_db"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from playwright.sync_api import sync_playwright
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        Member.objects.using(DB).create(user_id=pu.pk, site_id=self.site.pk,
                                        role="experimenter", is_active=True,
                                        display_name="Vera")
        self.target = Target.objects.using(DB).create(gene_name="SNCA",
                                                     protein_name="Syn")
        wt = CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                              site=self.site)
        CellLineVial.objects.using(DB).create(cell_line=wt, c_number=48)
        self.page = self.browser.new_page()
        self.errors = []
        # Tailwind is served from a CDN, so offline it 404s and every page
        # reports `tailwind is not defined`. That is the environment, not the
        # page — CLAUDE.md says to assert on classes, not on what is visible.
        def _note(text):
            if ("TUNNEL_CONNECTION_FAILED" in text
                    or "tailwind is not defined" in text
                    or "Failed to load resource" in text):
                return
            self.errors.append(text)
        self.page.on("response", lambda r: self.errors.append(f"{r.status} {r.url}")
                     if r.status >= 400 else None)
        self.page.on("pageerror", lambda e: _note(str(e)))
        self.page.on("console", lambda m: _note(m.text) if m.type == "error" else None)
        self._sign_in()

    def _sign_in(self):
        """Hand the page a session cookie instead of driving the login form.

        Every test paid a page load, two fills, a form round trip and a
        `networkidle` wait to reach the same state — 37 times, in `setUp`, which
        is the shape that cost this repo sixteen of its seventeen test minutes
        once already. `pipeline_login` calls plain `login(request, user)` and
        puts nothing else in the session, so a forged one is the same session.

        `test_the_login_form_signs_you_in` still walks the real form. Without
        it this shortcut would silently retire the only browser coverage the
        login page has — the door every user comes through, tested nowhere else.
        """
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        user = User.objects.using("academy_db").get(username="vera")
        store[SESSION_KEY] = str(user.pk)
        store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        store[HASH_SESSION_KEY] = user.get_session_auth_hash()
        store.save()
        self.page.context.add_cookies([{
            "name": settings.SESSION_COOKIE_NAME,
            "value": store.session_key,
            "url": self.live_server_url,
        }])

    def tearDown(self):
        self.page.close()

    def _settled(self, selector, before, timeout=20000):
        """Wait for a panel to say something different from what it said before.

        The generic form of `_checked` below, for the operations whose reply
        text nobody can predict — a commit, a delete, a redraw. It replaces a
        `wait_for_timeout` guess with the thing that is actually true: the panel
        has been rewritten. Capture `inner_text` *before* the click and pass it
        in.

        A sleep is slow when the guess is too high and flaky when it is too low,
        and this suite had 26.7 s of them. One had already come up short on CI
        and been rewritten (`_checked`); these are the rest.
        """
        self.page.wait_for_function(
            "([sel, prev]) => { const el = document.querySelector(sel);"
            "                   return el && el.innerText !== prev; }",
            arg=[selector, before], timeout=timeout)

    def _gone(self, pk, timeout=20000):
        """Wait for a deleted row to actually leave the grid."""
        self.page.wait_for_selector(
            f'tr[data-row="{pk}"]', state="detached", timeout=timeout)

    def _posted(self, action, timeout=20000):
        """Do `action` and wait for the write it triggers to come back.

        The right condition for anything that asserts on the *database*: the
        server has written by the time it replies, so the POST returning is the
        fact, and it needs no guess about wording or redraw.

        Watching the DOM instead is what a fixed sleep was really standing in
        for, and it is wrong in both directions here. The drawer's inline edit
        removes its `<input>` the moment you press Enter — optimistically,
        before the request is sent — so waiting for that to detach returned
        early and read an empty `comments` column. A panel's text can equally
        change to an interim line and back. The response is the only thing that
        cannot happen before the write.
        """
        with self.page.expect_response(
                lambda r: r.request.method == "POST", timeout=timeout):
            action()

    def _checked(self, panel="#new-panel"):
        """Wait for a Check to come back, rather than guessing how long it takes.

        `newEntry` writes "Checking…" into its output synchronously on the click
        and replaces it when the reply lands, so the wait has something true to
        watch. It was `wait_for_timeout(900)` — a guess, made on a dev machine
        with a blocked network and nothing else running. CI has neither, and on
        4 Aug the guess came up short: the panel was still reading "Checking…"
        when the assertion ran, and the failure named a missing link rather than
        a slow request. A fixed sleep before an assertion about a network round
        trip is a coin toss with a plausible-looking error message.
        """
        self.page.wait_for_function(
            "sel => { const el = document.querySelector(sel);"
            "         return el && !el.innerText.includes('Checking'); }",
            arg=panel, timeout=20000)

    def _saved(self, panel="#new-panel"):
        """Wait for a Create to come back, the way `_checked` waits for a Check.

        `newEntry` writes "Creating…" into its output synchronously on the
        click, so there is something true to watch. `_settled` is the wrong tool
        here for the reason its own sibling `_posted` gives: the panel's text
        changing is satisfied by the *interim* line, so a wait for "different
        from before" returns while the request is still in flight.
        """
        self.page.wait_for_function(
            "sel => { const el = document.querySelector(sel);"
            "         return el && !el.innerText.includes('Creating'); }",
            arg=panel, timeout=20000)

    def _open_add_panel(self):
        self.page.goto(f"{self.live_server_url}/pipeline/cell-lines/board/")
        self.page.wait_for_selector("#new-btn")
        self.page.click("#new-btn")
        self.page.wait_for_selector("#new-panel table tbody input")

    def test_the_login_form_signs_you_in(self):
        """The door every user comes through, and the only test that opens it.

        `setUp` used to log in through this form 37 times, which covered the
        page by accident. `_sign_in` now forges the session instead, so without
        this test the login page would have no browser coverage at all — the
        same shape as an export with no importer, one layer along: the cost of
        the shortcut is invisible until somebody breaks the form.
        """
        self.page.context.clear_cookies()
        self.page.goto(f"{self.live_server_url}/pipeline/login/")
        self.page.fill("input[name=username]", "vera")
        self.page.fill("input[name=password]", "pw")
        self.page.click("button[type=submit], input[type=submit]")
        # `nav a`, not a bare `[href*='/pipeline/']`: the stylesheet is served
        # from /static/pipeline/, so the loose form matched the `<link>` in the
        # head — which is never visible, so the wait sat there for 30s and
        # reported a timeout about the login form.
        self.page.wait_for_selector("#task-hub, nav a[href*='/pipeline/']")
        self.assertNotIn("/pipeline/login/", self.page.url,
                         "a correct password left us on the login page")

        # And a wrong one is refused in words, on the page.
        self.page.context.clear_cookies()
        self.page.goto(f"{self.live_server_url}/pipeline/login/")
        self.page.fill("input[name=username]", "vera")
        self.page.fill("input[name=password]", "not-the-password")
        self.page.click("button[type=submit], input[type=submit]")
        self.page.wait_for_selector("text=Invalid username or password")

    def test_the_grid_loads_and_the_console_is_clean(self):
        self.page.goto(f"{self.live_server_url}/pipeline/cell-lines/board/")
        # `#row-count` exists before the rows arrive; wait for it to say
        # something rather than guessing how long the fetch takes.
        self.page.wait_for_function(
            "() => (document.querySelector('#row-count')?.innerText || '').trim()")
        self.assertIn("cell line", self.page.inner_text("#row-count"))
        self.assertEqual(self.errors, [])

    # Both of the next two tests route the stylesheet rather than fetching it:
    # one aborts the request, the other answers it with a stub. That is the
    # point. Four browser tests passed for months and then failed the day CI
    # ran somewhere the CDN loaded, because they had quietly been asserting
    # about whichever network they happened to be on. A test about what
    # happens when styling is missing must not be one of those.
    #
    # The pattern matches *both* sources — this site's built file and the CDN
    # it falls back to — so these say the same thing whether or not
    # `bin/build_css.sh` has been run in the checkout they are running in.
    STYLESHEET = re.compile(r"cdn\.tailwindcss\.com|/static/pipeline/tailwind\.css")

    def test_a_stylesheet_that_never_arrives_leaves_the_page_navigable(self):
        """University and hospital networks block outside CDNs.

        `hidden` is Tailwind's class and it is how every dropdown, modal,
        delete confirmation and upload panel in this app is kept shut — so
        with no stylesheet they all render open and stacked down the page,
        and the nav's white text sits on no background at all. The fallback
        in `_styling.html` is what stands between that and a plain page.

        It can only ever run on a network nobody develops on, so nothing
        would notice it rotting until the day it was needed. That is what
        this pins.
        """
        self.page.route(self.STYLESHEET, lambda route: route.abort())
        self.page.goto(f"{self.live_server_url}/pipeline/cell-lines/board/")
        self.page.wait_for_selector("#styling-fallback-note")

        self.assertIn("no-tailwind",
                      self.page.get_attribute("html", "class") or "",
                      "the CDN failed and the fallback was not switched on")

        # Say the records are fine. The fear on reaching a screen that looks
        # broken is that the data behind it went too.
        note = (self.page.text_content("#styling-fallback-note") or "").lower()
        self.assertIn("nothing about your records is affected", note)

        # The thing the fallback exists for.
        self.assertFalse(self.page.is_visible("#browse-menu"),
                         "a closed dropdown rendered open with no stylesheet")

        # And the other half: the nav's links, search box and account controls
        # are `hidden md:flex`, so a fallback that taught `.hidden` and stopped
        # there would hide the navigation at every width.
        self.assertTrue(self.page.is_visible("#browse-btn"),
                        "the fallback hid the nav it was meant to rescue")

    # a fallback that leaked onto a working page would announce itself on
    # every one of them
    @tag("commissioning")
    def test_the_fallback_stays_out_of_the_way_when_the_stylesheet_loads(self):
        """The ordinary path, which is every page load that works.

        Nothing in the fallback may reach it — the rules are scoped under a
        class that only the two failure paths ever add.

        Only the CDN is stubbed: this site's own stylesheet is served by the
        live server, so where the build has been run it simply loads, and
        where it has not the stub stands in for the CDN. Either way the page
        gets styling without touching the network.
        """
        self.page.route(re.compile(r"cdn\.tailwindcss\.com"),
                        lambda route: route.fulfill(
                            status=200, content_type="application/javascript",
                            body="window.tailwind = {};"))
        self.page.goto(f"{self.live_server_url}/pipeline/cell-lines/board/")
        self.page.wait_for_selector("#browse-btn")

        self.assertNotIn("no-tailwind",
                         self.page.get_attribute("html", "class") or "")
        self.assertIsNone(self.page.query_selector("#styling-fallback-note"),
                          "the fallback announced itself on a working page")

    def test_the_example_is_a_header_row_and_row_one_is_empty(self):
        self._open_add_panel()
        head = self.page.inner_text("#new-panel table thead")
        # Case-folded because the header is `uppercase` in CSS: `inner_text`
        # returns what is rendered, so this read "e.g." with Tailwind absent
        # and "E.G." with it loaded. Asserting on rendered case makes the test
        # a test of whether the CDN was reachable.
        self.assertIn("e.g.", head.lower())
        self.assertIn("HAP1", head)
        self.assertIn("C-48", head)
        # Row 1's inputs are genuinely empty — no value, and no placeholder
        # pretending the row is filled in.
        first_row = self.page.query_selector_all(
            "#new-panel table tbody tr:first-child input")
        self.assertTrue(first_row)
        for inp in first_row:
            self.assertEqual(inp.input_value(), "")
            self.assertIn(inp.get_attribute("placeholder") or "", ("", None))
        # And it cannot be typed into or saved: it is in the head, so `tsv()` —
        # which reads tbody — cannot see it. This was a test of its own, which
        # is a second page load and a second panel open for one assertion about
        # the panel already on screen.
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#new-panel table thead input", "els => els.length"), 0)
        self.assertEqual(self.errors, [])

    def test_the_preview_names_what_each_row_resolved_to(self):
        """Three rows, one check — the preview's per-row notes.

        This was three tests, each opening the panel, filling row 1 and
        pressing Check for one sentence: an unknown gene offering the target
        board, a parent given as a C-number, and `NA` in the gene column being
        the honest answer for a wild type rather than a gene called NA. One
        panel, one round trip, the same three assertions. What each *note* says
        is `bulk_cell_lines.plan`'s and is pinned far more cheaply in
        `services/tests/`; what needs a browser is that `renderPreview` draws
        the note against the row it belongs to, which one paste shows better
        than three.
        """
        self._open_add_panel()
        rows = [("HAP1", "TRPA1", "KO", ""),       # a gene not in the pipeline
                ("HAP1", "SNCA", "KO", "C-48"),    # a parent by C-number
                ("HeLa", "NA", "", "")]            # a wild type, honestly
        for n, (name, gene, genotype, parent) in enumerate(rows, start=1):
            sel = f"#new-panel table tbody tr:nth-child({n})"
            self.page.fill(f"{sel} input[data-c='0']", name)
            self.page.fill(f"{sel} input[data-c='1']", gene)
            if genotype:
                self.page.fill(f"{sel} input[data-c='2']", genotype)
            if parent:
                self.page.fill(f"{sel} input[data-c='3']", parent)

        self.page.click("#new-panel button:has-text('Check these')")
        self._checked("#new-panel")
        out = self.page.inner_text("#new-panel")

        # An unknown gene is a link to the one place it can be added.
        self.assertIn("Add the gene as a new target", out)
        link = self.page.query_selector("#new-panel a:has-text('Add TRPA1')")
        self.assertIsNotNone(link, out[-800:])
        self.assertIn("/targets/board/", link.get_attribute("href"))
        self.assertIn("gene=TRPA1", link.get_attribute("href"))

        # A parent written as a C-number resolves, and says whose line it is.
        self.assertIn("C-48", out)
        self.assertIn("your site", out)

        # `NA` is "there isn't one", not a gene.
        self.assertIn("wild type", out.lower())
        self.assertNotIn("blocked", out.lower())
        self.assertEqual(self.errors, [])

    def test_a_big_board_draws_one_page_and_says_so(self):
        """The review got "page unresponsive" on 3,225 antibodies drawn in one
        `innerHTML`. 120 is enough to prove the slice and the pager without
        making the test itself slow."""
        from pipeline.models import Antibody, Company
        company = Company.objects.using(DB).create(name="Abcam")
        for i in range(120):
            Antibody.objects.using(DB).create(
                target_id=self.target.pk, company_id=company.pk,
                catalogue_number=f"ab{2000 + i}", site_id=self.site.pk)

        self.page.goto(f"{self.live_server_url}/pipeline/antibodies/board/")
        self.page.wait_for_selector("#row-pager button")
        drawn = self.page.eval_on_selector_all(
            "#antibody-grid tr, #board-grid tr, tbody tr[data-row]",
            "els => els.filter(e => e.dataset.row).length")
        self.assertEqual(drawn, 50, "the grid drew more than one page")
        self.assertIn("120", self.page.inner_text("#row-count"))
        self.assertIn("1–50 of 120", self.page.inner_text("#row-pager"))

        # `#row-count` is deliberately the whole filtered set and does NOT
        # change between pages — that is the rule the board exists to keep. The
        # pager is what moves.
        before = self.page.inner_text("#row-pager")
        self.page.click("#row-pager button[data-page='2']")
        self._settled("#row-pager", before)
        self.assertIn("51–100 of 120", self.page.inner_text("#row-pager"))
        # The count beside the filters is still the whole filtered set.
        self.assertIn("120", self.page.inner_text("#row-count"))

        # **And the download is not scoped to the page.** `page` is board state
        # and not a filter field, so the export href is built from the same form
        # the rows fetch is — a paginated board that handed back 50 of 120 rows
        # would be a silent loss on the one action taken to keep the data. This
        # was a test of its own on a board with no second page, which is the one
        # place the assertion cannot fail.
        self.assertNotIn("page=", self.page.get_attribute("#dl-btn", "href") or "")
        self.assertEqual(self.errors, [])

    @tag("commissioning")
    def test_a_bookmarked_session_opens_even_from_a_later_page(self):
        """`?open=<id>` is where a bookmark of the retired session page lands.
        With one page drawn, a session further down was reported as "outside
        the current filters" — false, and clearing them makes it worse."""
        from pipeline.models import ExperimentSession, Member
        member = Member.objects.using(DB).get(site_id=self.site.pk)
        sessions = [
            ExperimentSession.objects.using(DB).create(
                target_id=self.target.pk, procedure_type="WB",
                date=f"2026-0{1 + i // 28}-{1 + i % 28:02d}",
                site_id=self.site.pk, experimenter_id=member.pk)
            for i in range(70)]
        oldest = min(sessions, key=lambda s: s.date)

        self.page.goto(
            f"{self.live_server_url}/pipeline/sessions/board/?open={oldest.pk}")
        self.page.wait_for_selector(f'tr[data-row="{oldest.pk}"]', timeout=15000)
        # It landed on the row's own page, and said nothing false on the way.
        self.assertIn("51–70 of 70", self.page.inner_text("#row-pager"))
        body = self.page.inner_text("body")
        self.assertNotIn("outside the current filters", body)
        # The results are a drawer now, not a `<tr>` under the row — amended
        # here rather than duplicated in a new test, because every other
        # assertion in this one is still exactly what needs to hold.
        self.page.wait_for_selector(
            f'#results-drawer:not(.hidden) .results-panel[data-for="{oldest.pk}"]',
            timeout=15000)
        self.assertEqual(self.errors, [])

    # The cropper's `fitScale` and `setZoom` were driven from here until
    # 6 Aug 2026 and are not any more. Both called the function through
    # `page.evaluate` with `render` stubbed out and asserted on the number it
    # returned: scale arithmetic, no DOM, no server, nothing a browser was
    # needed for — a unit test in a Chromium costume, at ~0.9 s a run. The fit
    # one did not even pin the defect its own docstring described, which was
    # `loadSession` failing to *call* `fitScale`, not `fitScale` returning the
    # wrong number. If the arithmetic is worth pinning it belongs in `npm test`
    # beside `matcher.js`; if the missing call is, it needs a resumed session
    # driven for real.

    def test_a_sessions_results_open_in_a_drawer_and_edit(self):
        """The review: "The individual sessions appearing inside the sessions
        board when you click on a gene is not manageable … That will have to be
        a pop out or a different page."

        The trap is that cell editing is *delegated on the grid*, so results
        drawn outside the table have dead cells — and silently, because the
        markup is identical and the click simply reaches no listener. That is
        what this drives a browser for.
        """
        from pipeline.models import Antibody, Company, ExperimentSession, Member, WbResult
        member = Member.objects.using(DB).get(site_id=self.site.pk)
        company = Company.objects.using(DB).create(name="Abcam")
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab999", site_id=self.site.pk)
        session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=member.pk)
        WbResult.objects.using(DB).create(session_id=session.pk, antibody_id=ab.pk)

        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector(f'tr[data-row="{session.pk}"]')
        # The grid does not grow a row: the results are not in the table.
        before = self.page.eval_on_selector_all("#grid tr", "els => els.length")
        self.page.click(f'tr[data-row="{session.pk}"] .results-btn')
        self.page.wait_for_selector("#results-drawer:not(.hidden)")
        self.page.wait_for_selector("#results-drawer-body .results-panel")
        self.assertEqual(
            self.page.eval_on_selector_all("#grid tr", "els => els.length"), before)

        # An editable cell inside the drawer saves — the delegation reached it.
        target_cell = self.page.query_selector(
            "#results-drawer-body .edit[data-field='comments']")
        self.assertIsNotNone(target_cell, "no editable comments cell in the drawer")
        target_cell.click()
        self.page.fill("#results-drawer-body .edit input", "drawer edit")
        self._posted(lambda: self.page.keyboard.press("Enter"))
        row = WbResult.objects.using(DB).get(session_id=session.pk)
        self.assertEqual(row.comments, "drawer edit")

        # Escape closes it, the way it closes every other pop-out.
        self.page.keyboard.press("Escape")
        # `state="attached"`, not the default `"visible"`: this waits for the
        # drawer to carry `.hidden`, and an element that is hidden is by
        # definition not visible. It only ever passed because Tailwind comes
        # from a CDN — with no stylesheet `.hidden` sets nothing, so the closed
        # drawer was still "visible" to playwright. With CI's network it hid
        # properly and the wait timed out against the very state it wanted.
        self.page.wait_for_selector("#results-drawer.hidden", state="attached")
        self.assertEqual(self.errors, [])

    def _a_wb_session(self, **kwargs):
        from pipeline.models import (Antibody, Company, ExperimentSession,
                                     Member, WbResult)
        member = Member.objects.using(DB).get(site_id=self.site.pk)
        company = Company.objects.using(DB).create(name="Abcam")
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab999", site_id=self.site.pk)
        session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=member.pk,
            status="planned")
        WbResult.objects.using(DB).create(session_id=session.pk,
                                          antibody_id=ab.pk, **kwargs)
        return session

    # the stutter this removed is two labels in one cell, in front of you
    @tag("commissioning")
    def test_a_status_is_drawn_once_and_reads_as_words(self):
        """"Every session row prints its status twice — a grey Planned badge
        with the word 'planned' repeated underneath it."

        A read-only pill beside the editable copy, and no other column on the
        board does it. The pill is the control now: it carries the code in
        `data-value` and prints the label, which is also what stopped the grid
        showing raw `in_progress` where every other surface says "In Progress".

        A browser, because both halves are rendered facts — how many elements
        the cell contains, and which of the code and the label is on screen.
        `text_content`, not `inner_text`: the cell is inside no uppercasing
        rule, but a collapsed or off-screen row returns "" from the rendered
        form and that would pass this vacuously.
        """
        session = self._a_wb_session()
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        cell = self.page.wait_for_selector(
            f'tr[data-row="{session.pk}"] .edit[data-field="status"]')
        # One editable control, and nothing else drawing the same fact beside it.
        td = self.page.eval_on_selector(
            f'tr[data-row="{session.pk}"] .edit[data-field="status"]',
            "el => el.closest('td').textContent")
        self.assertEqual(td.strip(), "Planned", f"the status is drawn twice: {td!r}")
        self.assertEqual(cell.text_content().strip(), "Planned")
        # The label is shown, the code is what a save will post.
        self.assertEqual(cell.get_attribute("data-value"), "planned")
        self.assertEqual(self.errors, [])

    # a raw `planned` where the pill was, and the record is asserted
    # unchanged
    @tag("commissioning")
    def test_cancelling_a_status_edit_puts_the_words_back(self):
        """The other half of showing a label over a code: `showValue` is what
        runs on Escape, and without the label map it put the raw `planned` back
        where the pill had been — the stutter's own defect, reintroduced by the
        fix for it, and only after a keystroke nothing else exercises."""
        session = self._a_wb_session()
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector(f'tr[data-row="{session.pk}"]')
        sel = f'tr[data-row="{session.pk}"] .edit[data-field="status"]'
        self.page.click(sel)
        self.page.wait_for_selector(f"{sel} select")
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector(f"{sel} select", state="detached")
        self.assertEqual(
            self.page.text_content(sel).strip(), "Planned",
            "cancelling put the stored code on screen instead of the label")
        session.refresh_from_db(using=DB)
        self.assertEqual(session.status, "planned")
        self.assertEqual(self.errors, [])

    def test_a_status_cell_is_a_dropdown_and_saves_what_you_pick(self):
        """`status` is one of six codes. The Plan a session header draws it as a
        `<select>` and so does this board's own filter — the grid was the one
        surface that handed you a text box, so `done` could be typed and refused
        only after the save went out.

        A browser, because the failure is silent in exactly the way source tests
        cannot see: the server still answers correctly, the cell still saves, and
        the page has simply drawn the wrong control.
        """
        session = self._a_wb_session()
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector(f'tr[data-row="{session.pk}"]')
        self.page.click(f'tr[data-row="{session.pk}"] .edit[data-field="status"]')
        select = self.page.wait_for_selector(
            f'tr[data-row="{session.pk}"] .edit[data-field="status"] select')
        # The labels, not the codes — `In Progress`, not `in_progress`.
        self.assertIn("In Progress", select.inner_text())
        # Picking is the edit; there is no second confirming click to make.
        self._posted(lambda: self.page.select_option(
            f'tr[data-row="{session.pk}"] .edit[data-field="status"] select',
            "in_progress"))
        session.refresh_from_db(using=DB)
        self.assertEqual(session.status, "in_progress")
        self.assertEqual(self.errors, [])

    def test_a_rating_cell_offers_what_the_database_already_says(self):
        """Free text with no vocabulary written down anywhere, and whatever is
        typed there is what a generated Data Note prints. Offered, never
        enforced — so it stays an `<input>` with a `<datalist>`, and a rating
        nobody has used yet is still typed."""
        from pipeline.models import WbResult
        session = self._a_wb_session(rating="Recommended")
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector(f'tr[data-row="{session.pk}"]')
        self.page.click(f'tr[data-row="{session.pk}"] .results-btn')
        self.page.wait_for_selector("#results-drawer-body .results-panel")
        self.page.click("#results-drawer-body .edit[data-field='rating']")
        box = self.page.wait_for_selector(
            "#results-drawer-body .edit[data-field='rating'] input")
        listed = self.page.eval_on_selector_all(
            "#results-drawer-body .edit[data-field='rating'] datalist option",
            "els => els.map(e => e.value)")
        self.assertIn("Recommended", listed)
        self.assertIsNotNone(box.get_attribute("list"),
                             "the box does not point at the list beside it")
        # Still typed: a vocabulary nobody may add to stops describing the bench.
        self.page.fill("#results-drawer-body .edit[data-field='rating'] input",
                       "Recommended with caveats")
        self._posted(lambda: self.page.keyboard.press("Enter"))
        self.assertEqual(WbResult.objects.using(DB).get(session_id=session.pk).rating,
                         "Recommended with caveats")
        self.assertEqual(self.errors, [])

    # two boxes for one measurement is visible on the card
    @tag("commissioning")
    def test_a_wb_card_draws_one_dilution(self):
        """`dilution` and `primary_ab_dilution` are the same measurement, drawn
        on one card with nothing to say how they differ, because they do not."""
        session = self._a_wb_session(dilution="1:1000", primary_ab_dilution="1:1000")
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector(f'tr[data-row="{session.pk}"]')
        self.page.click(f'tr[data-row="{session.pk}"] .results-btn')
        self.page.wait_for_selector("#results-drawer-body .results-panel")
        self.assertIsNotNone(self.page.query_selector(
            "#results-drawer-body .edit[data-field='dilution']"))
        self.assertIsNone(self.page.query_selector(
            "#results-drawer-body .edit[data-field='primary_ab_dilution']"),
            "two boxes for one dilution is what this removed")
        self.assertEqual(self.errors, [])

    def test_the_session_panels_cell_line_boxes_offer_your_sites_lines(self):
        """They are free text on a panel whose rule is that anything new is
        created with the session — so a typo does not fail, it mints a
        near-duplicate line and controls the session against it.

        What is pinned is the round trip, not the format: pick what is offered,
        press Check, and read back that the panel resolved it to a real row
        rather than reporting it as something it would create.
        """
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector("#new-btn")
        self.page.click("#new-btn")
        self.page.wait_for_selector("#ne-wt")
        offered = self.page.eval_on_selector_all(
            "#wt-line-options option", "els => els.map(e => e.value)")
        self.assertTrue(offered, "the site has a wild type and none was offered")
        self.page.fill("#ne-gene", "SNCA")
        self.page.fill("#ne-date", "2026-08-04")
        self.page.fill("#ne-wt", offered[0])
        self.page.fill("#new-panel table tbody input[data-c='0']", "ab999")
        self.page.click("#new-panel button:has-text('Check these')")
        self._checked("#new-panel")
        out = self.page.inner_text("#new-panel")
        self.assertIn("HAP1", out)
        self.assertNotIn("it will be created with the session", out)
        self.assertEqual(self.errors, [])

    def test_a_raw_file_attaches_to_the_antibody_row_you_picked(self):
        """The files panel, end to end — and two halves that fail *silently*.

        `FileAttachment` carried the right categories from the initial migration
        and nothing in the app ever wrote to it, so the readings were recorded
        here and the images they were read off were on somebody's laptop. This
        is that join, and most of it fails loudly: a panel that does not draw
        says "Loading files…" forever, and an Attach that does not work leaves
        the list empty in front of you.

        The first quiet half is the picker. It is `attachments.result_options`,
        arriving with the file list, so if that goes it offers only "The whole
        session" — and a scan the scientist meant to file against one antibody
        is stored against the run, with no error and a record that looks
        perfectly plausible. It used to be read off `[data-result-row]` on the
        drawn result cards, which is why the gene page could not have this panel
        at all: it lists a session without ever drawing its results.

        The second is the storage refusal. `DEBUG=False` with no object storage
        is exactly the shape of a deploy that would lose the file, so the panel
        must grey Attach *before* a file is chosen; a refusal that never reaches
        the button is one deferred to after the upload.
        """
        import tempfile

        from pipeline.models import FileAttachment, Member, WbResult

        session = self._a_wb_session()
        row = WbResult.objects.using(DB).get(session_id=session.pk)
        member = Member.objects.using(DB).get(site_id=self.site.pk)

        # ── First, with storage as the test environment finds it ────────────
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector(f'tr[data-row="{session.pk}"]')
        self.page.click(f'tr[data-row="{session.pk}"] .results-btn')
        self.page.wait_for_selector(
            f'#results-drawer-body .files-panel[data-session="{session.pk}"]')
        self.page.wait_for_selector("#results-drawer-body button[disabled]")
        self.assertIsNone(
            self.page.query_selector("#results-drawer-body .file-attach"),
            "a file could be attached where it would not survive a deploy")
        # Greyed *with the reason on the page* — not hidden, and not on `title`.
        panel_text = self.page.text_content("#results-drawer-body .files-body")
        self.assertIn("would be lost", panel_text)
        self.assertNotIn("USE_R2", panel_text)

        # ── Now say storage is durable, and do the real thing ────────────────
        # The live server shares this process, so an override reaches the view.
        with override_settings(PERSISTENT_MEDIA_ROOTS=(settings.MEDIA_ROOT,)):
            self.page.reload()
            self.page.wait_for_selector(f'tr[data-row="{session.pk}"]')
            self.page.click(f'tr[data-row="{session.pk}"] .results-btn')
            # The list arrives on its own fetch, so wait for the form rather
            # than the shell — the shell is drawn with the rest of the panel.
            self.page.wait_for_selector("#results-drawer-body .file-attach")

            # A WB session leads with the kinds a gel produces. Ordering is the
            # help; the full list is still there.
            kinds = self.page.eval_on_selector_all(
                "#results-drawer-body .file-category option",
                "els => els.map(e => e.value)")
            self.assertEqual(kinds[0], "wb_scan", kinds)
            self.assertIn("fc_fcs", kinds, "a category was hidden, not ordered")

            # The silent half: the antibody has to be offerable at all.
            choices = self.page.eval_on_selector_all(
                "#results-drawer-body .file-result option",
                "els => els.map(e => [e.value, e.textContent.trim()])")
            self.assertEqual(choices[0][0], "",
                             "the whole session must be the default")
            picked = [c for c in choices if c[0] == str(row.pk)]
            self.assertTrue(picked, f"result row {row.pk} not offered: {choices}")
            self.assertIn("ab999", picked[0][1])

            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as fh:
                fh.write(b"not really a tiff")
                path = fh.name
            self.page.set_input_files("#results-drawer-body .file-input", path)
            self.page.select_option("#results-drawer-body .file-result", str(row.pk))
            self.page.fill("#results-drawer-body .file-description", "10 s exposure")

            self.page.click("#results-drawer-body .file-attach")
            # Wait for the redrawn *list*, not for the panel's text to change:
            # "Attaching…" is written before the POST is even sent, so a
            # text-changed wait returns while the upload is still in flight.
            self.page.wait_for_selector("#results-drawer-body .file-remove")

            # It reached the database, against the row that was picked.
            att = FileAttachment.objects.using(DB).get(session_id=session.pk)
            self.assertEqual(att.wb_result_id, row.pk)
            self.assertEqual(att.category, "wb_scan")
            self.assertEqual(att.description, "10 s exposure")
            self.assertEqual(att.uploaded_by_id, member.pk)
            # The checksum a deposit needs, computed on upload.
            self.assertEqual(len(att.checksum_sha256), 64)

            # And it is on screen without anybody reloading anything.
            body = self.page.inner_text("#results-drawer-body .files-body")
            self.assertIn(os.path.basename(path), body)
            self.assertIn("10 s exposure", body)
            # 17 bytes, and it must not read as "0 KB" — a zero beside a file
            # that uploaded fine reads as one that did not.
            self.assertIn("17 bytes", body)

            # Remove is two presses, not one.
            self.page.click("#results-drawer-body .file-remove")
            self.assertTrue(
                FileAttachment.objects.using(DB).filter(pk=att.pk).exists(),
                "one press of Remove deleted the file")
            self.page.click("#results-drawer-body .file-remove")
            self.page.wait_for_selector("#results-drawer-body .file-remove",
                                        state="detached")
            self.assertFalse(
                FileAttachment.objects.using(DB).filter(pk=att.pk).exists())
        self.assertEqual(self.errors, [])

    def test_raw_data_can_be_attached_from_the_genes_own_page(self):
        """The same panel, on the page the Zenodo deposit is pressed from.

        `services/deposit.py` packages every `FileAttachment` on a gene into
        `<GENE>_underlying_data.zip`, and the button that does it is on this
        page — while attaching one meant leaving it for the sessions board and
        opening a session's drawer. The deposit preview said "0 raw file(s)" and
        the one screen it was pressed from could not change that.

        A browser, because the failure shape here is entirely wiring: a toggle
        that inserts the shell and a *second* delegated click handler on the
        same element as the panel's own. Both are valid JS and both render; only
        driving it says whether pressing Attach reaches the database. The source
        test one file over asserts the mount exists, which is true of a mount in
        the wrong place.
        """
        import tempfile

        from pipeline.models import FileAttachment, WbResult

        session = self._a_wb_session()
        row = WbResult.objects.using(DB).get(session_id=session.pk)

        with override_settings(PERSISTENT_MEDIA_ROOTS=(settings.MEDIA_ROOT,)):
            self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
            btn = f'.gene-files-btn[data-session="{session.pk}"]'
            self.page.wait_for_selector(btn)
            # Nothing attached yet, so the button asks for something rather than
            # stating a fact — and the panel is not fetched until it is pressed.
            self.assertIn("Add raw data", self.page.text_content(btn))
            self.assertIsNone(self.page.query_selector("#sessions-card .files-panel"))

            self.page.click(btn)
            # The list arrives on its own fetch, so wait for the form.
            self.page.wait_for_selector("#sessions-card .file-attach")

            # The picker is the server's answer, not rows read off this page —
            # this page draws no result cards at all, which is why it could not
            # have had this panel before.
            choices = self.page.eval_on_selector_all(
                "#sessions-card .file-result option",
                "els => els.map(e => [e.value, e.textContent.trim()])")
            self.assertEqual(choices[0][0], "",
                             "the whole session must be the default")
            picked = [c for c in choices if c[0] == str(row.pk)]
            self.assertTrue(picked, f"result row {row.pk} not offered: {choices}")
            self.assertIn("ab999", picked[0][1])

            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as fh:
                fh.write(b"not really a tiff")
                path = fh.name
            self.page.set_input_files("#sessions-card .file-input", path)
            self.page.select_option("#sessions-card .file-result", str(row.pk))
            self.page.click("#sessions-card .file-attach")
            self.page.wait_for_selector("#sessions-card .file-remove")

            att = FileAttachment.objects.using(DB).get(session_id=session.pk)
            self.assertEqual(att.wb_result_id, row.pk)

            # The count beside the button moves with it. Two numbers on one
            # screen disagreeing reads as data loss, and this one is what the
            # deposit will find.
            self.page.wait_for_function(
                "sel => /Raw data \\(1\\)/.test(document.querySelector(sel).textContent)",
                arg=btn)

            # And it closes again without taking the panel with it.
            # `state="attached"`: the stylesheet is the app's own now, so
            # `.hidden` genuinely hides and the default wait would sit here
            # waiting for a hidden thing to become visible.
            self.page.click(btn)
            self.page.wait_for_selector(
                f'tr[data-files-row="{session.pk}"].hidden', state="attached")
            self.assertIsNotNone(
                self.page.query_selector("#sessions-card .files-panel"),
                "closing the row threw the panel away")
        self.assertEqual(self.errors, [])

    def test_an_unfiltered_download_asks_first(self):
        """"I accidently downloaded all the sessions, that should not happen I
        should by default download a gene." Every board's Download takes the
        grid's filters, which is right — and with none set that is the whole
        dataset, from a button one click from the one you wanted."""
        asked = []
        self.page.on("dialog", lambda d: (asked.append(d.message), d.dismiss()))
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector("#dl-btn")
        self.page.click("#dl-btn")
        # Return as soon as the dialog fires instead of always paying 500 ms.
        for _ in range(200):
            if asked:
                break
            self.page.wait_for_timeout(25)
        self.assertTrue(asked, "an unfiltered download did not ask")
        self.assertIn("No filters are set", asked[0])
        self.assertIn("Gene box", asked[0])

    # a pure negative, and the one fixed wait left in this file
    @tag("commissioning")
    def test_a_filtered_download_does_not_ask(self):
        asked = []
        self.page.on("dialog", lambda d: (asked.append(d.message), d.dismiss()))
        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/?gene=SNCA")
        self.page.wait_for_selector("#dl-btn")
        self.page.click("#dl-btn")
        # **The one honest fixed wait in this file.** Every other sleep here was
        # waiting for something to happen and could watch for it instead; this
        # one asserts that a dialog *never* appears, and a negative has no event
        # to wait on. The budget is what makes the claim mean anything, so it is
        # deliberate rather than a leftover guess.
        self.page.wait_for_timeout(500)
        self.assertEqual(asked, [], "a filtered download asked anyway")

    def test_the_feasibility_page_still_works_end_to_end(self):
        """`board.js` loads *after* feasibility's inline script, so anything
        mounted from that block throws `ReferenceError` and kills every function
        defined below it — including the Add button. Valid syntax, passing
        `node --check`, dead page. This is the check that would catch it."""
        from pipeline.models import Site
        Site.objects.using(DB).create(name="uOttawa", short_code="UOT")
        self.page.goto(f"{self.live_server_url}/pipeline/feasibility/")
        self.page.wait_for_selector("#gene-input")
        # The functions defined at the bottom of that script block exist, which
        # is what a ReferenceError higher up would prevent.
        for fn in ("addToPipeline", "doBulkCheck", "escHtml"):
            self.assertEqual(
                self.page.evaluate(f"() => typeof {fn}"), "function", fn)
        self.assertEqual(self.errors, [])

    def test_deleting_a_record_takes_three_deliberate_steps(self):
        """"Happy for target deletion to be a feature, but one that would be
        impossible to trigger accidentally."

        Shut, then a preview that names what would go, then a tick that arms the
        button. Driven in a browser because the wiring is the risk: a handler
        bound in the wrong scope is valid syntax that only fails when the line
        runs, which is exactly how the sessions board shipped a delete control
        that highlighted red and did nothing."""
        from pipeline.models import Company
        company = Company.objects.using(DB).create(name="Abcam")
        from pipeline.models import Antibody
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab-delete-me", site_id=self.site.pk)

        # `?gene=` because a child board only offers Delete with one gene in
        # front of you — see the gate test below.
        self.page.goto(
            f"{self.live_server_url}/pipeline/antibodies/board/?gene=SNCA")
        self.page.wait_for_selector(f'tr[data-row="{ab.pk}"]')
        # The control is on the row now, not inside the identity dialog — every
        # board has one, including the two that have no identity dialog at all.
        self.page.click(f'tr[data-row="{ab.pk}"] .delete-row')
        # Nothing is offered until the server has been asked.
        self.page.wait_for_selector("text=so it can be removed")
        self.page.wait_for_selector("button:has-text('Delete permanently')")

        # The button is dead until the box is ticked — two deliberate presses
        # with a manifest between them.
        # Playwright will not click a disabled control, which is the assertion:
        # the button cannot be pressed until the box is ticked.
        self.assertTrue(self.page.is_disabled("button:has-text('Delete permanently')"))
        self.assertTrue(Antibody.objects.using(DB).filter(pk=ab.pk).exists())

        self.page.check("#delete-panel input[type=checkbox]")
        self._posted(lambda: self.page.click("button:has-text('Delete permanently')"))
        self._gone(ab.pk)
        self.assertFalse(Antibody.objects.using(DB).filter(pk=ab.pk).exists())

    def test_a_target_is_deleted_from_its_own_page_and_lands_back_on_the_board(self):
        """The one actually asked for — "we created a mock gene USP8 that could
        be removed" — now on the gene's own page rather than on the list.

        Owner's call after using it: the warning box was right, the place was
        not. A 585-row list scrolled past at speed is the wrong home for a
        control beside every gene; arriving at one gene's page is already the
        deliberate act.

        The end of the round trip matters as much as the start. The page this
        was on no longer describes anything, so it goes back to the board — and
        the board has to *say* what happened, or a destructive act ends in a
        silent list with the one row you cannot go and check missing from it.
        """
        from pipeline.models import Target, TargetNomination
        mock = Target.objects.using(DB).create(gene_name="USP8")
        TargetNomination.objects.using(DB).create(
            target_id=mock.pk, site_id=self.site.pk, funded=False)

        # Not on the board.
        self.page.goto(f"{self.live_server_url}/pipeline/targets/board/")
        self.page.wait_for_selector(f'tr[data-row="{mock.pk}"]')
        self.assertIsNone(
            self.page.query_selector(f'tr[data-row="{mock.pk}"] .delete-row'),
            "the target board still deletes from the list")

        self.page.goto(f"{self.live_server_url}/pipeline/target/{mock.pk}/")
        self.page.click("#target-delete-btn")
        self.page.wait_for_selector("text=so it can be removed")
        self.assertTrue(
            self.page.is_disabled("button:has-text('Delete permanently')"))
        self.page.check("#target-delete-panel input[type=checkbox]")
        self.page.click("button:has-text('Delete permanently')")
        self.page.wait_for_url("**/targets/board/**", timeout=8000)
        # The banner the redirect carries, rather than a guess at how long the
        # board takes to draw it.
        self.page.wait_for_selector("#board-error:has-text('Deleted')")

        self.assertFalse(Target.objects.using(DB).filter(pk=mock.pk).exists())
        self.assertIn("Deleted", self.page.inner_text("#board-error"))
        self.assertIn("USP8", self.page.inner_text("#board-error"))
        # And the parameter is gone, so a refresh does not re-announce it and a
        # copied link does not carry it to somebody else.
        self.assertNotIn("deleted=", self.page.url)
        self.assertEqual(self.errors, [])

    def test_a_child_board_hides_delete_until_one_gene_is_picked(self):
        """*"delete should probably only be an option for antibodies or cell
        lines or sessions if you are filtered to just one gene"*.

        Driven in a browser because the gate is read from the **form on every
        draw**, not from a flag the server rendered: clearing the Gene box
        without pressing Apply and letting the grid redraw is exactly how a page
        rendered narrowed could repaint the whole dataset with a Delete on all
        of it. Source-reading cannot see that; this can.
        """
        from pipeline.models import Antibody, Company
        company = Company.objects.using(DB).create(name="Abcam")
        for i in range(25):
            Antibody.objects.using(DB).create(
                target_id=self.target.pk, company_id=company.pk,
                catalogue_number=f"ab-gated-{i:02d}", site_id=self.site.pk)
        ab = Antibody.objects.using(DB).get(catalogue_number="ab-gated-00")

        for url in ("/pipeline/antibodies/board/",
                    "/pipeline/cell-lines/board/",
                    "/pipeline/sessions/board/"):
            with self.subTest(url=url):
                self.page.goto(f"{self.live_server_url}{url}")
                self.page.wait_for_selector("#grid tr")
                self.assertIsNone(self.page.query_selector("#grid .delete-row"),
                                  f"{url} offers Delete with no gene picked")
                # The absence explains itself — a control that is simply gone is
                # a feature a reader concludes does not exist.
                #
                # Wait for the gate, do not read it. `#grid tr` says the rows
                # arrived; it says nothing about `geneGate` having run, and the
                # <p> ships `hidden` and empty from the template — so reading
                # straight after the rows is a race that lost about one time in
                # three, on whichever board happened to be slowest. It failed on
                # CI against the sessions board and locally against antibodies,
                # which is what a race looks like when it is read as a
                # board-specific bug. `:not(.hidden)` is the class the writer
                # toggles, so this waits for the state being asserted.
                self.page.wait_for_selector("#delete-gate:not(.hidden)")
                gate = self.page.text_content("#delete-gate")
                self.assertIn("single gene", gate, url)

        self.page.goto(
            f"{self.live_server_url}/pipeline/antibodies/board/"
            f"?gene=SNCA&per_page=10")
        self.page.wait_for_selector(f'tr[data-row="{ab.pk}"]')
        self.assertIsNotNone(
            self.page.query_selector(f'tr[data-row="{ab.pk}"] .delete-row'))
        self.assertEqual(self.page.inner_text("#delete-gate").strip(), "")

        # **Clearing the box and letting the grid redraw takes it away again.**
        # The pager is a redraw with no page load — the same one an add or a
        # delete triggers — so a gate read once at render would still say
        # "narrowed" here, and every row of the whole dataset would carry a
        # Delete.
        self.page.fill("#filters [name=gene]", "")
        before = self.page.inner_text("#row-pager")
        self.page.click("#row-pager button:has-text('Next')")
        self._settled("#row-pager", before)
        self.assertIsNone(self.page.query_selector("#grid .delete-row"),
                          "the gate is stale — it was read once, not per draw")
        self.assertIn("single gene", self.page.inner_text("#delete-gate"))
        self.assertEqual(self.errors, [])

    def test_a_superuser_sees_the_full_cascade_before_deleting_through_it(self):
        """Owner's decision: superusers may delete things with data attached.
        The screen has to make that a different act from the ordinary one —
        every record that goes, counted, in red."""
        from django.contrib.auth.models import User

        from pipeline.models import (Antibody, Company, ExperimentSession,
                                     Member, Target, TargetNomination, WbResult)
        member = Member.objects.using(DB).get(site_id=self.site.pk)
        company = Company.objects.using(DB).create(name="Abcam")
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab-cascade", site_id=self.site.pk)
        session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=member.pk)
        WbResult.objects.using(DB).create(session_id=session.pk, antibody_id=ab.pk)
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)

        # `vera` is an ordinary member in this fixture; make her a superuser for
        # this check rather than building a second logged-in browser.
        for alias in ("academy_db", DB):
            User.objects.using(alias).filter(username="vera").update(is_superuser=True)

        # On the gene's own page — the board no longer deletes from the list.
        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.click("#target-delete-btn")
        self.page.wait_for_selector("text=has work behind it")
        panel = self.page.inner_text("#target-delete-panel")
        for expected in ("antibod", "session", "result", "nomination"):
            self.assertIn(expected, panel.lower(), panel)
        # The button says how much, so the count is on the thing you press.
        self.assertIn("other records",
                      self.page.inner_text("button:has-text('Delete it and')"))

        # And pressing it through takes the lot. This half is here because the
        # commit sends the number back — the server refuses an override whose
        # count has changed since the panel drew it, so a page that forgot to
        # send it would leave every record on file with a refusal nobody could
        # act on. A response-based test cannot see that; only the round trip can.
        self.page.check("#target-delete-panel input[type=checkbox]")
        self.page.click("button:has-text('Delete it and')")
        # Deleting a gene from its own page lands back on the board carrying
        # `?deleted=` — that redirect is the thing to wait for.
        self.page.wait_for_url("**/targets/board/**", timeout=15000)
        self.assertFalse(Target.objects.using(DB).filter(pk=self.target.pk).exists())
        self.assertFalse(Antibody.objects.using(DB).filter(pk=ab.pk).exists())
        self.assertFalse(WbResult.objects.using(DB).exists())
        self.assertEqual(self.errors, [])

    def test_a_wild_type_names_what_survives_broken_not_only_what_dies(self):
        """**The panel that lies if it counts only deletions.**

        Nothing cascades off a wild type — `parent_line` is SET_NULL — so a
        dialog built from the delete count says "will also delete nothing else"
        while the knockout below it quietly stops being a knockout of anything.
        Driven in a browser because it is the *rendering* that was wrong: the
        server answered with an empty cascade, correctly, and the page drew a
        reassuring sentence over it.

        `vera` is an ordinary member here, not a superuser, so this is also the
        browser proof of *"site members can delete their own data"* — the whole
        fixture is Leicester's.
        """
        wt = CellLine.objects.using(DB).create(name="U2OS", genotype="WT",
                                               site=self.site)
        ko = CellLine.objects.using(DB).create(
            name="U2OS", genotype="KO", target_id=self.target.pk,
            parent_line_id=wt.pk, site=self.site)

        # **`?gene=NA`, because a wild type has no gene.** Deleting on a child
        # board needs one gene in front of you, and a gene filter can never
        # return a parental line — so `NA`, the app's own word for "there isn't
        # one", is what narrows this board to them. Without it every wild type
        # would be permanently undeletable, which the gate surfaced.
        self.page.goto(
            f"{self.live_server_url}/pipeline/cell-lines/board/?gene=NA")
        self.page.wait_for_selector(f'tr[data-row="{wt.pk}"]')
        self.page.click(f'tr[data-row="{wt.pk}"] .delete-row')
        self.page.wait_for_selector("text=has work behind it")
        panel = self.page.inner_text("#delete-panel")

        self.assertIn("Nothing else would be deleted", panel, panel)
        self.assertIn("left on file with a gap", panel, panel)
        self.assertIn("no parent line recorded", panel, panel)
        # And the button must not read "Delete it and 0 other records".
        self.assertEqual(
            self.page.inner_text("#delete-panel button.bg-red-700").strip(),
            "Delete permanently")

        self.page.check("#delete-panel input[type=checkbox]")
        self._posted(lambda: self.page.click("button:has-text('Delete permanently')"))
        self._gone(wt.pk)
        self.assertFalse(CellLine.objects.using(DB).filter(pk=wt.pk).exists())
        # The knockout is still here — that is the point of naming it first.
        ko.refresh_from_db(using=DB)
        self.assertIsNone(ko.parent_line_id)
        self.assertEqual(self.errors, [])

    def test_every_add_panel_gets_from_check_to_a_saved_record(self):
        """**The half of the pop-out no browser test has ever walked.**

        Every existing check here presses *Check these* and reads the preview.
        Nothing has pressed the second button — and the second button is where
        this file's worst released defect lived: `setCommitLabel` declared in
        `newEntry` and called from `uploadPanel`, which is valid syntax, passes
        `node --check`, passes a test that greps the file for `cfg.commitLabel`,
        and only fails when the line runs. It shipped a targets board still
        saying `Create them` and an upload panel throwing a `ReferenceError` on
        every preview.

        Two more traps live on the same press and are checked here rather than
        assumed: `bulk_*_commit` defaults to `dry_run=True`, so a commit that
        does not say otherwise previews again and writes nothing; and a save
        button armed by `arm(true)` has its label reset, so a count applied
        before that is silently discarded.

        The targets board is not here: adding a gene costs a UniProt lookup, and
        outbound network is blocked in dev, so its save correctly stays greyed.
        That path has its own test below.
        """
        from pipeline.models import Antibody, Company
        Company.objects.using(DB).create(name="Abcam")

        cases = [
            ("antibodies", "/pipeline/antibodies/board/",
             {0: "SNCA", 1: "ab-arc-1", 2: "Abcam"}, False,
             lambda: Antibody.objects.using(DB)
                     .filter(catalogue_number="ab-arc-1").exists()),
            ("cell lines", "/pipeline/cell-lines/board/",
             {0: "HEK293", 1: "NA"}, False,
             lambda: CellLine.objects.using(DB).filter(name="HEK293").exists()),
        ]

        for name, url, cells, counted, saved in cases:
            with self.subTest(board=name):
                self.assertFalse(saved(), f"{name}: fixture already exists")
                self.page.goto(f"{self.live_server_url}{url}")
                self.page.wait_for_selector("#new-btn")
                self.page.click("#new-btn")
                self.page.wait_for_selector("#new-panel table tbody input")
                for col, value in cells.items():
                    self.page.fill(
                        f"#new-panel table tbody tr:first-child input[data-c='{col}']",
                        value)

                # `newEntry` namespaces its ids, because a gene page mounts two
                # Add panels and fixed ids made every button on one drive the
                # other.
                save = self.page.query_selector("#new-panel button[id$='-commit']")
                self.assertIsNotNone(save, f"{name}: no save button in the panel")
                # Disabled until a check has run, *and* the reason is on the
                # page rather than only on a `title` no touch screen shows.
                self.assertTrue(save.is_disabled(), f"{name}: armed before a check")
                self.assertIn("check", self.page.inner_text("#new-panel").lower())

                self.page.click("#new-panel button:has-text('Check these')")
                self._checked("#new-panel")
                out = self.page.inner_text("#new-panel")
                self.assertFalse(save.is_disabled(),
                                 f"{name}: still disabled after a clean check — {out[-500:]}")
                if counted:
                    # `arm(true)` resets the label, so a count applied before it
                    # is silently discarded — the ordering bug that shipped a
                    # targets board still reading `Create them`.
                    self.assertRegex(save.inner_text(), r"\d",
                                     f"{name}: the save button does not say how many")

                self._posted(save.click)
                self.assertTrue(
                    saved(),
                    f"{name}: pressed the save and nothing was written — "
                    f"{self.page.inner_text('#new-panel')[-600:]}")
        self.assertEqual(self.errors, [])

    def test_deleting_a_row_refills_the_page_rather_than_shrinking_it(self):
        """**A board draws a page, and all six delete handlers forgot that.**

        Each removed the `<tr>` and decremented the count — right for a grid
        holding everything, wrong here: take a row out of a 50-row page and 49
        remain, with the row that should have moved up still on the next page.
        Delete a few and the page quietly shrinks.

        Not visible without pagination, which is why it survived the delete
        tests above: they all run on a board of three rows.
        """
        from pipeline.models import Antibody, Company
        company = Company.objects.using(DB).create(name="Abcam")
        for i in range(30):
            Antibody.objects.using(DB).create(
                target_id=self.target.pk, company_id=company.pk,
                catalogue_number=f"ab-page-{i:02d}", site_id=self.site.pk)

        self.page.goto(
            f"{self.live_server_url}/pipeline/antibodies/board/"
            f"?gene=SNCA&per_page=10")
        self.page.wait_for_selector("#grid tr[data-row]")
        rows = lambda: self.page.eval_on_selector_all(
            "#grid tr[data-row]", "els => els.length")
        self.assertEqual(rows(), 10)

        first = self.page.get_attribute("#grid tr[data-row]", "data-row")
        self.page.click(f'tr[data-row="{first}"] .delete-row')
        self.page.wait_for_selector("text=so it can be removed")
        self.page.check("#delete-panel input[type=checkbox]")
        # A delete refetches the page, so the grid empties and repaints. `_gone`
        # only says the row left; asserting the refill straight after it caught
        # the grid mid-reload and counted zero rows. The count changing is what
        # says the reload finished.
        before = self.page.inner_text("#row-count")
        self._posted(lambda: self.page.click("button:has-text('Delete permanently')"))
        self._gone(first)
        self._settled("#row-count", before)

        self.assertFalse(Antibody.objects.using(DB).filter(pk=first).exists())
        self.assertEqual(rows(), 10,
                         "the page is short — the next row did not move up")
        self.assertIn("29", self.page.inner_text("#row-count"))
        # And the delete still says so. `load` clears messages so a stale error
        # cannot survive a reload, which means the banner has to be posted after
        # the refetch and not before it.
        self.assertIn("Deleted", self.page.inner_text("#board-error"))
        self.assertEqual(self.errors, [])

    def test_a_genes_own_page_drives_both_its_add_panels_independently(self):
        """**The page that mounts two `newEntry` pop-outs, and the one that
        proved they need numbering.**

        Both panels built elements with fixed ids, so `getElementById` returned
        the first every time: every button on *Add cell lines* drove *Add
        antibodies* — "+ Add row" did nothing, Paste would not open, Check
        produced no output — on every gene page, with nothing on screen to
        explain it. `panelSeq` fixed it, and nothing had driven the page since.

        Both panels are opened at once here, deliberately: opening one at a time
        would pass on the broken version too.
        """
        from pipeline.models import Antibody, CellLine, Company
        Company.objects.using(DB).create(name="Abcam")

        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.wait_for_selector("#ab-add-btn")
        self.page.click("#ab-add-btn")
        self.page.click("#cl-add-btn")
        self.page.wait_for_selector("#ab-add-panel table tbody input")
        self.page.wait_for_selector("#cl-add-panel table tbody input")

        # Each panel's grid is its own. With shared ids the second mount's cells
        # do not exist and this fill throws.
        self.page.fill(
            "#ab-add-panel table tbody tr:first-child input[data-c='1']", "ab-gene-page")
        self.page.fill(
            "#ab-add-panel table tbody tr:first-child input[data-c='2']", "Abcam")
        self.page.fill(
            "#cl-add-panel table tbody tr:first-child input[data-c='0']", "HEK293T")

        # Check each, and confirm the output landed in *its own* panel — the
        # symptom was one panel's buttons writing into the other's.
        self.page.click("#ab-add-panel button:has-text('Check these')")
        self._checked("#ab-add-panel")
        self.assertIn("ab-gene-page", self.page.inner_text("#ab-add-panel"))
        self.assertNotIn("ab-gene-page", self.page.inner_text("#cl-add-panel"))

        self.page.click("#cl-add-panel button:has-text('Check these')")
        self._checked("#cl-add-panel")
        self.assertIn("HEK293T", self.page.inner_text("#cl-add-panel"))

        # And both save, each writing its own kind against this gene.
        for mount, saved in (
                ("#ab-add-panel",
                 lambda: Antibody.objects.using(DB)
                         .filter(catalogue_number="ab-gene-page").exists()),
                ("#cl-add-panel",
                 lambda: CellLine.objects.using(DB)
                         .filter(name="HEK293T").exists())):
            with self.subTest(panel=mount):
                save = self.page.query_selector(f"{mount} button[id$='-commit']")
                self.assertFalse(save.is_disabled(),
                                 self.page.inner_text(mount)[-500:])
                self._posted(save.click)
                self.assertTrue(saved(), self.page.inner_text(mount)[-600:])

        # The gene column is blank on this page — every row is this gene — so
        # the antibody has to have picked it up from `extraValues`.
        ab = Antibody.objects.using(DB).get(catalogue_number="ab-gene-page")
        self.assertEqual(ab.target_id, self.target.pk)
        self.assertEqual(self.errors, [])

    def test_a_paste_whose_first_column_is_blank_keeps_its_columns(self):
        """**`trim()` does not know a tab from a space, and a tab is a column.**

        The seventh field test pasted three antibodies into a gene page's Paste
        tab, doing exactly what the panel asks — *"leave the gene column blank —
        every row here is TRPA1"* — so every line began with an empty first
        column. `tsv()` read the box with `textarea.value.trim()`, which strips
        the **leading tab of line 1** and nothing else, so row 1 slid one column
        left while rows 2 and 3 read perfectly: the catalogue number was taken
        for the gene, the company for the catalogue, and the concentration `0.8`
        landed in `site`. It was caught only because sites are validated —
        *"There is no site called '0.8'"*. With a paste one column shorter the
        row saves, silently, into all the wrong fields.

        Driven in a browser because the mangling is entirely client-side: the
        server was answering correctly about the text it was given, and the whole
        defect is which text it was given. Both rows are checked, not just the
        first — the version with the bug gets row 2 right, so a one-row paste
        passes on it.
        """
        from pipeline.models import Antibody, Company
        Company.objects.using(DB).create(name="Abcam")

        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.wait_for_selector("#ab-add-btn")
        self.page.click("#ab-add-btn")
        self.page.wait_for_selector("#ab-add-panel table tbody input")
        self.page.click("#ab-add-panel button:has-text('Paste')")

        # gene · catalogue · company · rrid · host · clonality · clone · lot ·
        # site · concentration — the gene column blank, as the panel instructs,
        # and the concentration last so a shift lands it in `site`.
        blank = "\t" * 6
        self.page.fill("#ab-add-panel textarea",
                       f"\tab-paste-1\tAbcam{blank}\t0.8\n"
                       f"\tab-paste-2\tAbcam{blank}\t0.9")

        self.page.click("#ab-add-panel button:has-text('Check these')")
        self._checked("#ab-add-panel")
        out = self.page.inner_text("#ab-add-panel")
        self.assertNotIn("no site called", out.lower(),
                         f"the first row lost its blank gene column — {out[-600:]}")

        save = self.page.query_selector("#ab-add-panel button[id$='-commit']")
        self.assertFalse(save.is_disabled(), out[-600:])
        self._posted(save.click)

        # Both rows, and every value in the field it was typed under. The row
        # that shifts is the *first* one, so asserting on row 2 alone would pass
        # on the broken version.
        for cat, conc in (("ab-paste-1", Decimal("0.8")),
                          ("ab-paste-2", Decimal("0.9"))):
            with self.subTest(row=cat):
                ab = Antibody.objects.using(DB).filter(
                    catalogue_number=cat).first()
                self.assertIsNotNone(
                    ab, f"{cat} was not created — "
                        f"{self.page.inner_text('#ab-add-panel')[-600:]}")
                self.assertEqual(ab.target_id, self.target.pk)
                self.assertEqual(ab.company.name.lower(), "abcam")
                self.assertEqual(ab.concentration, conc)
                self.assertEqual(ab.site_id, self.site.pk)
        self.assertEqual(self.errors, [])

    # a missing button, or a panel that will not open, is the whole symptom
    @tag("commissioning")
    def test_the_gene_page_has_the_upload_button_its_own_text_names(self):
        """*"also there is no upload button here"* — owner, on a gene page whose
        Add panel ends **"fill it in, then bring it back with the upload
        button"**.

        The one instruction the panel gives could not be followed on the page
        that gives it. Same family as a refused concentration telling you to fix
        it on a board with no concentration column: fixing the sentence would
        have been the wrong half.

        Driven in a browser because the wiring is the risk — `uploadPanel`
        namespaces its ids, two are mounted here, and `afterResult` did not
        exist on it at all until this landed (a config key nothing reads is
        silent, not an error).
        """
        for button, panel, word in (("#ab-upload-btn", "#ab-upload-panel", "antibodies"),
                                    ("#cl-upload-btn", "#cl-upload-panel", "cell lines")):
            with self.subTest(panel=panel):
                # A fresh page per panel. `uploadPanel`'s scrim is
                # `fixed inset-0`, so with Tailwind loaded the first one covers
                # the page and *intercepts the click* that opens the second —
                # CI failed with "subtree intercepts pointer events". Offline
                # the scrim styles nothing and the two never collide, which is
                # why this only shows where the CDN resolves.
                self.page.goto(
                    f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
                self.page.wait_for_selector("#ab-add-btn")
                self.assertIsNotNone(self.page.query_selector(button))
                self.page.click(button)
                # Wait on the **class**, not on visibility. The mount is
                # zero-size while its scrim carries `hidden`, so playwright
                # calls it hidden and a default `wait_for_selector` waits for a
                # visible thing that never comes — CI: "resolved to hidden
                # <div id=\"cl-upload-panel\">", 64 times. Offline the class
                # styles nothing, the mount has size, and it passes. Exactly the
                # rule this file already carried, broken by me one line up.
                self.page.wait_for_selector(
                    f'{panel} [id$="scrim"]:not(.hidden)', state="attached")
                self.assertIsNotNone(
                    self.page.query_selector(f"{panel} input[type=file]"),
                    f"{panel} has no file input")
                self.assertIn(word, self.page.inner_text(panel).lower())
                # Escape closes it, like every other pop-out here.
                self.page.keyboard.press("Escape")
                # The *class*, not visibility: Tailwind comes from a CDN, so
                # offline `.hidden` styles nothing and the element never stops
                # being visible, and `state="hidden"` would only ever pass
                # online. And it is `uploadPanel`'s **scrim** that carries the
                # class, not the mount — the mount never gets it, so watching
                # the panel itself waits forever for something that cannot
                # happen. The id is namespaced per pop-out (`panelSeq`), hence
                # the suffix match.
                self.page.wait_for_selector(
                    f'{panel} [id$="scrim"].hidden', state="attached")

        # And the panels are their own: two mounts, two sets of ids.
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#ab-upload-panel input[type=file], #cl-upload-panel input[type=file]",
                "els => els.length"), 2)
        self.assertEqual(self.errors, [])

    def test_an_upload_says_what_it_saved(self):
        """**Pressing Save on an upload panel said nothing at all.**

        Run 12, on a gene page: the preview was praised ("2 rows read, 2 new",
        each row spelled out) and then *"the moment I press Save these changes,
        the buttons and the whole preview just vanish. No 'Saved', no row count,
        no 'show them on this page'."* Both uploads had in fact written
        perfectly — found by reloading. A save that writes and says nothing is
        the worst shape in this repo: the reader presses it again, or goes
        hunting.

        The Add grid one panel over answers *"Saved. 3 created, 0 updated."* and
        offers *"Show them on this page →"*, from the same `renderResult` /
        `afterResult` pair — so this asserts the upload path reaches both.
        """
        from pipeline.models import Antibody, Company
        Company.objects.using(DB).create(name="Abcam")
        gene = self.target.gene_name

        cases = [
            ("#ab-upload-btn", "#ab-upload-panel",
             "catalogue,company,lot\nab-up-1,Abcam,L9\n", "up.csv",
             lambda: Antibody.objects.using(DB)
                     .filter(catalogue_number="ab-up-1").exists()),
            ("#cl-upload-btn", "#cl-upload-panel",
             "name,genotype,parent\nHEK293,KO,HAP1\n", "cl.csv",
             lambda: CellLine.objects.using(DB).filter(name="HEK293").exists()),
        ]

        for button, panel, body, filename, saved in cases:
            with self.subTest(panel=panel):
                self.assertFalse(saved(), f"{panel}: fixture already exists")
                # A fresh page per panel — the first scrim is `fixed inset-0`
                # and intercepts the click that would open the second wherever
                # Tailwind resolves.
                self.page.goto(
                    f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
                self.page.wait_for_selector("#ab-add-btn")
                self.page.click(button)
                self.page.wait_for_selector(
                    f'{panel} [id$="scrim"]:not(.hidden)', state="attached")
                self.page.set_input_files(
                    f"{panel} input[type=file]",
                    {"name": filename, "mimeType": "text/csv",
                     "buffer": body.encode()})

                out = f'{panel} [id$="-out"]'
                self.page.click(f"{panel} button:has-text('Preview changes')")
                # "Reading the file…" is the interim line and it contains the
                # word `read`, so a `text=read` wait is satisfied before the
                # request comes back.
                self.page.wait_for_function(
                    "sel => { const el = document.querySelector(sel);"
                    "         return el && !el.innerText.includes('Reading'); }",
                    arg=out, timeout=20000)
                preview = self.page.text_content(out)
                self.assertIn("1 row read", preview)

                save = self.page.query_selector(
                    f"{panel} button:has-text('Save these changes')")
                self.assertIsNotNone(save, f"{panel}: no save button")
                self._posted(save.click)
                # The write is the easy half and it was never the complaint.
                self.assertTrue(saved(), f"{panel}: nothing was written")

                # The panel has to *say so*. `text_content`, not `inner_text`:
                # offline the CDN is absent, and this asserts on what is in the
                # DOM rather than on what CSS renders.
                self.page.wait_for_function(
                    "sel => { const el = document.querySelector(sel);"
                    "         return el && !el.innerText.includes('Saving'); }",
                    arg=out, timeout=20000)
                said = (self.page.text_content(out) or "")
                self.assertIn("Saved", said,
                              f"{panel}: pressed Save and the panel said "
                              f"{said!r}")
                self.assertIn("1 created", said, f"{panel}: no count — {said!r}")
                self.assertIn("Show them on this page", said,
                              f"{panel}: no way back to the records — {said!r}")
                self.assertIn(gene, self.page.text_content(panel) or "")

                # It has to be announced, not merely present: this is the bottom
                # element of a `fixed inset-0 overflow-y-auto` modal, the same
                # tall scroller the grid banner scrolls itself into.
                self.assertEqual(
                    self.page.get_attribute(out, "role"), "status",
                    f"{panel}: the receipt is not announced")
                self.assertTrue(
                    self.page.evaluate(
                        "sel => { const r = document.querySelector(sel)"
                        "           .getBoundingClientRect();"
                        "         return r.top < innerHeight && r.bottom > 0; }",
                        out),
                    f"{panel}: the receipt is off screen")

                # **And pressing Preview again must not delete it.** That is one
                # click from the state a reader reaches when they think the save
                # did nothing — and re-previewing the same file reads "0 new,
                # 1 already known", the panel contradicting its own receipt.
                again = self.page.query_selector(
                    f"{panel} button:has-text('Preview changes')")
                self.assertTrue(again.is_disabled(),
                                f"{panel}: Preview still armed on a spent file")
                self.assertIn("choose another file",
                              (self.page.text_content(panel) or "").lower(),
                              f"{panel}: greyed with no reason on the page")
                again.click(force=True)
                self.page.wait_for_timeout(300)
                self.assertIn("Saved", self.page.text_content(out) or "",
                              f"{panel}: a second Preview wiped the receipt")
        self.assertEqual(self.errors, [])

    def test_a_session_can_be_planned_from_the_board_and_lands_on_it(self):
        """The link of the arc with the least coverage.

        The drawer test above opens a session that a fixture created; nothing
        has ever driven the *quick New session* pop-out, which is the
        recommended route and the one a first week actually uses. It is also the
        pop-out with the most moving parts — a header of eight controls in
        `extraHtml`, a `buildRequest` that sends the cell-line boxes with the
        check as well as the commit, and a commit endpoint that reports a
        refusal as `errors` rather than `error`.

        What is asserted is the whole round trip: the check names the cell line
        it resolved (not the string that was typed), the save writes a session
        with its result row, and the row appears on the grid behind the panel
        without a reload.
        """
        from pipeline.models import (Antibody, Company, ExperimentSession,
                                     WbResult)
        company = Company.objects.using(DB).create(name="Abcam")
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab-session", site_id=self.site.pk)

        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector("#new-btn")
        self.page.click("#new-btn")
        self.page.wait_for_selector("#ne-gene")
        self.page.fill("#ne-gene", "SNCA")
        self.page.select_option("#ne-procedure", "WB")
        self.page.fill("#ne-date", "2026-08-03")
        # A bare `HAP1` — the fixture's own wild type. The check has to say
        # which line that resolved to, or a session is controlled against one of
        # several with nothing on screen to say which.
        self.page.fill("#ne-wt", "HAP1")
        self.page.fill(
            "#new-panel table tbody tr:first-child input[data-c='0']", "ab-session")
        self.page.fill(
            "#new-panel table tbody tr:first-child input[data-c='1']", "Abcam")

        self.page.click("#new-panel button:has-text('Check these')")
        self._checked("#new-panel")
        out = self.page.inner_text("#new-panel")
        self.assertIn("HAP1", out, out[-700:])
        self.assertIn("Leicester", out, "the check does not say whose line it used")

        save = self.page.query_selector("#new-panel button[id$='-commit']")
        self.assertFalse(save.is_disabled(), out[-700:])
        self._posted(save.click)

        session = ExperimentSession.objects.using(DB).filter(
            target_id=self.target.pk, procedure_type="WB").first()
        self.assertIsNotNone(
            session, f"nothing was created — {self.page.inner_text('#new-panel')[-700:]}")
        self.assertEqual(WbResult.objects.using(DB).filter(
            session_id=session.pk).count(), 1)
        # And the board behind the panel shows it, without being reloaded.
        self.page.wait_for_selector(f'tr[data-row="{session.pk}"]', timeout=5000)
        self.assertEqual(self.errors, [])

    def test_a_batch_of_targets_carries_its_funder_and_project(self):
        """Owner's ask, driven end to end: the two selects and the tick are in
        `extraHtml`, read at the moment of the press rather than at the check,
        and sent to an endpoint that resolves them. Every one of those joints is
        wiring that only fails when the line runs.

        The gene is one already on file, so no UniProt lookup is needed —
        outbound network is blocked here, and `_from_db` answers a known gene
        without one.
        """
        from pipeline.models import (GrantingAgency, Project, TargetNomination)
        cihr = GrantingAgency.objects.using(DB).create(name="CIHR")
        Project.objects.using(DB).create(name="ALS 2026", granting_agency=cihr)

        self.page.goto(f"{self.live_server_url}/pipeline/targets/board/")
        self.page.wait_for_selector("#new-btn")
        self.page.click("#new-btn")
        self.page.wait_for_selector("#new-panel table tbody input")
        self.page.fill(
            "#new-panel table tbody tr:first-child input[data-c='0']", "SNCA")
        self.page.select_option("#ne-project", label="ALS 2026")
        self.page.check("#ne-funded")

        self.page.click("#new-panel button:has-text('Check these')")
        self._checked("#new-panel")
        save = self.page.query_selector("#new-panel button[id$='-commit']")
        self.assertFalse(save.is_disabled(),
                         self.page.inner_text("#new-panel")[-500:])
        self._posted(save.click)

        nom = TargetNomination.objects.using(DB).filter(
            target_id=self.target.pk, site_id=self.site.pk).first()
        self.assertIsNotNone(
            nom, self.page.inner_text("#new-panel")[-600:])
        self.assertEqual(nom.project.name, "ALS 2026")
        # The project named its funder, so choosing one chose both.
        self.assertEqual(nom.granting_agency_id, cihr.pk)
        self.assertTrue(nom.funded)
        self.assertEqual(self.errors, [])

    def test_a_check_uniprot_could_not_answer_does_not_arm_the_save(self):
        """**Found by this file, on the run that added the test above.**

        Outbound network is blocked here, so a gene the pipeline has never seen
        comes back `unchecked` — which is exactly what a live UniProt outage
        looks like, and `bulk_targets` carries a lookup deadline because that is
        an ordinary condition rather than a rare one. The preview said so and
        told the reader to press Check these again. Directly under that
        sentence, `Create them` was live: `newEntry` armed on any 200 back from
        the check, and the targets board had a `commitLabel` and no way to say
        "nothing here can be written". Pressing it reported "0 target(s) added",
        which reads as a broken feature rather than a slow network.

        The other door — the feasibility box — greyed its button correctly and
        then named the wrong reason, "already on your list". Both halves come
        from `OGABoard.targetAddSummary` now.
        """
        self.page.goto(f"{self.live_server_url}/pipeline/targets/board/")
        self.page.wait_for_selector("#new-btn")
        self.page.click("#new-btn")
        self.page.wait_for_selector("#new-panel table tbody input")
        self.page.fill(
            "#new-panel table tbody tr:first-child input[data-c='0']", "TRPA1")
        save = self.page.query_selector("#new-panel button[id$='-commit']")
        # The outage is SIMULATED. This test is about what the panel does when
        # UniProt cannot answer, and it was getting that state from the
        # environment — dev blocks outbound HTTP, so the lookup failed by
        # accident. On CI, UniProt answered, TRPA1 was found, the save armed
        # correctly and the test failed for being right.
        #
        # The live server runs in a thread of this process, so patching
        # `requests` here reaches the view. Patch `requests`, not `lookup_gene`:
        # that function catches `RequestException` itself, so patching it would
        # exercise a branch the real call cannot reach.
        def dead(*args, **kwargs):
            raise requests.RequestException("no route to host")

        with mock.patch.multiple("requests", get=dead, post=dead, request=dead):
            self.page.click("#new-panel button:has-text('Check these')")
            self._checked("#new-panel")

        panel = self.page.inner_text("#new-panel")
        self.assertTrue(
            save.is_disabled(),
            "the save is live over a list that would write nothing")

        # The reason is on the page, not only on a `title` — a touch screen
        # never shows a tooltip, and every other refusal here names itself.
        why = self.page.inner_text("#new-panel [id$='-why']")
        self.assertIn("UniProt did not answer", why)
        self.assertNotIn("already on", why.lower(),
                         "greyed for the right reason, named as the wrong one")

        # **And an unreachable API is not a misspelt gene.** `lookup_gene`
        # returned `found=False` for both, so a blocked proxy read "TRPA1 not in
        # UniProt — not added, check the spelling" about a real gene, with the
        # raw `ProxyError` printed underneath it for a scientist to read.
        self.assertIn("not checked", panel.lower())
        self.assertNotIn("check the spelling", panel.lower())
        for leak in ("ProxyError", "OSError", "rest.uniprot.org", "Traceback"):
            self.assertNotIn(leak, panel, f"raw exception detail on screen: {leak}")
        self.assertEqual(self.errors, [])

    def test_a_gene_uniprot_denies_does_not_offer_a_live_add_button(self):
        """The other half of the test above, one panel up the same page.

        The bulk box learned to grey its save and say why. The **single-gene**
        Add button four inches above it never had a check at all: the twelfth
        field test looked up a deliberately fake `ZZZZZZ`, read an honest
        verdict card — *"No human protein found for gene 'ZZZZZZ'"* — and found
        a fully enabled Add to Pipeline beside it, over a panel below stating
        *a gene UniProt cannot confirm is not added*. One screen, the rule
        printed, enforced in one path and apparently broken in the other, on the
        one door where a typo reaches the master list the whole consortium works
        from.

        Driven rather than grepped: the source-level test asserts the template
        *contains* the disabling expression, which is true of an expression in
        the wrong function — the mistake that shipped `setCommitLabel` declared
        in one panel and called from another.
        """
        # SIMULATED, and simulated as the *answer* rather than the failure: this
        # is the "no such gene" branch, which `lookup_gene` reaches by getting a
        # perfectly good 200 back with an empty result list. Raising here would
        # exercise `unavailable` instead — the opposite verdict, and the one
        # whose whole point is that it must not say "check the spelling".
        empty = mock.Mock(**{"json.return_value": {"results": []},
                             "raise_for_status.return_value": None})
        self.page.goto(f"{self.live_server_url}/pipeline/feasibility/")
        self.page.wait_for_selector("#gene-input")
        self.page.fill("#gene-input", "ZZZZZZ")
        with mock.patch("requests.get", return_value=empty):
            self.page.click("#search-btn")
            self.page.wait_for_selector("#add-target-container #add-btn")

        add = self.page.query_selector("#add-btn")
        self.assertTrue(add.is_disabled(),
                        "Add to Pipeline is live over a gene UniProt denies")

        # Disabled *with the reason*, and the reason on the page rather than on
        # a `title` no touch screen shows.
        why = self.page.text_content("#add-why")
        self.assertIn("ZZZZZZ", why)
        self.assertIn("Check the spelling", why)
        # An unreachable API is a different answer, and this is not it.
        self.assertNotIn("could not be reached", why)

        # The card that points at the button must not point at a greyed one.
        self.assertIn("cannot be added yet",
                      self.page.text_content("#pipeline-body"))
        self.assertEqual(Target.objects.using(DB).filter(gene_name="ZZZZZZ").count(), 0)
        self.assertEqual(self.errors, [])

    # a refusal firing on everything takes the feature out in front of you
    @tag("commissioning")
    def test_a_gene_uniprot_confirms_still_arms_the_add_button(self):
        """The other direction, because a refusal that fires on everything is
        the same defect wearing a different hat."""
        found = mock.Mock(**{"raise_for_status.return_value": None,
                             "json.return_value": {"results": [{
                                 "primaryAccession": "P37840",
                                 "entryType": "UniProtKB reviewed (Swiss-Prot)",
                                 "proteinDescription": {"recommendedName": {
                                     "fullName": {"value": "Alpha-synuclein"}}},
                                 "genes": [{"geneName": {"value": "SNCA"}}],
                                 "sequence": {"molWeight": 14460}}]}})
        self.page.goto(f"{self.live_server_url}/pipeline/feasibility/")
        self.page.wait_for_selector("#gene-input")
        # A gene already on file here, so this asserts about the button rather
        # than about creating a second SNCA.
        self.page.fill("#gene-input", "SNCA")
        with mock.patch("requests.get", return_value=found):
            self.page.click("#search-btn")
            self.page.wait_for_selector("#add-target-container")
        self.assertIsNone(self.page.query_selector("#add-why"),
                          "a confirmed gene was given a refusal to read")
        self.assertEqual(self.errors, [])

    def test_a_gene_says_its_knockout_goes_with_it_and_the_row_really_goes(self):
        """**The panel warned, correctly, and the warning was not the fix.**

        `CellLine.target` is SET_NULL, so deleting a gene blanked its knockouts'
        gene rather than removing them, and this panel said so in those words.
        TRPA1 was deleted after a field test anyway — because what the sentence
        describes does not *sound* like it leaves a broken row, and it does: a
        cell line with no gene is what a wild type is, so the survivor could not
        be found by its own gene, read as a parental, and collided by name with
        the knockout added when the gene went back in.

        Driven rather than reasoned about, because the number on the button is
        what the reader consents to and the commit refuses a total that moved —
        so the manifest and the write have to agree, and only a press proves it.
        """
        from pipeline.models import TargetNomination
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)
        ko = CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk, site=self.site)

        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.click("#target-delete-btn")
        self.page.wait_for_selector("text=has work behind it")
        panel = self.page.inner_text("#target-delete-panel")
        self.assertIn("1 cell line", panel, panel)
        self.assertNotIn("no gene recorded", panel, panel)
        # A count next to a noun is where a grammar slip costs most — this panel
        # read "1 cell lines" the day the knockout started being deleted.
        self.assertNotIn("1 cell lines", panel, panel)

        self.page.check("#target-delete-panel input[type=checkbox]")
        self.page.click("button:has-text('Delete it and')")
        self.page.wait_for_url("**/targets/board/**", timeout=15000)
        self.assertFalse(CellLine.objects.using(DB).filter(pk=ko.pk).exists(),
                         "the knockout outlived the gene it is a knockout of")
        self.assertEqual(self.errors, [])

    def test_a_parent_created_by_the_same_paste_is_named_on_screen(self):
        """The stale warning, driven rather than reasoned about.

        Adding a knockout and the wild type it was made from in one paste — the
        pattern both workbooks teach — previewed as *parent "U2OS" is McGill's
        line — check that is the one you mean*, and then saved onto the Leicester
        line. `apply` writes wild types first; the preview asked the database
        before the paste. Two notes about one write, disagreeing, and the one a
        person reads before committing was the wrong one.
        """
        other = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        CellLine.objects.using(DB).create(name="U2OS", genotype="WT", site=other)
        Target.objects.using(DB).create(gene_name="TRPA1", protein_name="TRPA1")

        self._open_add_panel()
        rows = [("U2OS", "TRPA1", "KO"), ("U2OS", "NA", "WT")]
        for n, (name, gene, genotype) in enumerate(rows, start=1):
            sel = f"#new-panel table tbody tr:nth-child({n})"
            self.page.fill(f"{sel} input[data-c='0']", name)
            self.page.fill(f"{sel} input[data-c='1']", gene)
            self.page.fill(f"{sel} input[data-c='2']", genotype)
        self.page.fill("#new-panel table tbody tr:nth-child(1) input[data-c='3']", "U2OS")
        self.page.click("#new-panel button:has-text('Check these')")
        self._checked("#new-panel")

        out = self.page.inner_text("#new-panel")
        self.assertIn("row 2 of this paste", out, out[-900:])
        self.assertNotIn("McGill", out,
                         "the preview named a line the save will not use")
        self.assertEqual(self.errors, [])

    def test_readings_with_no_result_id_are_named_on_the_sessions_board(self):
        """The silent half of the sessions round trip, driven end to end.

        A session with no results yet exports a row whose `result_id` is blank.
        Type a dilution, a signal and a rating on it, upload, and the preview
        used to say `0 rows would change` — no error, nothing in the errors list,
        and three readings gone. The panel has to name the columns and say where
        the result row is made.
        """
        import io as _io
        import openpyxl
        from pipeline.models import ExperimentSession, Member
        from pipeline.services import session_io as sio

        member = Member.objects.using(DB).get(site_id=self.site.pk)
        session = ExperimentSession.objects.using(DB).create(
            procedure_type="WB", target_id=self.target.pk, experimenter=member,
            site=self.site, date="2026-02-18", status="planned", comments="")

        # The sheet the board itself hands out for this session.
        wb = openpyxl.load_workbook(_io.BytesIO(sio.build_export()))
        ws = wb["WB"]
        header = [sio._norm(c.value) for c in ws[1]]
        self.assertIn(ws.cell(2, header.index("result_id") + 1).value, ("", None))
        for field, value in (("dilution", "1:1000"), ("signal", "specific band"),
                             ("rating", "pass")):
            ws.cell(2, header.index(field) + 1, value)
        buf = _io.BytesIO()
        wb.save(buf)

        self.page.goto(f"{self.live_server_url}/pipeline/sessions/board/")
        self.page.wait_for_selector("#upload-btn")
        self.page.click("#upload-btn")
        self.page.wait_for_selector("#upload-file")
        self.page.set_input_files("#upload-file", {
            "name": "sessions.xlsx", "mimeType":
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": buf.getvalue()})
        self.page.click("#preview-btn")
        self.page.wait_for_selector("#upload-result >> text=cannot be recorded")

        out = self.page.inner_text("#upload-result")
        self.assertIn("3 readings cannot be recorded", out, out)
        for field in ("dilution", "signal", "rating"):
            self.assertIn(field, out, out)
        self.assertIn(f"session #{session.pk}", out, out)
        self.assertIn("Record its results here", out, out)
        self.assertEqual(self.errors, [])

    # ── allocating a site from the bulk Add panels ───────────────────────
    #
    # Both doors write nominations; until 4 Aug both wrote them at the *adder's*
    # site with nothing on screen offering the choice. The server half is pinned
    # in `test_bulk_targets.py`; what needs a browser is the half that has gone
    # wrong before — a control the page renders and does not send, which looks
    # exactly like a working feature until you read the database.

    def _bulk_add_panel(self):
        self.page.goto(f"{self.live_server_url}/pipeline/feasibility/")
        self.page.click("button:has-text('Bulk Add Targets')")
        self.page.wait_for_selector("#bulk-genes", state="visible")

    def test_the_bulk_box_nominates_at_the_site_you_chose(self):
        from pipeline.models import TargetNomination
        ontario = Site.objects.using(DB).create(name="Ontario", short_code="ONT")

        self._bulk_add_panel()
        self.page.select_option("#bulk-site", str(ontario.pk))
        self.page.fill("#bulk-genes", "SNCA")          # already on file: no UniProt
        self.page.click("#bulk-check-btn")
        self.page.wait_for_selector("#bulk-summary >> text=Ontario")
        # The preview names the site it was asked about, not the member's.
        self.assertIn("Ontario", self.page.text_content("#bulk-summary"))

        # A bulk add that lands cleanly now leaves this page for the targets
        # board, narrowed to what it added (owner's ask, `OGABoard.targetAddGo`)
        # — so the result panel is no longer where the outcome is read, and
        # waiting for `#bulk-nominated-section` waits for something this page
        # deliberately no longer stays around to show.
        self._posted(lambda: self.page.click("#bulk-add-btn"))
        self.page.wait_for_url("**/targets/board/?gene=SNCA", timeout=20000)

        # …and nothing was lost by moving: the row it lands on says Ontario,
        # which is the whole claim the result panel used to carry.
        row = self.page.wait_for_selector(f'tr[data-row="{self.target.pk}"]')
        self.assertIn("Ontario", row.inner_text())

        self.assertTrue(TargetNomination.objects.using(DB).filter(
            target_id=self.target.pk, site_id=ontario.pk).exists(),
            "the gene did not reach the site the page said it would")
        self.assertFalse(TargetNomination.objects.using(DB).filter(
            target_id=self.target.pk, site_id=self.site.pk).exists(),
            "it also landed on the adder's own site")
        self.assertEqual(self.errors, [])

    def test_moving_the_site_after_the_check_puts_you_back_to_step_one(self):
        """A two-step check-then-create can write something nobody previewed if
        anything changes in between — and a dropdown is no different from the
        table. The save has to grey itself, with the reason on the page."""
        ontario = Site.objects.using(DB).create(name="Ontario", short_code="ONT")

        self._bulk_add_panel()
        self.page.fill("#bulk-genes", "SNCA")
        self.page.click("#bulk-check-btn")
        self.page.wait_for_selector("#bulk-add-btn:not([disabled])")

        self.page.select_option("#bulk-site", str(ontario.pk))
        self.page.wait_for_selector("#bulk-add-btn[disabled]")
        why = self.page.text_content("#bulk-add-why") or ""
        self.assertIn("site changed", why.lower(), why)
        self.assertEqual(self.errors, [])

    # ── the wild-type picker on a gene's page ────────────────────────────
    #
    # "The server answered correctly and the page did the wrong thing with it"
    # is the shape a browser is for, and a dropdown wired into a spreadsheet grid
    # is exactly that: the options can be right in the context and reach no cell.

    def _gene_page(self):
        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.wait_for_selector("#cl-add-btn")

    # an empty picker is obvious; that what it offers is a value the parser
    # takes is the load-bearing half, and that stays on the push tier below
    @tag("commissioning")
    def test_the_parent_cell_offers_the_sites_wild_types(self):
        self._gene_page()
        self.page.click("#cl-add-btn")
        self.page.wait_for_selector("#cl-add-panel table tbody input")

        # The `parent` column's cells point at a datalist, and that list holds
        # the site's wild type — HAP1, created in setUp with vial C-48.
        cols = self.page.eval_on_selector_all(
            "#cl-add-panel table thead tr:first-child th",
            "els => els.map(e => e.textContent.trim().toLowerCase())")
        parent = cols.index("parent") - 1        # the row-number column is first
        cell = self.page.query_selector(
            f'#cl-add-panel table tbody input[data-r="0"][data-c="{parent}"]')
        list_id = cell.get_attribute("list")
        self.assertTrue(list_id, "the parent cell offers nothing to pick from")
        values = self.page.eval_on_selector_all(
            f"#{list_id} option", "els => els.map(e => e.value)")
        self.assertIn("HAP1", values)
        # A bare name or a C-number — never the display label, which
        # `bulk_cell_lines.resolve_parent` would refuse.
        for value in values:
            self.assertNotIn("—", value)
        # Still a text box: a C-number or another site's line is typed as before.
        self.assertEqual((cell.get_attribute("type") or "text").lower(), "text")
        self.assertEqual(self.errors, [])

    # the two prefilled cells are on screen before anything is pressed
    @tag("commissioning")
    def test_add_a_wild_type_opens_the_grid_with_the_row_already_a_wild_type(self):
        """Two cells make a row a wild type — genotype WT and no gene — and this
        page sends its own gene with every row, so the button sets both rather
        than printing the rule under the grid."""
        self._gene_page()
        self.page.click("#cl-add-wt-btn")
        self.page.wait_for_selector("#cl-add-panel table tbody input")

        cols = self.page.eval_on_selector_all(
            "#cl-add-panel table thead tr:first-child th",
            "els => els.map(e => e.textContent.trim().toLowerCase())")
        def value_of(column):
            c = cols.index(column) - 1
            return self.page.input_value(
                f'#cl-add-panel table tbody input[data-r="0"][data-c="{c}"]')
        self.assertEqual(value_of("genotype"), "WT")
        self.assertEqual(value_of("gene"), "NA")
        self.assertEqual(value_of("name"), "", "row 1 is still yours to fill in")
        self.assertEqual(self.errors, [])

    # a spurious prefill is visible in the grid before Check
    @tag("commissioning")
    def test_the_ordinary_add_button_does_not_prefill_anything(self):
        """Every existing caller passes `entry.open` straight to a click
        listener, so the first argument is a MouseEvent. Its own properties live
        on the prototype, so a prefill read from it would quietly do nothing —
        quietly being the problem."""
        self._gene_page()
        self.page.click("#cl-add-btn")
        self.page.wait_for_selector("#cl-add-panel table tbody input")
        values = self.page.eval_on_selector_all(
            '#cl-add-panel table tbody input[data-r="0"]',
            "els => els.map(e => e.value)")
        self.assertEqual([v for v in values if v], [])
        self.assertEqual(self.errors, [])

    def test_a_picked_parent_is_one_the_parser_accepts(self):
        """The whole point of the picker, and the half a source test cannot see.

        `resolve_parent` matches a bare name or a C-number and never
        `cell_lines.label()`'s "HAP1 — Leicester". Offering the app's own display
        string would put a value in the cell that the app's own parser refuses —
        which is the failure this repo already records one column over. So drive
        it: pick, check, and read what the preview says resolved.
        """
        self._gene_page()
        self.page.click("#cl-add-btn")
        self.page.wait_for_selector("#cl-add-panel table tbody input")
        cols = self.page.eval_on_selector_all(
            "#cl-add-panel table thead tr:first-child th",
            "els => els.map(e => e.textContent.trim().toLowerCase())")

        def fill(column, value):
            c = cols.index(column) - 1
            self.page.fill(
                f'#cl-add-panel table tbody input[data-r="0"][data-c="{c}"]', value)

        # The value the dropdown offers, typed exactly as it hands it over.
        list_id = self.page.get_attribute(
            f'#cl-add-panel table tbody input[data-r="0"]'
            f'[data-c="{cols.index("parent") - 1}"]', "list")
        picked = self.page.eval_on_selector(f"#{list_id} option", "e => e.value")

        fill("name", "HAP1")
        fill("genotype", "KO")
        fill("parent", picked)
        self.page.click("#cl-add-panel button:has-text('Check')")
        self._checked("#cl-add-panel")

        out = self.page.text_content("#cl-add-panel")
        self.assertIn("your site's line", out, out)
        self.assertNotIn("no cell line called", out, out)

    # ── Where a save of genes leaves you ──────────────────────────────────
    #
    # Owner's ask: a bulk add lands on the targets board **filtered to the genes
    # it just added**, rather than on the panel you pressed with a green line
    # under it. Driven in a browser because every part of it is the shape this
    # file exists for — the server answers correctly and the page decides what
    # to do with the answer. Nothing source-level can tell a navigation that
    # happens from one that is merely written down.

    def test_a_bulk_add_lands_on_the_board_filtered_to_what_it_added(self):
        """SNCA is on file and not on Leicester's list, so the press writes a
        nomination and needs no UniProt lookup — outbound network is blocked
        here, and `_from_db` answers a known gene without one."""
        from pipeline.models import Target
        Target.objects.using(DB).create(gene_name="ELP3")

        self.page.goto(f"{self.live_server_url}/pipeline/targets/board/")
        self.page.wait_for_selector("#new-btn")
        self.page.click("#new-btn")
        self.page.wait_for_selector("#new-panel table tbody input")
        self.page.fill(
            "#new-panel table tbody tr:first-child input[data-c='0']", "SNCA")

        self.page.click("#new-panel button:has-text('Check these')")
        self._checked()
        save = self.page.query_selector("#new-panel button[id$='-commit']")
        self.assertFalse(save.is_disabled(),
                         self.page.inner_text("#new-panel")[-500:])
        save.click()

        # The board, narrowed to the gene — not the whole 585-row list with
        # yours somewhere in it, which is what the one link on offer used to do.
        # The navigation *is* the condition, so there is nothing to guess at
        # here: either the save moved the page or the wait fails saying so.
        self.page.wait_for_url("**/targets/board/?gene=SNCA", timeout=20000)
        self.page.wait_for_selector(f'tr[data-row="{self.target.pk}"]')
        self.assertEqual(
            self.page.eval_on_selector("#filters [name=gene]", "e => e.value"),
            "SNCA", "the filter box did not carry the gene, so the next rows "
                    "fetch will drop it and repaint the whole dataset")
        self.assertIsNone(self.page.query_selector('tr[data-row] >> text=ELP3'),
                          "the board is not actually filtered")
        self.assertEqual(self.errors, [])

    def test_a_result_with_something_left_to_read_stays_where_it_is(self):
        """**The rule the navigation could have broken.** `CLAUDE.md`: a page
        must not reload itself out from under its own result line, because that
        line is often the only place a *dropped* value is named. Navigating is
        that reload wearing a different hat.

        So a save that has something else to report does not move — it offers
        the same destination as a button. Here ELP3 is already on Leicester's
        list, so it is reported as *already yours* alongside a SNCA that landed.
        """
        from pipeline.models import Target, TargetNomination
        elp3 = Target.objects.using(DB).create(gene_name="ELP3")
        TargetNomination.objects.using(DB).create(
            target_id=elp3.pk, site_id=self.site.pk, funded=False)

        self.page.goto(f"{self.live_server_url}/pipeline/targets/board/")
        self.page.wait_for_selector("#new-btn")
        self.page.click("#new-btn")
        self.page.wait_for_selector("#new-panel table tbody input")
        self.page.click("#new-panel button:has-text('Paste')")
        self.page.fill("#new-panel textarea", "gene\nSNCA\nELP3")

        self.page.click("#new-panel button:has-text('Check these')")
        self._checked()

        # **Asserting that a page did *not* move needs a condition of its own.**
        # A sleep would pass here by being long enough and pass equally by being
        # short enough, which is no test at all. `targetAddGo` runs immediately
        # after the result is rendered, so the panel having stopped saying
        # "Creating…" is the point after which "still here" means something.
        self.page.query_selector("#new-panel button[id$='-commit']").click()
        self._saved()

        self.assertNotIn("?gene=", self.page.url,
                         "navigated away from a result that still had something "
                         "to say — the 'already yours' line is now unreadable")
        out = self.page.inner_text("#new-panel")
        self.assertIn("already yours", out, out)
        # …and the destination is still one click away rather than gone.
        link = self.page.query_selector('#new-panel a[href*="targets/board/?gene="]')
        self.assertIsNotNone(link, out)
        self.assertIn("SNCA", link.get_attribute("href"))
        self.assertEqual(self.errors, [])

    def test_a_gene_list_in_the_box_does_not_open_delete(self):
        """The delete gate is "the board is narrowed to **one** gene", and the
        gene box now takes a list — so a six-gene filter is six genes' worth of
        records with a Delete on every row, which is the situation the gate
        exists to prevent, one comma away."""
        from pipeline.models import Antibody, Company
        company = Company.objects.using(DB).create(name="Abcam")
        elp3 = Target.objects.using(DB).create(gene_name="ELP3")
        for target in (self.target, elp3):
            Antibody.objects.using(DB).create(
                target_id=target.pk, company_id=company.pk,
                catalogue_number=f"ab-{target.gene_name}", site_id=self.site.pk)

        self.page.goto(f"{self.live_server_url}"
                       "/pipeline/antibodies/board/?gene=SNCA")
        self.page.wait_for_selector("#grid .delete-row")

        self.page.goto(f"{self.live_server_url}"
                       "/pipeline/antibodies/board/?gene=SNCA,ELP3")
        self.page.wait_for_selector("#grid tr[data-row]")
        self.assertEqual(len(self.page.query_selector_all("#grid tr[data-row]")), 2,
                         "the board did not narrow to both genes")
        self.page.wait_for_selector("#delete-gate:not(.hidden)")
        self.assertIsNone(
            self.page.query_selector("#grid .delete-row"),
            "two genes in the box is not one gene, and every row has a Delete")
        self.assertEqual(self.errors, [])

    # a gene that is plain text rather than a link is visible in the column
    @tag("commissioning")
    def test_a_gene_name_in_a_grid_opens_that_genes_page(self):
        """It was a word you could not act on, in the column most likely to be
        what you are following. The targets board's first column always linked;
        these two printed plain text."""
        from pipeline.models import Antibody, Company
        company = Company.objects.using(DB).create(name="Abcam")
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab-link", site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk, site=self.site)

        for url in ("/pipeline/antibodies/board/", "/pipeline/cell-lines/board/"):
            with self.subTest(url=url):
                self.page.goto(f"{self.live_server_url}{url}")
                self.page.wait_for_selector("#grid tr[data-row]")
                self.page.click(f'#grid a[href="/pipeline/target/{self.target.pk}/"]')
                self.page.wait_for_url(f"**/target/{self.target.pk}/", timeout=8000)
                self.assertIn("SNCA", self.page.text_content("h1"))
        self.assertEqual(self.errors, [])

    def _picker_state(self):
        """What the Downloads & uploads field picker claims, per family."""
        return self.page.evaluate("""() => {
          const out = {};
          document.querySelectorAll('#families .fam').forEach(fam => {
            const key = fam.getAttribute('data-fam');
            out[key] = {
              count: document.querySelector(`[data-count="${key}"]`).textContent.trim(),
              note: document.querySelector(`[data-note="${key}"]`).textContent.trim(),
              keysTicked: [...fam.querySelectorAll('input[data-kind="key"]')]
                            .every(b => b.checked),
            };
          });
          return {fams: out,
                  famCount: document.getElementById('famCount').textContent.trim(),
                  selCount: document.getElementById('selCount').textContent.trim(),
                  href: document.getElementById('dlxlsx').href};
        }""")

    @staticmethod
    def _families_in(href):
        """The families the export URL actually asks the server for."""
        from urllib.parse import parse_qs, urlparse
        fields = parse_qs(urlparse(href).query).get("fields", [""])[0]
        return {part.split(":")[0] for part in fields.split("|") if part}

    def test_a_family_that_shows_ticks_is_a_family_in_the_file(self):
        """Ten families with tick counts, four sheets in the workbook.

        The locked **key** boxes were rendered `checked disabled` whatever else
        was picked, so every family showed ticks and a count — "Cell-line
        batches 3 / 15" — while `selectionParam()` skipped any family with no
        non-key field ticked and the server wrote it no sheet at all. The field
        test read ten selected families off the panel and got four, and the only
        thing on the page telling the truth was the footer's "across 4
        families", which reads as the smaller number when the ten above it
        disagree.

        A browser, because both halves are in the page: the ticks are set by JS
        and the URL they produce is built by JS. The server half — every family
        the picker offers can produce a sheet — is pinned far more cheaply in
        `services/tests/test_dataset.py`.
        """
        self.page.goto(f"{self.live_server_url}/pipeline/data/")
        self.page.wait_for_selector("#families .fam")

        state = self._picker_state()
        asked = self._families_in(state["href"]) or {
            "targets", "antibodies", "cell_lines", "reports"}   # "" = the default
        ticked = {k for k, f in state["fams"].items()
                  if not f["count"].startswith("0 / ")}
        self.assertEqual(
            ticked, asked,
            "a family showing a tick count is not a family in the file: "
            f"{state['fams']}")

        for key, fam in state["fams"].items():
            with self.subTest(family=key):
                if key in asked:
                    self.assertEqual(fam["note"], "")
                    self.assertTrue(fam["keysTicked"],
                                    "keys come with a family that is in the file")
                else:
                    self.assertEqual(fam["note"], "not in the file")
                    self.assertTrue(fam["count"].startswith("0 / "), fam["count"])
                    self.assertFalse(fam["keysTicked"],
                                     "locked keys ticked on a family that gets no sheet")
        self.assertEqual(state["famCount"], str(len(asked)))
        self.assertEqual(self.errors, [])

    # the panel's own count says how many families it asked for
    @tag("commissioning")
    def test_everything_asks_for_every_family(self):
        """The other direction: the preset that should reach all ten, does."""
        self.page.goto(f"{self.live_server_url}/pipeline/data/")
        self.page.wait_for_selector("#families .fam")
        self.page.click('#presets .preset[data-preset="everything"]')

        state = self._picker_state()
        self.assertEqual(self._families_in(state["href"]), set(state["fams"]))
        self.assertEqual(state["famCount"], str(len(state["fams"])))
        self.assertEqual([f["note"] for f in state["fams"].values()],
                         [""] * len(state["fams"]))
        self.assertEqual(self.errors, [])

    def test_picking_nothing_does_not_hand_back_the_default_file(self):
        """An empty selection is also what the export URL uses to mean "the
        historical default", so unticking everything downloaded a 54-column
        four-family workbook with nothing on screen saying so. The buttons grey,
        and say why on the page rather than on a `title` no touch screen shows.
        """
        self.page.goto(f"{self.live_server_url}/pipeline/data/")
        self.page.wait_for_selector("#families .fam")
        self.page.evaluate("""() => {
          document.querySelectorAll('#families input[type=checkbox]')
            .forEach(b => { if (!b.disabled) b.checked = false; });
          refreshCounts();
        }""")
        self.assertEqual(self._picker_state()["famCount"], "0")
        self.assertIn("pointer-events-none",
                      self.page.get_attribute("#dlxlsx", "class"))
        self.assertEqual(self.page.get_attribute("#dlxlsx", "aria-disabled"), "true")
        self.assertIn("Tick at least one field",
                      self.page.text_content("#dlWhy"))
        self.assertEqual(self.errors, [])

    def test_a_dataset_download_says_it_happened(self):
        """`data-receipt` was on the snapshot link and nothing ever called
        `OGABoard.downloadReceipts()`, so the `<p role="status">` under it stayed
        empty forever and all three downloads on the page announced nothing —
        the same silence that got a working Generate Report filed as broken."""
        self.page.goto(f"{self.live_server_url}/pipeline/data/")
        self.page.wait_for_selector("#families .fam")
        self.page.click("#dlxlsx")
        # Not `_settled`: the receipt says "Preparing the file…" first, which is
        # a change and not the answer.
        self.page.wait_for_function(
            "() => /Downloaded|could not be built/"
            ".test(document.getElementById('dlreceipt').textContent)",
            timeout=20000)
        got = self.page.text_content("#dlreceipt")
        self.assertIn("oga_pipeline_dataset.xlsx", got, got)
        self.assertEqual(self.errors, [])

    # ── The step-by-step session form ────────────────────────────────────

    def _wizard_to_step_3(self, gene="SNCA"):
        """Open the wizard, pick the gene and a procedure, land on step 3."""
        self.page.goto(f"{self.live_server_url}/pipeline/session/new/")
        self.page.wait_for_selector("#target-search")
        self.page.fill("#target-search", gene)
        self.page.click(f'#target-dropdown button:has-text("{gene}")')
        self.page.wait_for_selector("#target-selected:not(.hidden)")
        self.page.click('.proc-btn[data-proc="WB"]')
        self.page.click('button:has-text("Next: Protocol")')
        self.page.click('button:has-text("Next: Conditions")')
        self.page.wait_for_selector('.step-panel[data-step="3"]:not(.hidden)')

    def test_the_ko_box_offers_nothing_that_is_not_a_knockout(self):
        """The twelfth field test: a fresh gene with no cell lines of its own
        offered exactly one KO option — a McGill line of genotype `other` with
        no gene at all — and a dropdown with one option reads as the answer.

        Driven rather than read, because the leak was half in the query and
        half in the browser: the page fetched one list and put anything of
        genotype `other` in **both** dropdowns.
        """
        mcgill = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        stray = CellLine.objects.using(DB).create(
            name="CellLine-134", genotype="other", site=mcgill)
        CellLineVial.objects.using(DB).create(cell_line=stray, c_number=77)

        self._wizard_to_step_3()
        self.page.wait_for_selector("#cl-ko:disabled")
        ko = [o.strip() for o in self.page.eval_on_selector(
            "#cl-ko", "el => [...el.options].map(o => o.textContent)")]
        self.assertEqual(len(ko), 1, f"the KO box offered {ko}")
        self.assertIn("none on file", ko[0])

        wt = self.page.eval_on_selector(
            "#cl-wt", "el => [...el.options].map(o => o.textContent).join('|')")
        self.assertNotIn("CellLine-134", wt,
                         "a line that is not a wild type reached the WT box")
        self.assertIn("HAP1", wt, "the site's own parental went missing")
        self.assertEqual(self.errors, [])

    # an empty box with no sentence beside it is what the reader sees
    @tag("commissioning")
    def test_an_empty_ko_box_says_so_where_the_box_is(self):
        """A picker holding only its own placeholder says nothing about which
        of "none exist" and "this is broken" is true. The sentence is the gene
        page's own, and the link is a way to act on it."""
        self._wizard_to_step_3()
        self.page.wait_for_selector("#cl-ko-note:not(.hidden)")
        note = self.page.text_content("#cl-ko-note")
        self.assertIn("No knockout line on file for SNCA", note)
        self.assertIn("cell lines board", note)
        link = self.page.get_attribute("#cl-ko-link", "href")
        self.assertIn("/pipeline/cell-lines/board/", link)
        self.assertIn("gene=SNCA", link)
        self.assertEqual(self.errors, [])

    # a label missing its site is read off the dropdown
    @tag("commissioning")
    def test_a_knockout_of_this_gene_is_offered_with_its_site(self):
        """The other direction: the box works, and two labs' HAP1 are still
        tellable apart because the site is part of every label."""
        ko = CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk, site=self.site)
        CellLineVial.objects.using(DB).create(cell_line=ko, c_number=631)

        self._wizard_to_step_3()
        self.page.wait_for_selector("#cl-ko:not(:disabled)")
        labels = self.page.eval_on_selector(
            "#cl-ko", "el => [...el.options].map(o => o.textContent)")
        # Both numbers are on tubes, so both are offered: the line's own, issued
        # by `lab_numbers` on save, and the batch's. Read the line's back rather
        # than hard-coding it — which number it draws depends on the fixture.
        self.assertEqual(
            labels[1].strip(),
            f"HAP1 SNCA KO [C-{ko.c_number}, C-631] — Leicester")
        self.assertTrue(self.page.query_selector("#cl-ko-note.hidden"))
        self.assertEqual(self.errors, [])

    # a popup coming back is unmissable, and nothing is saved by a refused
    # step
    @tag("commissioning")
    def test_every_missing_field_is_named_at_once_beside_its_box(self):
        """Validation was one blocking popup at a time — target, then press
        Next again for procedure — none of them attached to the field it was
        about. A popup is also the one refusal a page cannot scroll into view.
        """
        popups = []
        self.page.on("dialog", lambda d: (popups.append(d.message), d.dismiss()))

        self.page.goto(f"{self.live_server_url}/pipeline/session/new/")
        self.page.wait_for_selector("#target-search")
        self.page.click('button:has-text("Next: Protocol")')

        self.page.wait_for_selector("#err-target:not(.hidden)")
        self.assertFalse(self.page.query_selector("#err-procedure.hidden"),
                         "the procedure was not named until the next press")
        self.assertEqual(popups, [], f"still popping up: {popups}")
        self.assertTrue(
            self.page.query_selector('.step-panel[data-step="1"]:not(.hidden)'),
            "a refused step should leave you where the boxes are")

        # An answered field stops complaining straight away. `state="attached"`
        # because `.hidden` genuinely hides — the default waits for a hidden
        # element to become visible and times out on the thing it came to see.
        self.page.click('.proc-btn[data-proc="WB"]')
        self.page.wait_for_selector("#err-procedure.hidden", state="attached")
        self.assertFalse(self.page.query_selector("#err-target.hidden"),
                         "answering one field cleared another field's message")
        self.assertEqual(self.errors, [])

    def test_planning_a_session_saves_the_lines_that_were_picked(self):
        """The whole point of the two boxes, walked end to end.

        The submit handler is new — it runs the same required-field check the
        step buttons do, so that pressing Plan Session with step 1 unanswered
        lands you on step 1 rather than on a server-rendered error page one
        reload away from the boxes that caused it. A handler that refuses a
        good form would take the feature out entirely, and `preventDefault` on
        a valid press is invisible to every source-level test.
        """
        from pipeline.models import ExperimentSession
        wt = CellLine.objects.using(DB).get(name="HAP1", genotype="WT")
        ko = CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk, site=self.site)

        self._wizard_to_step_3()
        self.page.wait_for_selector("#cl-ko:not(:disabled)")
        self.page.select_option("#cl-wt", str(wt.pk))
        self.page.select_option("#cl-ko", str(ko.pk))
        self.page.click('button:has-text("Plan Session")')
        self.page.wait_for_url("**/sessions/board/?open=*", timeout=20000)

        session = ExperimentSession.objects.using(DB).get(target_id=self.target.pk)
        self.assertEqual(session.cell_line_wt_id, wt.pk)
        self.assertEqual(session.cell_line_ko_id, ko.pk)
        self.assertEqual(self.errors, [])

    def test_a_doi_already_on_file_can_be_corrected_and_cleared(self):
        """The thirteenth field test's finding, and it is a *rendering* one.

        The Zenodo column drew a link **instead of** an editable cell as soon as
        the field held anything, so a value typed once could never be corrected
        — not here, not on the gene page, not through a spreadsheet (a blank
        cell clears nothing on the way in) and not in the admin area. A source
        test cannot see that: the server was answering perfectly, and the page
        was drawing an `<a>` where the cell used to be.

        Two presses, because the two halves fail differently. Typing rubbish is
        refused on the server and the cell has to *revert* rather than sit there
        showing a value nothing stored; clearing it has to reach the database
        and take `completed` back down with it.
        """
        from pipeline.models import Report
        Report.objects.using(DB).create(
            target_id=self.target.pk,
            zenodo_doi="https://doi.org/10.5281/zenodo.16812915",
            status=Report.ReportStatus.PUBLISHED)

        self.page.goto(f"{self.live_server_url}/pipeline/targets/board/")
        row = f'tr[data-row="{self.target.pk}"]'
        self.page.wait_for_selector(row)

        # The DOI is drawn as the DOI, not as the bare word "link", and it is
        # still a cell you can open.
        cell = self.page.query_selector(f"{row} .edit[data-field='zenodo_doi']")
        self.assertIsNotNone(cell, "the DOI cell is not editable once it has a "
                                   "value — the whole finding")
        self.assertIn("10.5281/zenodo.16812915", cell.text_content())
        # The link is beside it rather than instead of it, and it is absolute.
        href = self.page.get_attribute(f"{row} td a[href^='https://doi.org/']", "href")
        self.assertEqual(href, "https://doi.org/10.5281/zenodo.16812915")

        # A refusal reverts the cell. The banner carries it; what must not
        # happen is the screen keeping a value the database never took.
        cell.click()
        self.page.fill(f"{row} .edit[data-field='zenodo_doi'] input",
                       "definitely not a doi 12345")
        self._posted(lambda: self.page.keyboard.press("Enter"))
        self.page.wait_for_function(
            "sel => { const el = document.querySelector(sel);"
            "         return el && !el.querySelector('input'); }",
            arg=f"{row} .edit[data-field='zenodo_doi']", timeout=20000)
        report = Report.objects.using(DB).get(target_id=self.target.pk)
        self.assertEqual(report.zenodo_doi,
                         "https://doi.org/10.5281/zenodo.16812915")
        self.assertIn("10.5281/zenodo.16812915", self.page.text_content(
            f"{row} .edit[data-field='zenodo_doi']"))

        # Clearing it is how a typo is taken back out, and the gene reopens.
        self.page.click(f"{row} .edit[data-field='zenodo_doi']")
        self.page.fill(f"{row} .edit[data-field='zenodo_doi'] input", "")
        self._posted(lambda: self.page.keyboard.press("Enter"))
        self.page.wait_for_selector(f"{row} >> text=open", timeout=20000)
        # And the row goes with the value. This asserted the record survived
        # with a blank DOI and a `draft` status, which is what left the gene
        # page reading **REPORTS (1) · Draft — No DOI linked yet** about a
        # deposit nobody made — a corrected typo becoming a permanent phantom.
        # A Report stating nothing is the residue of a cell edit rather than a
        # record; `target_board.records_nothing` is the test, and a row carrying
        # anything else is kept (`tests_board_patch.py`).
        self.assertFalse(Report.objects.using(DB).filter(pk=report.pk).exists())
        # A 400 is an expected part of this test — the refusal above.
        self.assertEqual([e for e in self.errors if "400 " not in e], [])

    # ── Set recommendations ────────────────────────────────────────────────
    #
    # The twentieth field test's three findings on that page. All three are the
    # shape this file exists for — the server answered correctly every time.

    def _a_gene_with_a_published_figure(self):
        """A target carrying one antibody with one WB figure on file."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        from pipeline.models import Antibody, Company, PublicationImage
        company = Company.objects.using(DB).create(name="Abcam")
        antibody = Antibody.objects.using(DB).create(
            catalogue_number="ab58844", target=self.target,
            company=company, site=self.site)
        PublicationImage.objects.using(DB).create(
            antibody=antibody, application_type="WB",
            image=SimpleUploadedFile("wb.png", b"not-really-a-png"))
        return antibody

    def _js_errors(self):
        """Only the page's own errors — the figure fixture is a few bytes of
        text with a .png name and MEDIA is not served here, so its <img> 404s
        and that is the environment rather than the page."""
        import re as _re
        return [e for e in self.errors if not _re.match(r"^\d{3} ", e)]

    def test_the_navigation_survives_the_recommendations_page(self):
        """Finding 17, and it is only visible in a browser.

        The page declared `.hidden { display: none !important; }` for its own
        image overlay. The chrome draws its desktop half with Tailwind's
        `hidden md:flex`, and `!important` on a bare class beats `md:flex`
        whatever the specificity or the order — so the top bar rendered with
        nothing in it but the wordmark, and there was no way to sign out at all.
        Every one of those elements was in the HTML, which is exactly why
        `ChromeIsOnEveryPageTests` passed.

        Asserted as *not* `none` rather than as `flex`, so it means the same
        thing with the Tailwind CDN reachable and without it: offline the
        utilities do not exist and these elements are their default display,
        online `md:flex` wins at this width. The bug produces `none` either way,
        because the rule it came from is the page's own.
        """
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self.page.goto(f"{self.live_server_url}/pipeline/recommendations/")
        # `state="attached"`: an <option> is never "visible" to playwright, and
        # the default wait is for visibility — the trap this file has hit before.
        self.page.wait_for_selector("#filter-gene option", state="attached",
                                    timeout=20000)

        for label, selector in (
                ("the search box", 'form[action="/pipeline/find/"]'),
                ("the Browse menu", "#browse-btn"),
                ("the sign-out form", 'form[action$="/logout/"]')):
            with self.subTest(part=label):
                self.assertIsNotNone(self.page.query_selector(selector),
                                     f"{label} is not in the page at all")
                display = self.page.eval_on_selector(
                    selector, "el => getComputedStyle(el).display")
                self.assertNotEqual(
                    display, "none",
                    f"{label} is in the page and invisible — the page's own "
                    "stylesheet is overriding the chrome")

    # the picker keeping its placeholder is the first thing on the page
    @tag("commissioning")
    def test_a_gene_in_the_url_opens_that_genes_figures(self):
        """Finding 18. `?gene=` was read (the nav links on the page picked it
        up and pointed at the boards filtered to it) and not used by the picker
        it is for — so with a 160-item dropdown and no link here from a gene's
        own page, setting a gene's recommendations meant scrolling to find it
        by hand every time.

        Driven rather than asserted at the source because the failure is in the
        order of two things: the value can only be applied once `rec_genes` has
        answered and the `<option>`s exist.
        """
        self._a_gene_with_a_published_figure()
        self.page.goto(
            f"{self.live_server_url}/pipeline/recommendations/?gene=snca")
        self.page.wait_for_selector(".ab-card", timeout=20000)

        # Resolved to the target's own spelling, or the select is handed a
        # value no option carries and silently keeps its placeholder.
        self.assertEqual(self.page.input_value("#filter-gene"), "SNCA")
        self.assertIn("ab58844", self.page.text_content("#antibody-grid"))
        self.assertEqual(self._js_errors(), [])

    def test_enlarging_a_figure_is_a_button_and_does_not_recommend_it(self):
        """Finding 20. The only way to enlarge one of these was right-click,
        which is also how the browser's own menu opens — on the judgement that
        decides which antibody the field is told to buy.

        The half that must not regress is the second assertion: the thumbnail's
        own click writes a recommendation, so a control sitting on top of it
        that failed to stop the event would silently record a verdict every
        time somebody wanted a closer look.
        """
        antibody = self._a_gene_with_a_published_figure()
        self.page.goto(
            f"{self.live_server_url}/pipeline/recommendations/?gene=SNCA")
        self.page.wait_for_selector(".thumb-zoom", timeout=20000)

        overlay = "#image-preview"
        self.assertIn("rec-hidden", self.page.get_attribute(overlay, "class"))

        self.page.click(".thumb-zoom")
        self.page.wait_for_selector(f"{overlay}:not(.rec-hidden)", timeout=20000)
        self.assertIn("ab58844", self.page.text_content("#preview-caption"))

        # Escape dismisses whatever is open — the habit every pop-out teaches.
        # `state="hidden"`, because that is the whole assertion: the default
        # wait is for visibility and would sit here until it timed out.
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector(overlay, state="hidden", timeout=20000)
        self.assertIn("rec-hidden", self.page.get_attribute(overlay, "class"))

        # Nothing was recommended by looking.
        antibody.refresh_from_db(using=DB)
        self.assertFalse(antibody.wb_recommended)
        self.assertEqual(self._js_errors(), [])

    def test_the_thumbnail_itself_still_records_the_verdict(self):
        """The other side of the test above: adding a control on top of the
        thumbnail must not stop the thumbnail doing its job."""
        antibody = self._a_gene_with_a_published_figure()
        self.page.goto(
            f"{self.live_server_url}/pipeline/recommendations/?gene=SNCA")
        self.page.wait_for_selector(".exp-thumb", timeout=20000)

        self._posted(lambda: self.page.click(".exp-thumb img"))
        antibody.refresh_from_db(using=DB)
        self.assertTrue(antibody.wb_recommended)
        self.assertEqual(self._js_errors(), [])

    def test_the_sites_cell_draws_a_site_only_the_import_recorded(self):
        """The board printed an empty SITES cell offering "set site" for every
        one of the 508 targets the Access import created, while Overview listed
        the same genes under the site it had stamped on them. The thirteenth
        field test checked 348 of them and found 192 disagreeing.

        Driven in a browser because this is a new branch in `sitesCell` keyed on
        a new field in the row payload: a source-level test can only assert the
        string is somewhere in the file, which is true of a branch that never
        runs. Then typing in it must *record* a nomination — there is none to
        move — which is the half that reaches the database.
        """
        from pipeline.models import TargetNomination

        mcgill = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        imported = Target.objects.using(DB).create(
            gene_name="ABHD2", site_id=mcgill.pk)

        self.page.goto(f"{self.live_server_url}/pipeline/targets/board/")
        row = f'tr[data-row="{imported.pk}"]'
        self.page.wait_for_selector(row)

        cell = self.page.query_selector(f"{row} .edit[data-field='site']")
        self.assertIsNotNone(cell, "the SITES cell is missing entirely")
        self.assertIn("McGill", cell.text_content(),
                      "the board says nothing where Overview says McGill")
        # …and says which kind of record that is. A nomination has a funder, a
        # project and a date behind it; this is a column on a 2019 spreadsheet.
        self.assertIn("from import",
                      self.page.text_content(f"{row}").lower())

        # Typing here files the nomination the target never had.
        cell.click()
        self.page.fill(f"{row} .edit[data-field='site'] input", "Leicester")
        self._posted(lambda: self.page.keyboard.press("Enter"))
        self.page.wait_for_function(
            "sel => { const el = document.querySelector(sel);"
            "         return el && !el.querySelector('input'); }",
            arg=f"{row} .edit[data-field='site']", timeout=20000)
        nom = TargetNomination.objects.using(DB).filter(
            target_id=imported.pk).first()
        self.assertIsNotNone(nom, "nothing was recorded")
        self.assertEqual(nom.site_id, self.site.pk)
        # And the row stops claiming the import once a real nomination exists.
        self.assertNotIn("from import", self.page.text_content(row).lower())
        self.assertEqual(self.errors, [])

    # ── The gene's own page records what the board records ────────────────
    #
    # The reported journey: a paper comes out, you open the gene's page to
    # write it down, and there is nowhere to do it. The panels that fix it are
    # drawn by JavaScript from `target_board.row_for` and edited through
    # `board.js`'s delegated handlers — "the server answered correctly and the
    # page did the wrong thing with it" is precisely the shape here, so a source
    # reading cannot say whether any of it works.

    def test_recording_an_f1000_paper_on_the_gene_page(self):
        """Type the DOI and the date on the gene's page; both reach the record.

        The date is the half that matters and the half that was hardest to
        reach: `completed_report_q` asks for it, not for the DOI, so a gene
        published in F1000 and recorded by DOI alone does not read as completed
        anywhere in the app.
        """
        from pipeline.models import Report

        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.wait_for_selector("#publication-grid .edit[data-field='f1000_doi']")

        self.page.click("#publication-grid .edit[data-field='f1000_doi']")
        self.page.fill("#publication-grid .edit[data-field='f1000_doi'] input",
                       "10.12688/f1000research.185471.1")
        self._posted(lambda: self.page.keyboard.press("Enter"))

        self.page.wait_for_selector("#publication-grid .edit[data-field='f1000_date']")
        self.page.click("#publication-grid .edit[data-field='f1000_date']")
        self.page.fill("#publication-grid .edit[data-field='f1000_date'] input",
                       "2026-08-05")
        self._posted(lambda: self.page.keyboard.press("Enter"))

        report = Report.objects.using(DB).get(target_id=self.target.pk)
        self.assertEqual(report.f1000_doi,
                         "https://doi.org/10.12688/f1000research.185471.1")
        self.assertEqual(str(report.f1000_date), "2026-08-05")
        self.assertEqual(report.status, "published")

        # The panel redrew from the save: the published record is drawn from the
        # same payload as the cells, so the list cannot still be saying nothing
        # is published above the value that just made that untrue.
        #
        # Waited for, not assumed: `_posted` returns when the *response* lands,
        # and the repaint is the continuation after that await — so reading the
        # panel straight afterwards catches a cell still saying "saving…".
        # `#refresh-page` only exists once `afterPatch` has seen `completed`
        # flip, which is the last thing to happen in the redraw.
        self.page.wait_for_selector("#refresh-page")
        panel = self.page.text_content("#publication-grid")
        self.assertNotIn("Nothing published for this target yet", panel)
        self.assertIn("F1000Research", panel)
        # …including the count in the heading above it, which is server-rendered
        # and would otherwise still read "(0)" over the record just published.
        self.assertEqual(self.page.text_content("#reports-count"), "(1)")
        # …and the page says the badge above it is now behind, with a button
        # rather than an instruction to reload.
        self.assertIn("now counts as", panel)
        self.assertIsNotNone(self.page.query_selector("#refresh-page"))
        self.assertEqual(self.errors, [])

    def test_a_date_the_page_cannot_read_is_refused_where_it_can_be_seen(self):
        """A refused save that shows nothing reads as an accepted one — and this
        page is long enough that a message in the wrong place is no message."""
        from pipeline.models import Report

        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.wait_for_selector("#publication-grid .edit[data-field='f1000_date']")
        self.page.click("#publication-grid .edit[data-field='f1000_date']")
        self.page.fill("#publication-grid .edit[data-field='f1000_date'] input",
                       "last tuesday")
        self._posted(lambda: self.page.keyboard.press("Enter"))

        self.page.wait_for_selector("#board-error:not(.hidden)")
        self.assertIn("F1000 publication date",
                      self.page.text_content("#board-error-text"))
        self.assertFalse(Report.objects.using(DB).filter(target_id=self.target.pk).exists())
        # The cell went back to what it held, so nothing on screen claims a
        # value the database does not have.
        self.assertEqual(
            self.page.get_attribute("#publication-grid .edit[data-field='f1000_date']",
                                    "data-empty"), "1")

    def test_the_funding_panel_files_a_nomination_for_a_gene_with_none(self):
        """The case "there is nowhere to record it" was most true in: a gene
        nobody has nominated. The board could do this; the gene's page showed a
        sentence saying it had not happened and offered nothing."""
        from pipeline.models import TargetNomination

        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.wait_for_selector("#funding-grid .edit[data-field='site']")
        self.assertFalse(
            TargetNomination.objects.using(DB).filter(target_id=self.target.pk).exists())

        self.page.click("#funding-grid .edit[data-field='site']")
        # A site is a closed list — `services/sites.py` refuses anything else by
        # name — so the cell is a <select> and picking is the edit.
        self._posted(lambda: self.page.select_option(
            "#funding-grid .edit[data-field='site'] select", "Leicester"))

        nom = TargetNomination.objects.using(DB).get(target_id=self.target.pk)
        self.assertEqual(nom.site_id, self.site.pk)
        self.assertEqual(self.errors, [])

    def test_the_funded_toggle_and_a_protein_class_write_from_the_gene_page(self):
        from pipeline.models import TargetClassification, TargetNomination

        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk)
        self.page.goto(f"{self.live_server_url}/pipeline/target/{self.target.pk}/")
        self.page.wait_for_selector("#funding-grid .toggle[data-field='funded']")

        self._posted(lambda: self.page.click("#funding-grid .toggle[data-field='funded']"))
        self.assertIs(
            TargetNomination.objects.using(DB).get(target_id=self.target.pk).funded, True)

        self.page.click("#funding-grid .edit[data-field='add_class']")
        self.page.fill("#funding-grid .edit[data-field='add_class'] input", "Kinase")
        self._posted(lambda: self.page.keyboard.press("Enter"))
        self.assertTrue(TargetClassification.objects.using(DB).filter(
            target_id=self.target.pk, label="Kinase").exists())
        # The chip is drawn with a × because it was added by hand — a derived
        # one is not, since `remove_class` would not delete it.
        self.page.wait_for_selector(
            "#funding-grid .toggle[data-field='remove_class']")
        self.assertEqual(self.errors, [])

    def test_releasing_a_figure_from_the_review_queue_publishes_it(self):
        """The one press in this app that puts something on the public website.

        Driven in a browser rather than asserted against the endpoint — which
        `tests_review.py` already does — because everything between the two is
        wiring: the rows arrive as JSON in a `json_script` block, the tick has
        to reach the selection, the count has to reach the button, and the
        confirm panel has to send the count back. Any one of those failing
        leaves a page that renders, a button that presses and a website that
        never changes, which is the shape this repo keeps meeting.
        """
        from pipeline.models import Company, PendingPublicationImage, PublicationImage
        from pipeline.services import review as review_svc

        company = Company.objects.using(DB).create(name="Abcam")
        antibody = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab138501", site_id=self.site.pk)
        review_svc.stage(antibody=antibody, application_type="WB",
                         content=_one_pixel_png(), filename="SNCA_ab138501_WB.png",
                         recommended=True, staged_by="vera")

        # Releasing is a superuser's press (`services/review.py::
        # release_refusal`), so vera is promoted for this test only — the rest
        # of this class depends on her being an ordinary experimenter, which is
        # what the site-scoped delete gating is tested against. The session
        # re-reads the user on every request, so this takes effect on the load
        # below without signing in again.
        vera = User.objects.using("academy_db").get(username="vera")
        vera.is_superuser = True
        vera.save(using="academy_db")

        self.page.goto(f"{self.live_server_url}/pipeline/review/?gene=SNCA")
        self.page.wait_for_selector("#grid .pick")
        self.page.check("#grid .pick")
        # The count reaches the button, which is what a release is consented on.
        self.assertIn("1 figure", self.page.text_content("#release"))

        self.page.click("#release")
        self.page.wait_for_selector("#consent:not(.hidden)")
        self._posted(lambda: self.page.click("#release-confirm"))

        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)
        antibody.refresh_from_db()
        self.assertTrue(antibody.wb_recommended,
                        "the recommendation held on the pending row was not applied")
        self.assertEqual(
            PendingPublicationImage.objects.using(DB).get().status, "released")
        self.assertEqual(self.errors, [])

    def test_the_recommendation_toggles_without_ticking_the_card(self):
        """The verdict is a button inside the card's `<label>`.

        Which is why it is driven rather than asserted: a `<label>` forwards a
        click to the control it wraps unless the target is interactive content,
        so this works *because* the verdict is a `<button>`. Rebuilt as a styled
        `<span>` — the obvious tidy-up, since it is drawn as a pill — pressing
        it would also tick the figure for release: a page that appears to work,
        changes the verdict, and quietly selects something for publication.
        Nothing at the source level can see either behaviour.

        (Measured on 14 Aug 2026: with the handler's `preventDefault` and
        `stopPropagation` removed this still passes, because the button alone
        is enough. They are kept as belt and braces and this docstring does not
        claim they are what saves it.)

        Also pinned: the off state says the OPPOSITE WORD. A greyed
        "recommended" scans as recommended, which is finding 3 all over again.
        """
        from pipeline.models import Company, PendingPublicationImage
        from pipeline.services import review as review_svc

        company = Company.objects.using(DB).create(name="Abcam")
        antibody = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab138501", site_id=self.site.pk)
        review_svc.stage(antibody=antibody, application_type="WB",
                         content=_one_pixel_png(), filename="SNCA_ab138501_WB.png",
                         recommended=False, staged_by="vera")

        self.page.goto(f"{self.live_server_url}/pipeline/review/?gene=SNCA")
        self.page.wait_for_selector("#grid .rec-toggle")
        self.assertEqual(
            self.page.text_content("#grid .rec-toggle").strip(), "not recommended",
            "an off verdict must say the opposite word, not the same word greyed")

        self._posted(lambda: self.page.click("#grid .rec-toggle"))
        self.page.wait_for_selector("#grid .rec-toggle[data-on='1']")

        item = PendingPublicationImage.objects.using(DB).get()
        self.assertTrue(item.recommended, "the verdict did not reach the record")
        self.assertEqual(
            self.page.text_content("#grid .rec-toggle").strip(), "recommended")
        # The press must not have leaked through the label to the tick box.
        self.assertFalse(
            self.page.is_checked("#grid .pick"),
            "toggling the verdict also selected the figure for release")
        self.assertIn("Release to the public site",
                      self.page.text_content("#release"))

        # And back off again — "add or remove", not a one-way flag.
        self._posted(lambda: self.page.click("#grid .rec-toggle"))
        self.page.wait_for_selector("#grid .rec-toggle[data-on='0']")
        item.refresh_from_db()
        self.assertFalse(item.recommended)
        # Nothing public moved: the flag waits on the staged row until release.
        antibody.refresh_from_db()
        self.assertFalse(antibody.wb_recommended)
        self.assertEqual(self.errors, [])

    # -- Adding a freeze-down batch ---------------------------------------
    # Driven in a browser because everything that could go wrong here is on the
    # page rather than in the reply: the dialog opens from a per-row button, the
    # date arrives pre-filled by JS, and the save redraws one row from what it
    # returned. The service is covered cheaply in `tests_batches.py`; this is the
    # wiring, which is the half that shipped broken twice while `node --check`
    # stayed clean — once for a helper called from the wrong function, and once
    # here for `OGABoard.escapeCloses`, which was not exported at all and killed
    # the whole board script.
    def _row_with_add_batch(self):
        self.page.goto(f"{self.live_server_url}/pipeline/cell-lines/board/")
        self.page.wait_for_selector("tr[data-row] .add-batch", state="attached")
        return self.page.query_selector("tr[data-row] .add-batch")

    def test_the_add_batch_button_opens_a_dialog_with_today_filled_in(self):
        from datetime import date
        self._row_with_add_batch().click()
        self.page.wait_for_selector("#add-batch-panel form", state="attached")
        self.assertEqual(
            self.page.input_value("#add-batch-form input[name=freeze_date]"),
            date.today().isoformat(),
            "the freeze date did not arrive filled in with today")

    def test_adding_a_batch_names_the_number_it_issued_and_redraws_the_row(self):
        self._row_with_add_batch().click()
        self.page.wait_for_selector("#add-batch-panel form", state="attached")
        self.page.fill("#add-batch-form input[name=vial_count]", "6")
        self.page.click("#add-batch-form button[type=submit]")

        # The receipt names the number — a number the app gave a record is a
        # thing the app did, so it says so.
        self.page.wait_for_function(
            "() => { const el = document.getElementById('board-error-text');"
            "        return el && /C-\\d+/.test(el.textContent); }",
            timeout=20000)
        said = self.page.text_content("#board-error-text")
        self.assertIn("6 vials", said, said)

        # And the row redrew from what the save returned, rather than the page
        # being reloaded or left showing the line as it was.
        self.page.wait_for_function(
            "() => { const el = document.querySelector('tr[data-row] td .add-batch');"
            "        return el && el.closest('td').textContent.match(/C-\\d+.*C-\\d+/s); }",
            timeout=20000)

    def test_escape_closes_the_batch_dialog(self):
        """`escapeCloses` reaches a fourth pop-out now. It was not exported, so
        calling it threw before anything else on the page ran."""
        self._row_with_add_batch().click()
        self.page.wait_for_selector("#add-batch-panel form", state="attached")
        self.page.keyboard.press("Escape")
        self.page.wait_for_function(
            "() => document.getElementById('add-batch-panel')"
            ".classList.contains('hidden')", timeout=10000)

    def test_a_refused_batch_says_why_beside_the_box(self):
        """The one refusal a page cannot scroll into view for you, so it goes
        next to the control rather than into an alert."""
        self._row_with_add_batch().click()
        self.page.wait_for_selector("#add-batch-panel form", state="attached")
        self.page.click("#add-batch-form details summary")
        self.page.fill("#add-batch-form input[name=c_number]", "C-48")
        self.page.click("#add-batch-form button[type=submit]")
        self.page.wait_for_function(
            "() => { const el = document.getElementById('add-batch-why');"
            "        return el && el.textContent.includes('C-48'); }",
            timeout=20000)
        self.assertFalse(
            self.page.is_hidden("#add-batch-panel"),
            "the dialog closed on a refusal, losing what was typed")
