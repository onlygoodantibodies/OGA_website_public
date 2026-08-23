"""The search box, driven in a real browser.

The behaviour of both search boxes moved into one shared file
(``core/static/core/oga_search.js``), and the failure that change can produce is
the one no response test can see: the page renders, the box is there, the markup
is right, and typing into it does nothing. ``manage.py check`` passes on it and
``node --check`` passes on it, because a ``ReferenceError`` only exists when the
line runs. So the press is walked here.

Needs a ``StaticLiveServerTestCase`` — a plain ``LiveServerTestCase`` does not
serve ``oga_search.js``, so the dropdown never loads and every assertion below
times out looking exactly like the bug it came for.

Assertions are on the DOM, not on rendered text: the site's stylesheet is served,
but nothing here should depend on what CSS makes visible.

``@tag("commissioning")`` follows the rule set out in
``pipeline/tests_browser_board.py``: the push tier keeps what fails **silently**,
the nightly takes what a reader would see the moment it broke. Three stay on the
push tier. A typeset catalogue number that stops resolving answers "no match" for
a reagent we hold two figures for — indistinguishable from a true absence, which
is the whole failure this feature exists to end. The header-bar mount is the
drift case: the homepage box goes on working, so nothing looks broken. And the
end-to-end press covers the ``?ab=`` landing quietly highlighting nothing, on a
page that otherwise renders perfectly. The rest — gene search, aliases, the empty
state, the chip — are loud and write nothing, so they run nightly.
"""
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.test import tag
from django.urls import reverse

# The browser and the skip condition are `pipeline/tests_browser_board.py`'s.
# A second chrome-finder here is exactly the copy that drifts: that one already
# reported `OK (skipped=26)` once because it knew `chrome-linux` and playwright
# had moved to `chrome-linux64`, and a skip is the failure this tier can least
# afford. It also sets DJANGO_ALLOW_ASYNC_UNSAFE on import, which the sync API
# needs before the ORM will answer.
from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT

from pipeline.models import Antibody, Company, PublicationImage, Target

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class SearchBoxInARealBrowserTests(StaticLiveServerTestCase):
    databases = {"default", "pipeline_db", "academy_db"}

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
        target = Target.objects.create(
            protein_name="Parkin", gene_name="PRKN", aliases="PARK2")
        self.antibody = Antibody.objects.create(
            target=target, company=Company.objects.create(name="Proteintech"),
            catalogue_number="14060-1-AP", rrid="AB_2878005")
        PublicationImage.objects.create(
            antibody=self.antibody, application_type="WB",
            image="pubs/prkn_wb.png")
        self.page = self._browser.new_page()

    def tearDown(self):
        self.page.close()

    def _type(self, url, selector, text):
        self.page.goto(self.live_server_url + url)
        self.page.fill(selector, text)
        return self.page

    # --- the homepage box --------------------------------------------------

    def test_the_homepage_box_offers_the_antibody_and_goes_to_its_row(self):
        page = self._type(reverse("home"), ".search-bar", "14060-1-AP")
        page.wait_for_selector("#oga-dropdown .suggestion", state="attached")
        self.assertIn("14060-1-AP",
                      page.text_content("#oga-dropdown .suggestion"))
        # Supplier and gene are what tell two products with one catalogue
        # number apart, so they have to actually render.
        self.assertIn("Proteintech",
                      page.text_content("#oga-dropdown .suggestion"))

        page.click("#oga-dropdown .suggestion")
        page.wait_for_url("**/antibodies/PRKN/**")
        self.assertIn("ab=", page.url)
        page.wait_for_selector(f"#ab-{self.antibody.pk}.oga-ab-focused",
                               state="attached")

    @tag("commissioning")
    def test_the_homepage_box_still_finds_a_gene(self):
        page = self._type(reverse("home"), ".search-bar", "PRKN")
        page.wait_for_selector("#oga-dropdown .suggestion", state="attached")
        self.assertIn("PRKN", page.text_content("#oga-dropdown .suggestion"))

    @tag("commissioning")
    def test_a_gene_alias_still_works(self):
        page = self._type(reverse("home"), ".search-bar", "PARK2")
        page.wait_for_selector("#oga-dropdown .suggestion", state="attached")
        self.assertIn("PRKN", page.text_content("#oga-dropdown .suggestion"))

    @tag("commissioning")
    def test_a_query_matching_nothing_says_so_once(self):
        """And only once the server has answered — "no match" printed while the
        antibody half is still in flight is a claim the page cannot make yet."""
        page = self._type(reverse("home"), ".search-bar", "zzzznotathing")
        page.wait_for_selector("#oga-dropdown .no-match", state="attached")
        self.assertEqual(page.locator("#oga-dropdown .no-match").count(), 1)
        self.assertEqual(page.locator("#oga-dropdown .suggestion").count(), 0)

    # --- the box in the header bar, on every other page --------------------

    def test_the_header_bar_box_finds_an_antibody_too(self):
        """The two boxes share one implementation; this is what says the second
        mount is actually wired, rather than merely present in the markup."""
        page = self._type(reverse("about"), ".oga-genesearch-input", "14060-1-AP")
        page.wait_for_selector(".oga-genesearch-item", state="attached")
        self.assertIn("14060-1-AP", page.text_content(".oga-genesearch-item"))

        page.click(".oga-genesearch-item")
        page.wait_for_url("**/antibodies/PRKN/**")
        page.wait_for_selector(f"#ab-{self.antibody.pk}.oga-ab-focused",
                               state="attached")

    def test_a_typeset_catalogue_number_still_finds_the_row(self):
        """Pasted out of a PDF, en-dash and all — the case the whole normaliser
        exists for, walked end to end rather than asserted at the service."""
        page = self._type(reverse("about"), ".oga-genesearch-input", "14,060–1-AP")
        page.wait_for_selector(".oga-genesearch-item", state="attached")
        self.assertIn("14060-1-AP", page.text_content(".oga-genesearch-item"))

    # --- the landing page --------------------------------------------------

    @tag("commissioning")
    def test_the_chip_clears_back_to_the_whole_list(self):
        gene_page = reverse("antibody_table", kwargs={"gene_name": "PRKN"})
        self.page.goto(f"{self.live_server_url}{gene_page}?ab=14060-1-AP")
        self.page.wait_for_selector(".oga-ab-focus-clear", state="attached")
        self.page.click(".oga-ab-focus-clear")
        self.page.wait_for_url(f"**{gene_page}")
        self.assertEqual(
            self.page.locator(".row-box.oga-ab-focused").count(), 0)
