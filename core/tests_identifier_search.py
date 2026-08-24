"""Searching by the identifier a reader holds, and landing on the row.

The site could only be searched by gene, so ``14060-1-AP`` answered "no gene
matches" about an antibody we hold two published figures for. These pin the parts
of the fix that would be **silently** wrong — a miss that reads as an absence, a
landing page that quietly does not highlight anything, a resolver that picks one
of two products, and an index that reaches records with nothing behind them.

Deliberately not pinned: the wording of the chip, the ranking of suggestions, the
styling. Those are meant to change once somebody has used this.
"""
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from core import public_search
from pipeline.models import Antibody, Company, PublicationImage, Target

# Assert on the *applied* markup, never on the bare class name: the page carries
# a <style> block naming all three, so `assertNotIn("oga-ab-focused", html)`
# passes on the stylesheet and says nothing about any row.
FOCUSED_ROW = 'class="row-box oga-ab-focused"'
CHIP = '<div class="oga-ab-focus" role="status">'
MISSING_CHIP = '<div class="oga-ab-focus oga-ab-focus-missing" role="status">'


class IdentifierSearchTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()  # both the gene index and the antibody index are cached
        proteintech = Company.objects.create(name="Proteintech")
        abcam = Company.objects.create(name="Abcam")

        self.prkn = Target.objects.create(
            protein_name="Parkin", gene_name="PRKN", aliases="PARK2")
        self.published = Antibody.objects.create(
            target=self.prkn, company=proteintech,
            catalogue_number="14060-1-AP", rrid="AB_2878005",
            clone_id="", host_species="rabbit")
        PublicationImage.objects.create(
            antibody=self.published, application_type="WB",
            image="pubs/prkn_wb.png")

        self.second = Antibody.objects.create(
            target=self.prkn, company=abcam,
            catalogue_number="ab77924", rrid="AB_1566102")
        PublicationImage.objects.create(
            antibody=self.second, application_type="IP",
            image="pubs/prkn_ip.png")

        # Real row, nothing published against it. Must not be reachable.
        self.unpublished = Antibody.objects.create(
            target=self.prkn, company=abcam, catalogue_number="ab-secret-1",
            rrid="AB_9999999")

        # A placeholder where an RRID should be. `?` is not an identifier, and
        # indexing it would make a single typed character match every such row.
        self.no_rrid = Antibody.objects.create(
            target=self.prkn, company=proteintech,
            catalogue_number="10683-1-AP", rrid="?")
        PublicationImage.objects.create(
            antibody=self.no_rrid, application_type="WB",
            image="pubs/prkn_wb2.png")

    # --- what the index may reach -------------------------------------------

    def test_a_published_antibody_is_found_by_its_catalogue_number(self):
        hits = public_search.search("14060-1-AP")
        self.assertEqual([h["id"] for h in hits], [self.published.pk])

    def test_a_published_antibody_is_found_by_its_rrid(self):
        hits = public_search.search("AB_2878005")
        self.assertEqual([h["id"] for h in hits], [self.published.pk])

    def test_an_antibody_with_nothing_published_is_not_searchable(self):
        """Same scope rule as the gene pages — there is nothing to show."""
        self.assertEqual(public_search.search("ab-secret-1"), [])
        self.assertEqual(public_search.search("AB_9999999"), [])

    def test_a_placeholder_rrid_is_not_an_identifier(self):
        """`?` in the RRID column means nobody recorded one, not a value."""
        self.assertEqual(public_search.search("?"), [])
        # …and the row is still reachable by the identifier it does have.
        self.assertEqual([h["id"] for h in public_search.search("10683-1-AP")],
                         [self.no_rrid.pk])

    # --- the typesetting, which is the whole reason for the normaliser -------

    def test_a_publishers_typesetting_still_finds_the_record(self):
        """`14,060–1-AP` is how Springer sets it; the stored form has neither.

        A miss here is the harmful direction: it reads as "OGA has not tested
        this" about a reagent carrying a published figure.
        """
        for printed in ("14,060–1-AP", "14 060-1-AP", "#14060-1-AP",
                        "cat. no. 14060-1-AP", "140601AP"):
            with self.subTest(printed=printed):
                hits = public_search.search(printed)
                self.assertEqual([h["id"] for h in hits], [self.published.pk])

    # --- resolving to one row, or refusing to ------------------------------

    def test_an_identifier_naming_one_antibody_resolves(self):
        self.assertEqual(public_search.resolve("14060-1-AP")["id"],
                         self.published.pk)

    def test_an_identifier_two_products_share_resolves_to_neither(self):
        """A catalogue number is not unique across suppliers.

        Picking the first would send a reader to a verdict about a reagent they
        do not own, with nothing on the page to say so.
        """
        other_gene = Target.objects.create(
            protein_name="Synuclein", gene_name="SNCA")
        clash = Antibody.objects.create(
            target=other_gene, company=Company.objects.create(name="Novus"),
            catalogue_number="14060-1-AP", rrid="AB_5555555")
        PublicationImage.objects.create(
            antibody=clash, application_type="WB", image="pubs/snca_wb.png")
        cache.clear()

        self.assertIsNone(public_search.resolve("14060-1-AP"))
        # Both are still offered as suggestions — that is the point of a list.
        self.assertEqual(len(public_search.search("14060-1-AP")), 2)

    # --- the endpoint ------------------------------------------------------

    def test_the_endpoint_answers_with_a_link_to_the_row(self):
        response = self.client.get(reverse("antibody_search"), {"q": "14060-1-AP"})
        self.assertEqual(response.status_code, 200)
        hit = response.json()["antibodies"][0]
        self.assertEqual(hit["gene"], "PRKN")
        self.assertEqual(hit["supplier"], "Proteintech")
        self.assertIn("/antibodies/PRKN/", hit["url"])
        self.assertIn("ab=", hit["url"])

    def test_the_endpoint_is_quiet_about_an_empty_query(self):
        response = self.client.get(reverse("antibody_search"), {"q": "  "})
        self.assertEqual(response.json()["antibodies"], [])

    def test_every_suggested_antibody_leads_to_a_page_that_highlights_it(self):
        """The suggestion's own URL, followed. No hit may lead to a page that
        renders as though nothing was asked for — that is the silent failure."""
        for query in ("14060-1-AP", "AB_1566102", "10683-1-AP"):
            with self.subTest(query=query):
                hit = self.client.get(
                    reverse("antibody_search"), {"q": query}).json()["antibodies"][0]
                page = self.client.get(hit["url"])
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, f'id="ab-{hit["id"]}"')
                self.assertContains(page, FOCUSED_ROW)

    # --- the gene page -----------------------------------------------------

    def _gene_page(self, **params):
        return self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "PRKN"}), params)

    def test_ab_puts_the_named_row_first_and_marks_it(self):
        html = self._gene_page(ab="ab77924").content.decode()
        self.assertIn(f'id="ab-{self.second.pk}"', html)
        self.assertIn(FOCUSED_ROW, html)
        # First of the real rows, ahead of the one the shuffle would have put there.
        self.assertLess(html.index(f'id="ab-{self.second.pk}"'),
                        html.index(f'id="ab-{self.published.pk}"'))

    def test_the_other_antibodies_stay_on_the_page(self):
        """The alternatives are the reason to land here rather than on a card."""
        html = self._gene_page(ab="ab77924").content.decode()
        self.assertIn(f'id="ab-{self.published.pk}"', html)
        self.assertIn("Show all", html)

    def test_no_ab_leaves_the_page_exactly_as_it_was(self):
        html = self._gene_page().content.decode()
        self.assertNotIn(FOCUSED_ROW, html)
        self.assertNotIn(CHIP, html)
        self.assertNotIn(MISSING_CHIP, html)

    def test_an_ab_this_gene_does_not_have_says_so(self):
        html = self._gene_page(ab="nothing-like-this").content.decode()
        self.assertIn(MISSING_CHIP, html)
        self.assertNotIn(FOCUSED_ROW, html)

    def test_an_ab_hidden_by_a_filter_does_not_get_a_chip(self):
        """The row is genuinely not on the page, so a chip would point at nothing."""
        html = self._gene_page(ab="14060-1-AP", host="mouse").content.decode()
        self.assertNotIn(FOCUSED_ROW, html)
        self.assertNotIn(CHIP, html)

    def test_ab_reaches_a_row_by_rrid_too(self):
        html = self._gene_page(ab="AB_2878005").content.decode()
        self.assertIn(f'id="ab-{self.published.pk}"', html)
        self.assertIn(FOCUSED_ROW, html)

    # --- the homepage ------------------------------------------------------

    def test_the_homepage_sends_an_exact_identifier_to_the_row(self):
        response = self.client.get(reverse("home"), {"search": "14060-1-AP"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/antibodies/PRKN/", response["Location"])
        self.assertIn("ab=", response["Location"])

    def test_the_homepage_still_searches_genes(self):
        response = self.client.get(reverse("home"), {"search": "PRKN"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PRKN")

    def test_a_partial_identifier_narrows_the_grid_rather_than_guessing(self):
        """`1406` names no one antibody, so it must not redirect anywhere."""
        response = self.client.get(reverse("home"), {"search": "1406"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PRKN")


class BothSearchBoxesReachAntibodiesTests(TestCase):
    """Two boxes that answer differently give a reader no way to tell which is
    broken. They share `oga_search.js`; what a page can still get wrong is
    failing to hand it the antibody endpoint, which silently makes that box
    gene-only again.
    """
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()
        target = Target.objects.create(protein_name="Parkin", gene_name="PRKN")
        antibody = Antibody.objects.create(
            target=target, company=Company.objects.create(name="Proteintech"),
            catalogue_number="14060-1-AP", rrid="AB_2878005")
        PublicationImage.objects.create(
            antibody=antibody, application_type="WB", image="pubs/prkn.png")

    def test_the_homepage_box_is_wired_to_the_antibody_endpoint(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn("oga_search.js", html)
        self.assertIn(reverse("antibody_search"), html)

    def test_the_header_bar_box_is_wired_to_it_too(self):
        html = self.client.get(reverse("about")).content.decode()
        self.assertIn("oga_search.js", html)
        self.assertIn(reverse("antibody_search"), html)


class TheEmbedCardShowsOnlyPublishedWorkTests(TestCase):
    """`/embed/` is designed to be iframed into somebody else's page, and it read
    every `Antibody` row — so a target with nothing published rendered as an
    assessment with every application blank, on the most public surface there is.
    The gene pages 404 on exactly this; the card was the door left open.
    """
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()
        company = Company.objects.create(name="Abcam")
        target = Target.objects.create(protein_name="Parkin", gene_name="PRKN")
        self.published = Antibody.objects.create(
            target=target, company=company,
            catalogue_number="ab77924", rrid="AB_1566102")
        PublicationImage.objects.create(
            antibody=self.published, application_type="WB",
            image="pubs/prkn.png")
        self.unpublished = Antibody.objects.create(
            target=target, company=company,
            catalogue_number="ab-not-done", rrid="AB_7777777")

    def test_a_published_antibody_still_renders(self):
        response = self.client.get(reverse("embed_antibody_card"),
                                   {"catalogue": "ab77924"})
        self.assertContains(response, "ab77924")

    def test_an_antibody_with_nothing_published_is_not_served(self):
        for params in ({"catalogue": "ab-not-done"}, {"rrid": "AB_7777777"}):
            with self.subTest(params=params):
                response = self.client.get(
                    reverse("embed_antibody_card"), params)
                self.assertContains(response, "not found")
