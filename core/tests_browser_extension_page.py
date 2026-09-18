"""Click-to-enlarge on `/extension/`, in a real browser.

Each figure there is a 1280x800 screenshot drawn at roughly 400px, so the card
inside it is about a third of life size — legible as a shape and not as words
(owner, 29 Aug 2026). Enlarging is the fix, and none of it can be checked
without running the page: the control is a delegated listener, the dialog is a
`hidden` attribute toggled in script, and focus handling is invisible to every
response test.

**A control that is merely possible is a feature a reader concludes does not
exist**, which is why the magnifier is asserted as well as the behaviour. And
the trigger is a real `<button>` because an `<img>` is not focusable: a page
where the only way to enlarge is a mouse click is one a keyboard user cannot
use at all.
"""
from __future__ import annotations

import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT
from pipeline.tests_timeouts import DB


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
@override_settings(EXTENSION_PAGE_PUBLIC=True)
class TheScreenshotsEnlargeTests(StaticLiveServerTestCase):
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
        self.page = self.browser.new_page(
            viewport={"width": 1280, "height": 900})
        self.page.goto(f"{self.live_server_url}/extension/")
        self.page.wait_for_selector(".bx-shot img", timeout=20000)

    def tearDown(self):
        self.page.close()

    def test_every_figure_offers_the_control_in_words(self):
        """Not a bare icon: a magnifier alone is a shape somebody has to guess
        at, and the word is what makes it a control rather than decoration."""
        shots = self.page.query_selector_all(".bx-shot")
        self.assertTrue(shots, "no figures on the page")
        for shot in shots:
            btn = shot.query_selector(".bx-zoom")
            self.assertIsNotNone(btn, "a figure with no way to enlarge it")
            self.assertIn("Enlarge", btn.text_content())
            self.assertIsNotNone(btn.query_selector("svg"), "no magnifier")

    def test_pressing_it_opens_the_full_size_image(self):
        self.page.click(".bx-shot .bx-zoom")
        self.page.wait_for_selector("#bx-lightbox:not([hidden])", timeout=10000)
        # The enlarged image is the same file, drawn bigger than the thumbnail.
        big = self.page.query_selector("#bx-lightbox-img").bounding_box()
        small = self.page.query_selector(".bx-shot img").bounding_box()
        self.assertGreater(big["width"], small["width"] * 1.5)

    def test_the_image_itself_is_clickable_too(self):
        """The button is what makes it discoverable; the image is what a reader
        actually clicks."""
        self.page.click(".bx-shot img")
        self.page.wait_for_selector("#bx-lightbox:not([hidden])", timeout=10000)

    def test_escape_closes_it_and_focus_comes_back(self):
        """A dialog that closes without returning focus strands a keyboard user
        at the top of the document."""
        self.page.click(".bx-shot .bx-zoom")
        self.page.wait_for_selector("#bx-lightbox:not([hidden])", timeout=10000)
        self.page.keyboard.press("Escape")
        # `state="attached"`: a hidden element is not "visible", so the
        # default wait never matches the thing it is waiting for.
        self.page.wait_for_selector("#bx-lightbox[hidden]",
                                    state="attached", timeout=10000)
        focused = self.page.evaluate(
            "() => document.activeElement.className")
        self.assertIn("bx-zoom", focused,
                      "focus was not returned to the control that opened it")

    def test_the_backdrop_closes_it(self):
        self.page.click(".bx-shot .bx-zoom")
        self.page.wait_for_selector("#bx-lightbox:not([hidden])", timeout=10000)
        self.page.mouse.click(12, 12)
        # `state="attached"`: a hidden element is not "visible", so the
        # default wait never matches the thing it is waiting for.
        self.page.wait_for_selector("#bx-lightbox[hidden]",
                                    state="attached", timeout=10000)
