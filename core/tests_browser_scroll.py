"""The page scrolls the viewport, and "Back to Top" reaches the top.

Every "Back to Top" button on the site calls
``window.scrollTo({top: 0, behavior: 'smooth'})``, which scrolls the *viewport*.
The stylesheet had put ``overflow-x: hidden`` on ``body`` as well as ``html``
(three times: once globally and twice inside the mobile media query, for
``body.no-wobble`` and ``body.home-page``). That forces body's computed
``overflow-y`` to ``auto`` — CSS turns ``visible`` into ``auto`` when the other
axis is not visible — and ``html, body { height: 100% }`` further down then pins
body to the viewport height. Body became the scroll container: the page scrolled
inside it while ``window.scrollY`` stayed 0 forever, so every one of those
buttons did nothing, on every page that has one, at every width.

Nothing in the suite could see it. The markup is right, the button is there, the
handler runs without error and the CSS is valid — the button just moves an
element that is not the one scrolling. That is the shape this file exists for.

Two things are asserted, and the second is the one that would go quiet:
the button returns the reader to the top, and the *reader* still cannot drag the
page sideways. The horizontal clip is the reason the body rules were written, so
a fix that restored scrolling by giving up "no wobble" would be no fix at all.
``overflow-x: hidden`` on ``html`` propagates to the viewport and keeps it —
programmatic ``scrollTo`` can still move a hidden axis, so the check has to be a
real gesture, not ``window.scrollTo``.
"""
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

# The browser and the skip condition are `pipeline/tests_browser_board.py`'s —
# see the note in `core/tests_browser_search.py` about not writing a second
# chrome-finder here.
from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
@tag("commissioning")
class ViewportScrollingTests(StaticLiveServerTestCase):
    """Tagged commissioning: it fails loudly and writes nothing. It is kept
    because the failure is invisible to every cheaper tier — and because the
    rule it guards is a CSS one that reads as harmless tidying."""

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def _home(self, width):
        page = self._browser.new_page(viewport={"width": width, "height": 800})
        # Nothing off-origin is part of what is being asserted, and waiting on
        # it is the difference between a 1-second test and a minute of it: a
        # webfont that 404s in CI and hangs behind a blocked proxy locally
        # decides how long this takes and nothing else.
        page.route("**/*", lambda route: route.continue_()
                   if route.request.url.startswith(self.live_server_url)
                   else route.abort())
        page.goto(self.live_server_url + reverse("home"),
                  wait_until="domcontentloaded")
        page.wait_for_timeout(200)
        return page

    def test_back_to_top_returns_the_reader_to_the_top(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                page = self._home(width)
                try:
                    page.locator(".back-to-top").first.scroll_into_view_if_needed()
                    page.wait_for_timeout(200)
                    self.assertGreater(
                        page.evaluate("() => window.scrollY"), 0,
                        "the viewport must be what scrolls — if window.scrollY "
                        "is 0 at the footer, something has made body the "
                        "scroll container again")
                    page.locator(".back-to-top").first.click()
                    page.wait_for_timeout(900)
                    self.assertEqual(page.evaluate("() => window.scrollY"), 0,
                                     "Back to Top did not reach the top")
                finally:
                    page.close()

    def test_the_reader_still_cannot_scroll_the_page_sideways(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                page = self._home(width)
                try:
                    page.mouse.move(width // 2, 400)
                    page.mouse.wheel(600, 0)
                    page.wait_for_timeout(300)
                    self.assertEqual(page.evaluate("() => window.scrollX"), 0,
                                     "the page wobbles sideways again")
                finally:
                    page.close()
