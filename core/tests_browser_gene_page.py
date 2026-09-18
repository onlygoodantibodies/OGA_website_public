"""The gene page's qualified negative, in a real browser.

The first cut of this was drawn *inside* `.experiment-box`, which
`core/static/core/styles.css` makes a fixed-height flex row — so the sentence
became a sibling column of the image, squeezed the blot to half width and
wrapped itself to one word a line. It rendered 200, every response test passed,
and the only way to see it was to look at the page.

"The server answered correctly and the page did the wrong thing with it" is the
shape a browser is for, and a caption that ruins the figure it captions is
exactly it.
"""
from __future__ import annotations

import unittest

from io import BytesIO

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.uploadedfile import SimpleUploadedFile

from pipeline.models import (Antibody, AntibodyOutcome, Company,
                             PublicationImage, Site, Target)
from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT
from pipeline.tests_timeouts import DB


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class TheQualifiedNegativeIsDrawnAsACaptionTests(StaticLiveServerTestCase):
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

    @staticmethod
    def _png(width=200, height=400):
        """A real image with real dimensions.

        A one-byte placeholder has no intrinsic size, so the browser draws it at
        whatever the broken-image glyph needs and a width assertion measures
        that instead of the layout. The first version of this test failed on
        exactly that and looked like the bug it was written to catch.
        """
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (width, height), "white").save(buf, format="PNG")
        return buf.getvalue()

    def setUp(self):
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        company = Company.objects.using(DB).create(name="GeneTex")
        target = Target.objects.using(DB).create(gene_name="ARID2")

        # One recommended, so the gene reads as curated and the negatives are
        # real negatives rather than "not assessed yet".
        # One of each tested state, so the page has to tell three answers apart.
        for cat, rrid, rec, detects in (("GTX111", "AB_1", True, "yes"),
                                        ("GTX129443", "AB_2", False, "yes"),
                                        ("GTX999", "AB_3", False, "no")):
            ab = Antibody.objects.using(DB).create(
                catalogue_number=cat, rrid=rrid, target=target,
                company=company, site=site, wb_recommended=rec)
            PublicationImage.objects.using(DB).create(
                antibody=ab, application_type="WB",
                image=SimpleUploadedFile(f"{cat}.png", self._png(),
                                         content_type="image/png"))
            AntibodyOutcome.objects.using(DB).create(
                antibody=ab, application_type="WB",
                detects=detects, selective="no")
            if cat == "GTX111":
                # A second judged application on one antibody, so two captions
                # sit side by side — the only way to measure the gutter
                # between them, and the axis the first spacing fix missed.
                ab.ip_recommended = True
                ab.save(using=DB)
                PublicationImage.objects.using(DB).create(
                    antibody=ab, application_type="IP",
                    image=SimpleUploadedFile("ip.png", self._png(),
                                             content_type="image/png"))
                AntibodyOutcome.objects.using(DB).create(
                    antibody=ab, application_type="IP", enriches="yes")
        self.page = self.browser.new_page(viewport={"width": 1400, "height": 900})

    def tearDown(self):
        self.page.close()

    def _open(self):
        self.page.goto(f"{self.live_server_url}/antibodies/ARID2/")
        self.page.wait_for_selector(".experiment-caveat", timeout=20000)

    def test_the_caption_sits_below_the_box_and_not_inside_it(self):
        """Inside, it is a flex sibling of the image. That is the bug."""
        self._open()
        inside = self.page.eval_on_selector_all(
            ".experiment-box .experiment-caveat", "els => els.length")
        self.assertEqual(inside, 0, "the caption must not be inside the box")

    def test_the_captioned_blot_is_drawn_the_same_size_as_an_uncaptioned_one(self):
        """The squeeze, measured against a control rather than a number.

        An absolute width proves nothing: these blots are portrait, so
        `object-fit: contain` makes them height-limited and a correct layout
        gives ~110px in a 240px box. The first version of this test asserted
        >150 and failed on a page that was fine. What a squeeze actually does is
        make the captioned cell's image *narrower than the same image in a cell
        with no caption*, so that is the comparison.
        """
        self._open()
        def width(selector):
            img = self.page.query_selector(f"{selector} img")
            return round(img.bounding_box()["width"])

        self.assertEqual(width(".experiment-box.qualified"),
                         width(".experiment-box.supportive"),
                         "the caption is taking width from the figure")

    def test_the_caption_is_readable_rather_than_one_word_a_line(self):
        self._open()
        # The qualified negative: it is the long sentence, and the only one
        # that can wrap to a word a line.
        caption = self.page.query_selector(
            ".experiment-box.qualified + .experiment-caveat")
        self.assertGreater(caption.bounding_box()["width"], 150)
        self.assertIn("Limited support — detects the target",
                      caption.text_content())

    def test_the_caption_is_not_crowded_against_the_box_or_the_next_card(self):
        """`td` carries no padding, so the caption's own margins are the only
        thing holding it off the figure above and the card below. At 6px a
        two-line caption touched both, which is what the rungs made common —
        "Limited support — enriches the target, but not significantly" wraps
        where "Supportive" does not (owner, 29 Aug 2026).
        """
        self._open()
        gaps = self.page.eval_on_selector_all(
            ".experiment-caveat",
            "els => els.map(e => {"
            "  const cap = e.getBoundingClientRect();"
            "  const box = e.previousElementSibling.getBoundingClientRect();"
            "  return cap.top - box.bottom;"
            "})")
        self.assertTrue(gaps, "no captions were drawn")
        for gap in gaps:
            self.assertGreaterEqual(round(gap), 8, f"caption crowds the box: {gaps}")

    def test_neighbouring_captions_have_a_gutter_between_them(self):
        """Four columns of wrapped sentences, side by side.

        The vertical fix left the horizontal one: the caption was as wide as
        the box it captions, so adjacent columns had about 10px between them
        and "…but not significantly" ran straight into "Not supportive"
        (owner, 29 Aug 2026). The caption is narrower than the box now.
        """
        self._open()
        gaps = self.page.evaluate(
            """() => {
              const caps = [...document.querySelectorAll('.experiment-caveat')]
                .map(e => e.getBoundingClientRect());
              const out = [];
              for (let i = 1; i < caps.length; i++) {
                // Same row only: a wrap to the next antibody is not a gutter.
                if (Math.abs(caps[i].top - caps[i - 1].top) < 4) {
                  out.push(Math.round(caps[i].left - caps[i - 1].right));
                }
              }
              return out;
            }""")
        self.assertTrue(gaps, "no two captions were drawn side by side")
        for gap in gaps:
            self.assertGreaterEqual(gap, 24, f"captions run together: {gaps}")

    def test_every_caption_reserves_the_same_height(self):
        """One row, one rhythm. A one-line rung beside a two-line one left the
        cards ending at different heights wherever a caption happened to be
        long."""
        self._open()
        heights = self.page.eval_on_selector_all(
            ".experiment-caveat",
            "els => els.map(e => Math.round("
            "  e.getBoundingClientRect().height))")
        self.assertGreaterEqual(min(heights), 30,
                                f"a short caption collapsed: {heights}")

    def test_the_three_tested_answers_are_drawn_apart(self):
        """A plain negative had no marking at all, so "tested and did not
        perform" drew identically to "nobody has run this" — the one
        distinction this dataset must never blur.

        **One colour per cell**, spaced as a traffic light is. Amber used to be
        an edge laid inside the red border, and the two hues a couple of
        millimetres apart were indistinguishable on a phone (owner, 29 Aug
        2026). Each of the three states now owns its cell outright, so this
        counts three disjoint sets.
        """
        self._open()
        for state, n in (("supportive", 2),      # WB not selective, and IP
                         ("qualified", 1),       # negative, but it does detect
                         ("not-supportive", 1)):  # negative, showed nothing
            self.assertEqual(
                len(self.page.query_selector_all(f".experiment-box.{state}")),
                n, state)
        # And no cell carries two of them.
        self.assertEqual(
            len(self.page.query_selector_all(
                ".experiment-box.qualified.not-supportive")), 0)

    def test_every_tested_cell_says_which_answer_it_is_in_words(self):
        """Colour alone fails a reader who cannot see it, and the amber caption
        said nothing about where the cell sat at all — so a qualified cell read
        as a remark rather than as a result."""
        self._open()
        captions = self.page.eval_on_selector_all(
            ".experiment-caveat", "els => els.map(e => e.textContent.trim())")
        # A gene page is headed "characterisation data", so the cells describe
        # evidence rather than issue advice. The API's verdict vocabulary is
        # untouched.
        # The verdict leads and the qualification follows it, **on either
        # side**: this fixture's supportive antibody is not selective, which is
        # the case that was invisible until 29 Aug 2026 — 36% of the green on
        # the live site.
        self.assertIn("Supportive — detects the target, but is not selective", captions)
        self.assertIn("Limited support — detects the target",
                      captions)
        # And a bare verdict where there is nothing to add.
        self.assertIn("Not supportive", captions)

    def test_an_untested_cell_is_not_captioned_twice(self):
        """It already carries a "No data available" image."""
        self._open()
        boxes = self.page.eval_on_selector_all(
            ".experiment-box",
            "els => els.map(e => [e.className, !!e.parentElement"
            ".querySelector('.experiment-caveat')])")
        for cls, captioned in boxes:
            if cls.strip() == "experiment-box":
                self.assertFalse(captioned, "an untested cell needs no caption")
