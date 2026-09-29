"""The portal draws a small batch of cards and adds more on a press.

It drew 60 per page, and on the Embed tab each card is an iframe that is a whole
page loading its own figures, so a supplier opening the portal waited on dozens
of pages at once (owner, 29 Sep 2026). Now it draws `BATCH_SIZE` and a
"Show more" button adds the next batch — a smaller one on the Embed tab,
whose cards are the heavy ones.

Two things only a browser can see, and one of them is silent. A button that does
nothing is loud. But "Show more" redrawing the whole grid instead of appending
to it would look exactly right and reload every embedded card already on
screen — the cost this exists to remove — so the test marks the first card
before the press and asks for the same node after it.

The feed is intercepted rather than built from fixtures: which antibodies a
consumer may see is `core/tests_api.py`'s question, and this one needs a few
batches' worth of rows and nothing else.
"""
import json
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.urls import reverse

from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT

from core.models import APIConsumer

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright

# A 1x1 PNG, so no card waits on the network for its figure.
_PIXEL = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
          "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
# More than two batches in all, and more than one batch on SNCA alone, so every
# assertion below has a button to press. Raise both if BATCH_SIZE passes 60.
TOTAL = 120
ON_SNCA = 60


def _feed():
    return {"antibodies": [{
        "antibody_name": f"ab{1000 + i}",
        "gene": "SNCA" if i < ON_SNCA else "LRRK2",
        "metadata": {"supplier": "Abcam"},
        "recommendations": {"WB": True},
        "embed_urls": {"all": "about:blank"},
        "experiments": [{"experiment_type": "WB", "image_url": _PIXEL}],
    } for i in range(TOTAL)]}


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class ThePortalShowsASmallBatchAndMoreOnAPressTests(StaticLiveServerTestCase):
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

    def setUp(self):
        cache.clear()
        self.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")
        self.page = self._browser.new_page()
        self.page.route("**/api/v1/antibodies/*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_feed())))
        self.page.goto(f"{self.live_server_url}{reverse('portal')}"
                       f"?key={self.consumer.api_key}")
        self.page.wait_for_selector("#dashboard:not(.hidden)")
        self.batch = self.page.evaluate("BATCH_SIZE")
        self.embed_batch = self.page.evaluate("EMBED_BATCH_SIZE")

    def tearDown(self):
        self.page.close()

    def _count(self, selector):
        return self.page.eval_on_selector_all(selector, "els => els.length")

    def test_the_embed_tab_appends_the_next_batch_without_redrawing(self):
        self.page.click('[data-tab="embed"]')
        self.page.wait_for_selector("#embed-gallery .embed-item")
        self.assertEqual(self._count("#embed-gallery .embed-item"), self.embed_batch)
        self.assertIn(f"Showing {self.embed_batch} of {TOTAL}",
                      self.page.text_content("#pager-embed"))

        self.page.evaluate(
            "document.querySelector('#embed-gallery iframe').dataset.first = 'yes'")
        self.page.click("#pager-embed button")
        self.assertEqual(self._count("#embed-gallery .embed-item"), 2 * self.embed_batch)
        # The same iframe node, so nothing already on screen was reloaded.
        self.assertEqual(
            self.page.evaluate(
                "document.querySelector('#embed-gallery iframe').dataset.first"),
            "yes")

    def test_the_data_tab_runs_out_and_a_filter_starts_again(self):
        self.page.click('[data-tab="data"]')
        self.page.wait_for_selector("#antibody-grid .ab-card")
        self.assertEqual(self._count("#antibody-grid .ab-card"), self.batch)

        while self._count("#pager-data button"):
            self.page.click("#pager-data button")
        self.assertEqual(self._count("#antibody-grid .ab-card"), TOTAL)
        self.assertIn(f"All {TOTAL} antibodies shown",
                      self.page.text_content("#pager-data"))

        # A filter is a new set, so it starts from one batch again.
        self.page.select_option("#filter-gene", "SNCA")
        self.assertEqual(self._count("#antibody-grid .ab-card"), self.batch)
        self.assertIn(f"Showing {self.batch} of {ON_SNCA}",
                      self.page.text_content("#pager-data"))
