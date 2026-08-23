"""A discontinued antibody is hidden from the gene page, never denied.

Every published antibody was checked against its supplier's own catalogue in
August 2026 (``pipeline/data/antibody_availability_2026_08.csv``,
``manage.py import_antibody_availability``). That moved ``out_of_market`` from
58 rows to 182, so the gene page's discontinued filter — which had been
defaulting to *hide* since it was written, with no control anywhere on the page
to turn it off — went from quietly dropping 3.5% of the dataset to quietly
dropping 11% of it.

What is pinned here is the pair of failures that are silent:

* **The count and the list must agree.** A filter that removes rows without
  saying how many is indistinguishable from a gene that never had them, and the
  home page's total still counts every published antibody — so a gene page that
  says nothing is one of two disagreeing answers on one site.
* **A row the reader named must not disappear.** The home page's search box
  resolves a catalogue number and redirects to ``?ab=``. Hide the row it lands
  on and the page reports *"No X antibody here matches …"* about a reagent with
  published figures, which is the one thing this site must never say. Discovered
  by reading the filter against ``public_search``, which does not exclude
  discontinued antibodies from its index and should not.
"""
from django.test import TestCase
from django.urls import reverse

from pipeline.models import Antibody, Company, PublicationImage, Target

DB = "pipeline_db"


class TheGenePageHidesDiscontinuedByDefaultTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="Proteintech")
        cls.target = Target.objects.create(
            protein_name="Synuclein", gene_name="SNCA")

        def antibody(catalogue, *, gone):
            ab = Antibody.objects.create(
                target=cls.target, company=company,
                catalogue_number=catalogue, out_of_market=gone)
            PublicationImage.objects.create(
                antibody=ab, application_type="WB",
                image=f"pubs/{catalogue}.png")
            return ab

        cls.live = antibody("10842-1-AP", gone=False)
        cls.gone = antibody("60009-1-Ig", gone=True)
        cls.also_gone = antibody("66412-1-Ig", gone=True)

    def _page(self, **params):
        url = reverse("antibody_table", kwargs={"gene_name": "SNCA"})
        return self.client.get(url, params)

    def test_a_discontinued_antibody_is_not_in_the_default_page(self):
        body = self._page().content.decode()
        self.assertIn("10842-1-AP", body)
        self.assertNotIn("60009-1-Ig", body)

    def test_the_default_page_says_how_many_it_is_hiding(self):
        # The number is the whole point: without it the count above the table
        # is smaller than the home page's for no stated reason.
        page = self._page()
        self.assertEqual(page.context["hidden_discontinued"], 2)
        self.assertEqual(page.context["antibody_count"], 1)
        self.assertContains(page, "Not shown: 2 antibodies")

    def test_the_tick_shows_them(self):
        page = self._page(discontinued="show")
        body = page.content.decode()
        self.assertIn("60009-1-Ig", body)
        self.assertIn("66412-1-Ig", body)
        self.assertEqual(page.context["antibody_count"], 3)
        self.assertEqual(page.context["hidden_discontinued"], 0)
        self.assertEqual(page.context["shown_discontinued"], 2)

    def test_the_tick_box_is_on_the_page_in_both_states(self):
        # A control that is simply absent is a feature a reader concludes does
        # not exist — which is exactly what the page was before this.
        self.assertContains(self._page(), 'name="discontinued"')
        self.assertContains(self._page(discontinued="show"), "checked")

    def test_the_retired_filters_value_still_shows_rather_than_hides(self):
        # `?discontinued=yes` meant "discontinued only" on the retired page. A
        # bookmark carrying it should not come back emptier than it went.
        self.assertContains(self._page(discontinued="yes"), "60009-1-Ig")

    def test_a_gene_with_nothing_discontinued_says_nothing(self):
        Antibody.objects.filter(out_of_market=True).update(out_of_market=False)
        page = self._page()
        self.assertEqual(page.context["hidden_discontinued"], 0)
        self.assertNotContains(page, "Not shown:")

    def test_the_hidden_count_respects_the_other_filters(self):
        # A count computed over the whole gene rather than the filtered set
        # would contradict the table directly above it.
        self.gone.host_species = "mouse"
        self.gone.save()
        self.also_gone.host_species = "rabbit"
        self.also_gone.save()
        self.assertEqual(
            self._page(host="mouse").context["hidden_discontinued"], 1)


class ANamedDiscontinuedRowIsStillShownTests(TestCase):
    """``?ab=`` names a row; the filter must not answer that we do not have it."""

    databases = {"default", "pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="Abcam")
        cls.target = Target.objects.create(protein_name="Tau", gene_name="MAPT")
        for catalogue, gone in (("ab80579", False), ("ab32057", True)):
            ab = Antibody.objects.create(
                target=cls.target, company=company,
                catalogue_number=catalogue, out_of_market=gone)
            PublicationImage.objects.create(
                antibody=ab, application_type="WB", image=f"pubs/{catalogue}.png")

    def _page(self, **params):
        url = reverse("antibody_table", kwargs={"gene_name": "MAPT"})
        return self.client.get(url, params)

    def test_searching_a_discontinued_catalogue_number_finds_it(self):
        page = self._page(ab="ab32057")
        self.assertContains(page, "ab32057")
        self.assertEqual(page.context["focus_missing"], "")

    def test_the_page_does_not_claim_it_has_no_such_antibody(self):
        self.assertNotContains(self._page(ab="ab32057"), "No MAPT antibody here matches")

    def test_the_named_row_is_exempt_and_the_rest_are_not(self):
        # One row is let through by name. That is not the tick being turned on.
        page = self._page(ab="ab32057")
        self.assertFalse(page.context["show_discontinued"])
        self.assertEqual(page.context["hidden_discontinued"], 0)

    def test_a_catalogue_number_this_gene_does_not_have_still_says_so(self):
        page = self._page(ab="ab999999")
        self.assertEqual(page.context["focus_missing"], "ab999999")

    def test_the_exempted_row_says_it_is_discontinued(self):
        # Letting it through silently would put a reagent nobody can buy at the
        # top of the page with nothing to say so.
        self.assertContains(self._page(ab="ab32057"), "Discontinued")


class NoTemplateCommentReachesTheGenePageTests(TestCase):
    """The public counterpart of ``BoardsRenderCleanlyTests``, which had no reach here.

    Django's short comment form does not span lines and ends at the *first*
    closing delimiter, so a wrapped one renders as text and one that names the
    delimiters prints its own tail. The four pipeline boards have been checked
    for this since a comment leaked onto one; the gene page — which is the page
    the public actually reads — was not, and a three-line ``{# … #}`` added
    alongside the discontinued tick promptly rendered on it.
    """

    databases = {"default", "pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="Abcam")
        target = Target.objects.create(protein_name="Parkin", gene_name="PRKN")
        ab = Antibody.objects.create(
            target=target, company=company, catalogue_number="ab77924")
        PublicationImage.objects.create(
            antibody=ab, application_type="WB", image="pubs/prkn.png")

    def test_neither_delimiter_reaches_the_reader(self):
        for params in ({}, {"discontinued": "show"}, {"ab": "ab77924"}):
            for delim in ("{" + "#", "#" + "}"):
                with self.subTest(params=params, delim=delim):
                    page = self.client.get(
                        reverse("antibody_table", kwargs={"gene_name": "PRKN"}),
                        params)
                    self.assertEqual(page.status_code, 200)
                    self.assertNotIn(delim, page.content.decode())
