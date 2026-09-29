"""A manufacturer signs in to the comparison, already showing a gene.

Owner, 29 Sep 2026: comparing your antibodies with other suppliers' on one gene
is why most manufacturers open the portal, so the portal opens there, and its
gene picker is big, first on the tab, and steps through your genes with arrows.
A key that is not a manufacturer's cannot have the comparison at all (the
server refuses it), so it opens on Download images instead.

Browser tests, because every part of it is page wiring: which tab is active
after sign-in, which gene the picker starts on, and whether the arrow loads the
next gene rather than only changing the box. `core/tests_api.py` answers which
antibodies the comparison may show.
"""
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.urls import reverse

from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT

from core.models import APIConsumer
from pipeline.models import Antibody, Company, PublicationImage, Target

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class ThePortalOpensOnTheComparisonTests(StaticLiveServerTestCase):
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
        abcam = Company.objects.create(name="Abcam", display_name="Abcam")
        other = Company.objects.create(name="Proteintech", display_name="Proteintech")
        for gene, ours, theirs in (("SNCA", "ab-snca", "pt-snca"),
                                   ("TRPA1", "ab-trpa1", "pt-trpa1")):
            target = Target.objects.create(gene_name=gene, protein_name=gene)
            for company, cat in ((abcam, ours), (other, theirs)):
                ab = Antibody.objects.create(target=target, company=company,
                                             catalogue_number=cat)
                PublicationImage.objects.create(
                    antibody=ab, application_type="WB",
                    image=f"pubs/{gene}_{cat}_WB.png")
        self.page = self._browser.new_page()
        # No figure needs to arrive for this; keep the page off the network.
        self.page.route("**/*.png", lambda r: r.fulfill(status=204))

    def tearDown(self):
        self.page.close()

    def _connect(self, consumer_type):
        consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type=consumer_type, supplier_filter="Abcam")
        self.page.goto(f"{self.live_server_url}{reverse('portal')}"
                       f"?key={consumer.api_key}")
        self.page.wait_for_selector("#dashboard:not(.hidden)")

    def _active_tab(self):
        return self.page.get_attribute(".tab.active", "data-tab")

    def test_a_manufacturer_lands_on_their_first_gene_and_the_arrow_moves_on(self):
        self._connect("manufacturer")
        self.assertEqual(self._active_tab(), "competitor")
        self.assertEqual(self.page.input_value("#compare-gene"), "SNCA")
        self.page.wait_for_selector("#competitor-content .ab-card")
        body = self.page.text_content("#competitor-content")
        self.assertIn("ab-snca", body)
        self.assertIn("pt-snca", body)
        # The shared filter bar filters a different dataset, so it is not here.
        self.assertFalse(self.page.is_visible("#filter-bar"))

        self.page.click('[aria-label="Next gene"]')
        self.page.wait_for_function(
            "() => document.querySelector('#competitor-content')"
            ".textContent.includes('pt-trpa1')")
        self.assertEqual(self.page.input_value("#compare-gene"), "TRPA1")
        self.assertNotIn("pt-snca", self.page.text_content("#competitor-content"))

    def test_any_other_key_lands_on_download_images(self):
        self._connect("rrid")
        self.assertEqual(self._active_tab(), "data")
        self.assertFalse(self.page.is_visible('[data-tab="competitor"]'))
        self.page.wait_for_selector("#antibody-grid .ab-card")
