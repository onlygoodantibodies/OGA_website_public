"""Set recommendations — the page where a gene's antibodies get their verdict.

Three findings from the twentieth field test, 5 Aug 2026.

**The navigation bar emptied out.** The page declared
``.hidden { display: none !important; }`` for its own image overlay, and the
chrome shows its desktop half with Tailwind's ``hidden md:flex``. An
``!important`` on a bare class beats ``md:flex`` whatever the specificity, so
the top bar rendered with nothing in it but the wordmark — no Task hub, no
Browse, no search box, no sign-out — while every one of those was in the page.
That is pinned by ``tests_navigation.py::NoPageStyleBlockOverridesTheChromeTests``,
which asks the *rule* across every template rather than this one page, and by a
browser test that reads what is actually visible.

**``?gene=`` was ignored**, on the one page in the app with a 160-item dropdown
and no link to it from a gene's own page. The nav links on the same page picked
the value up perfectly — so it was being read, and not used by the control it
is for.

**A western blot was judged from a 120px thumbnail**, and the only way to
enlarge one was right-click, which is also how the browser's own menu opens.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from pipeline.models import Antibody, Company, PublicationImage, Site, Target
from pipeline.tests_timeouts import DB, _member_client

PAGE = "/pipeline/recommendations/"


class TheRecommendationsPageTakesAGeneTests(TestCase):
    """``?gene=`` means the same thing here as it does on the four boards."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)
        company = Company.objects.using(DB).create(name="Abcam")

        self.pictured = Target.objects.using(DB).create(gene_name="TRPA1")
        antibody = Antibody.objects.using(DB).create(
            catalogue_number="ab58844", target=self.pictured,
            company=company, site=self.site)
        PublicationImage.objects.using(DB).create(
            antibody=antibody, application_type="WB",
            image=SimpleUploadedFile("wb.png", b"not-really-a-png"))

        # On file, real, and with nothing published — which is the only reason a
        # genuine gene is missing from this particular picker.
        self.bare = Target.objects.using(DB).create(gene_name="STMN2")
        Antibody.objects.using(DB).create(
            catalogue_number="ab12345", target=self.bare,
            company=company, site=self.site)

    def test_a_gene_with_figures_is_opened(self):
        resp = self.client.get(PAGE, {"gene": "TRPA1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["requested_gene"], "TRPA1")
        self.assertEqual(resp.context["gene_note"], "")

    def test_the_case_a_link_carries_does_not_have_to_match(self):
        """The `<option>` values are what `rec_genes` stored, so the server
        hands back the target's own spelling — otherwise `?gene=trpa1` sets the
        select to a value no option has and the picker stays on its
        placeholder, which is indistinguishable from the parameter being
        ignored. It matters for the eight mis-cased symbols in particular: a
        link written `?gene=RAB44` has to open the row stored as `Rab44`."""
        resp = self.client.get(PAGE, {"gene": "trpa1"})
        self.assertEqual(resp.context["requested_gene"], "TRPA1")

    def test_a_gene_with_no_published_figures_says_so(self):
        """Not the same answer as "no such gene", and from outside the picker
        they look identical: both leave it reading "Select a gene"."""
        resp = self.client.get(PAGE, {"gene": "STMN2"})
        self.assertEqual(resp.context["requested_gene"], "")
        note = resp.context["gene_note"]
        self.assertIn("STMN2", note)
        self.assertIn("no published validation figures", note)
        self.assertIn("Publish figures", note)

    def test_a_gene_that_is_not_in_the_pipeline_says_that_instead(self):
        resp = self.client.get(PAGE, {"gene": "ZZZZZZ"})
        self.assertEqual(resp.context["requested_gene"], "")
        self.assertIn("no gene called ZZZZZZ", resp.context["gene_note"])

    def test_no_gene_asked_for_is_not_a_complaint(self):
        resp = self.client.get(PAGE)
        self.assertEqual(resp.context["requested_gene"], "")
        self.assertEqual(resp.context["gene_note"], "")

    def test_the_note_reaches_the_page(self):
        """Computed and then not drawn is the same bug as not computed."""
        body = self.client.get(PAGE, {"gene": "ZZZZZZ"}).content.decode()
        self.assertIn("no gene called ZZZZZZ", body)

    def test_a_gene_list_opens_the_first_and_says_which(self):
        """`?gene=` carries several on the boards — a bulk add lands on one —
        and this page sets one gene at a time. Taking the first silently is the
        kind of drop this app keeps being bitten by."""
        resp = self.client.get(PAGE, {"gene": "TRPA1, STMN2"})
        self.assertEqual(resp.context["requested_gene"], "TRPA1")
        self.assertIn("STMN2", resp.context["gene_note"])
        self.assertIn("one gene at a time", resp.context["gene_note"])


class AGenesOwnPageLinksToItsRecommendationsTests(TestCase):
    """Setting a gene's recommendations meant the task hub and then scrolling a
    160-item dropdown by hand — from a page that names the gene in its URL."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")

    def test_the_antibodies_panel_offers_set_recommendations_for_this_gene(self):
        body = self.client.get(
            reverse("pipeline:target_detail", args=[self.target.pk])
        ).content.decode()
        self.assertIn(f"{PAGE}?gene=TRPA1", body)


class EnlargingAFigureIsAnOrdinaryClickTests(TestCase):
    """Right-click is also how the browser's own menu opens, and choosing a
    recommended antibody off a thumbnail is the most consequential judgement
    anybody makes in this app.

    Source-level, so it can only say the control is *there* — that it works is
    ``tests_browser_board.py``'s job. The pair is deliberate: this one costs
    milliseconds and catches the control being deleted.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)

    def test_the_page_builds_an_enlarge_button(self):
        body = self.client.get(PAGE).content.decode()
        self.assertIn("thumb-zoom", body)
        self.assertIn("event.stopPropagation()", body,
                      "the thumbnail's own click writes a recommendation, so "
                      "enlarging must not reach it")

    def test_the_instructions_no_longer_teach_right_click_as_the_only_way(self):
        body = self.client.get(PAGE).content.decode()
        self.assertNotIn("Right-click an image to enlarge it.", body)
