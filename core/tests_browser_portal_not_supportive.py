"""The portal's Not Supportive tab, opened in a real browser.

This tab is drawn entirely in the browser from one API call, and the two ways
it can go wrong are both invisible to a response test — the page returns 200
and the tab is simply empty, which reads as *OGA has nothing of yours here*.
For this list that is the pleasant misreading, which makes it the dangerous
one: a supplier concludes none of their products failed.

The *membership* question — which antibodies belong on the list, and the
untested-is-not-failed rule underneath it — is answered far more cheaply in
``core/tests_not_supportive.py``. This is only about the press: does the tab
draw, does a figure arrive, and do the two downloads produce a file.
"""
import io
import unittest
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import tag
from django.urls import reverse
from PIL import Image

# The browser and the skip condition are `pipeline/tests_browser_board.py`'s —
# a second chrome-finder here is exactly the copy that drifts.
from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT

from core.models import APIConsumer
from pipeline.models import Antibody, Company, PublicationImage, Target

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (30, 90, 200)).save(buf, "PNG")
    return buf.getvalue()


def _publish(antibody, application):
    image = PublicationImage(antibody=antibody, application_type=application)
    image.image.save(f"{antibody.catalogue_number}_{application}.png",
                     ContentFile(_png()), save=False)
    image.save()
    return image


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class NotSupportiveTabInARealBrowserTests(StaticLiveServerTestCase):
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
        company = Company.objects.create(name="Abcam", display_name="Abcam")
        target = Target.objects.create(gene_name="SNCA", protein_name="Syn")
        # Supportive on the gene: it curates the gene (which is what makes a
        # missing flag mean "tested and not supportive" rather than "untested")
        # AND it is the reference example the gene heading draws.
        good = Antibody.objects.create(target=target, company=company,
                                       catalogue_number="ab-good",
                                       wb_recommended=True)
        _publish(good, "WB")
        failer = Antibody.objects.create(
            target=target, company=company, catalogue_number="ab138501",
            rrid="AB_2687467")
        _publish(failer, "WB")

        # A second gene, so the page has a transition to demarcate.
        other = Target.objects.create(gene_name="TRPA1", protein_name="Trp")
        Antibody.objects.create(target=other, company=company,
                                catalogue_number="ab-good-2", wb_recommended=True)
        second = Antibody.objects.create(target=other, company=company,
                                         catalogue_number="ab-other")
        _publish(second, "WB")

        self.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")
        self.page = self._browser.new_page(accept_downloads=True)

        # A published figure's URL is absolute — R2 gives one, and local storage
        # gets `BASE_URL` in front of it the same way `api_views` does. Under
        # the test server that would point every image at the live site, so the
        # constant is pointed at this server instead. What is being tested is
        # that a figure arrives at all, not which host it came from.
        patch = mock.patch("core.api_not_supportive.BASE_URL",
                           self.live_server_url)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.page.close()

    def _open_tab(self):
        self.page.goto(f"{self.live_server_url}{reverse('portal')}"
                       f"?key={self.consumer.api_key}")
        self.page.wait_for_selector("#dashboard:not(.hidden)")
        self.page.click('[data-tab="notsupportive"]')
        self.page.wait_for_selector("#notsupportive-content .ab-card")

    @tag("commissioning")
    def test_the_tab_draws_the_failing_antibody_and_its_published_figure(self):
        self._open_tab()
        body = self.page.text_content("#notsupportive-content")
        self.assertIn("ab138501", body)
        self.assertIn("SNCA", body)
        # Both counts, so "failed all four" cannot read like "failed the one".
        self.assertIn("1 of 1 tested application", body)
        # The supportive antibody is never a CARD — but it is the gene's
        # reference example, so its catalogue number is on the page in the
        # heading. Assert on the cards, which is where the claim lives.
        cards = self.page.text_content("#notsupportive-content .ab-card")
        self.assertNotIn("ab-good", cards)
        # These figures are public objects, so a plain <img src> is right here —
        # unlike the pre-release tab, where the key is a header.
        self.page.wait_for_function(
            "() => { const i = document.querySelector('#notsupportive-content img');"
            " return i && i.complete && i.naturalWidth > 0; }")

    @tag("commissioning")
    def test_the_manifest_is_printed_before_the_press(self):
        """A download owes a manifest, not a gate."""
        self._open_tab()
        body = self.page.text_content("#notsupportive-content")
        self.assertIn("2 antibodies", body)
        self.assertIn("2 application results", body)
        # Both rungs are named even with the tick off — and what is EXCLUDED is
        # said as an exclusion, never as a count of zero.
        self.assertIn("Not supportive", body)
        self.assertIn("Limited support", body)
        self.assertIn("are left out of this list", body)

    # The only one here on the push tier, and the tier rule is why. The other
    # three prove the tab worked when it was built and fail LOUDLY — a blank
    # tab, a broken image, a button that produces nothing — which is what
    # earns `commissioning`. This one guards a SILENT failure: if the page
    # stopped sending the gene to the download endpoint, it would still draw,
    # the button would still work, and the file would quietly hold a different
    # set from the count printed beside it. `tests_not_supportive.py` pins the
    # server half of that far more cheaply; only a browser can ask whether the
    # page sends the filter at all.
    def test_the_gene_picker_refetches_and_says_the_downloads_moved_with_it(self):
        """The picker narrows the files too, so the page has to say so."""
        self._open_tab()
        self.page.select_option("#ns-gene", "SNCA")
        self.page.wait_for_selector("#notsupportive-content .ab-card")
        body = self.page.text_content("#notsupportive-content")
        self.assertIn("ab138501", body)
        self.assertIn("It covers SNCA only", body)

    @tag("commissioning")
    def test_the_download_produces_a_file(self):
        """The key is a header, so it cannot be a plain link."""
        self._open_tab()
        with self.page.expect_download() as caught:
            self.page.click("#ns-csv")
        self.assertTrue(caught.value.suggested_filename.endswith(".csv"),
                        caught.value.suggested_filename)
        # A download says it happened — an invisible one reads as a click that
        # did not register.
        self.page.wait_for_selector("#toast.show")
        self.assertIn("Saved", self.page.text_content("#toast"))

    @tag("commissioning")
    def test_the_strip_rates_all_four_and_every_figure_is_drawn(self):
        """All four ratings, and no figure behind a click.

        A strip that drew only the failing applications would leave the
        untested ones nowhere on the card, which is the one distinction this
        list must never blur. And every figure is on the page rather than one
        at a time — that is the whole reason the PDF went.
        """
        self._open_tab()
        # Scoped to the first GENE SECTION: `.ab-card:first-of-type` matches the
        # first card inside every section, and there are two sections.
        card = '#notsupportive-content .ns-gene:first-of-type .ab-card'
        chips = self.page.query_selector_all(f'{card} .ns-chip')
        self.assertEqual([c.inner_text().split("\n")[0] for c in chips],
                         ["WB", "IP", "ICC-IF", "FC"])
        self.assertIn("Not tested", self.page.text_content(f'{card} .ns-chips'))

        # Every figure this antibody has is visible without touching anything.
        figures = self.page.query_selector_all(f'{card} .ns-figures img')
        self.assertEqual(len(figures), 1, "one published figure, one image")
        self.page.wait_for_function(
            "() => Array.from(document.querySelectorAll("
            "'#notsupportive-content .ns-figures img'))"
            ".every(i => i.complete && i.naturalWidth > 0)")

    @tag("commissioning")
    def test_the_page_is_grouped_by_gene_with_the_reference_example(self):
        self._open_tab()
        headings = [h.inner_text() for h in self.page.query_selector_all(
            "#notsupportive-content .ns-gene-name")]
        self.assertEqual(headings, ["SNCA", "TRPA1"])

        section = self.page.text_content(
            "#notsupportive-content .ns-gene:first-of-type")
        self.assertIn("What a supportive result on SNCA looks like", section)
        self.assertIn("ab-good", section)
        # Named as somebody else's product, or a supplier reads it as theirs.
        self.assertIn("different", section)
        self.page.wait_for_function(
            "() => { const i = document.querySelector("
            "'#notsupportive-content .ns-ref img');"
            " return i && i.complete && i.naturalWidth > 0; }")

    def test_the_limited_support_tick_is_off_by_default_and_refetches(self):
        """The one that fails silently: a tick the downloads do not follow.

        If the page stopped sending `include_limited` with the file requests,
        it would still draw, the tick would still move the list, and the
        download would quietly hold a different set from the count beside it.
        """
        self._open_tab()
        tick = self.page.query_selector("#ns-limited")
        self.assertIsNotNone(tick, "the Limited support tick is not on the page")
        self.assertFalse(tick.is_checked(), "it must be off by default")
        body = self.page.text_content("#notsupportive-content")
        self.assertIn("Limited support is excluded", body)

        self.page.check("#ns-limited")
        self.page.wait_for_function(
            "() => document.querySelector('#notsupportive-content')"
            ".textContent.includes('Limited support is included')")
        self.assertTrue(self.page.query_selector("#ns-limited").is_checked())
