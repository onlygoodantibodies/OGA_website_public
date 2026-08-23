"""A Target is not a public gene.

`/antibodies/GAPDH/` served a page reading "characterisation data for 0 GAPDH
antibodies" because the gene page keyed off *any* Target row, and the browser
extension shipped every Target as a gene it has data on. GAPDH is the standard
loading control — a target the lab has never characterised — so the extension
badged it amber and pointed at an empty page.

These lock the shared rule in `pipeline.public`: a public gene is a named gene
with at least one antibody carrying a published figure. That matters far more now
the target board imports hundreds of not-yet-started targets.
"""
from django.test import TestCase
from django.urls import reverse

from pipeline.models import Antibody, Company, PublicationImage, Target
from pipeline.public import public_gene_names, public_targets, unpublished_targets

DB = "pipeline_db"


class PublicGeneTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        company = Company.objects.create(name="Proteintech")
        # Characterised: an antibody with a published figure.
        self.done = Target.objects.create(protein_name="Synuclein", gene_name="SNCA")
        ab = Antibody.objects.create(
            target=self.done, company=company, catalogue_number="10842-1-AP")
        PublicationImage.objects.create(
            antibody=ab, application_type="WB", image="pubs/snca_wb.png")

        # A loading control: a real target, never characterised. This is GAPDH.
        self.bare = Target.objects.create(protein_name="GAPDH", gene_name="GAPDH")

        # Has an antibody logged, but nothing published — still not public.
        self.logged = Target.objects.create(protein_name="Tau", gene_name="MAPT")
        Antibody.objects.create(
            target=self.logged, company=company, catalogue_number="A00012")

    # --- the rule itself ---

    def test_public_set_needs_a_published_figure(self):
        self.assertEqual(public_gene_names(), ["SNCA"])

    def test_an_antibody_alone_is_not_enough(self):
        self.assertNotIn("MAPT", public_gene_names())

    def test_unpublished_is_the_exact_complement(self):
        self.assertEqual(
            sorted(unpublished_targets().values_list("gene_name", flat=True)),
            ["GAPDH", "MAPT"])

    def test_public_targets_yields_one_row_per_target(self):
        """Two published figures must not duplicate the gene in counts or lists."""
        ab = self.done.antibodies.first()
        PublicationImage.objects.create(
            antibody=ab, application_type="IP", image="pubs/snca_ip.png")
        self.assertEqual(public_targets().count(), 1)
        self.assertEqual(public_gene_names(), ["SNCA"])

    # --- the gene page ---

    def test_characterised_gene_page_renders(self):
        response = self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "SNCA"}))
        self.assertEqual(response.status_code, 200)

    def test_gene_page_404s_when_there_is_nothing_to_show(self):
        for gene in ("GAPDH", "MAPT"):
            with self.subTest(gene=gene):
                response = self.client.get(
                    reverse("antibody_table", kwargs={"gene_name": gene}))
                self.assertEqual(response.status_code, 404)

    def test_unknown_gene_still_404s(self):
        response = self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "NOTAGENE"}))
        self.assertEqual(response.status_code, 404)

    # --- the browser extension ---

    def test_extension_index_claims_only_public_genes(self):
        from core.extension_index import build_index
        index = build_index()
        self.assertEqual(index["genes"], ["SNCA"])
        self.assertNotIn("GAPDH", index["genes"])
        self.assertEqual(index["counts"]["genes"], 1)

    def test_every_gene_the_extension_claims_has_a_live_page(self):
        """The guarantee that actually matters: no amber badge links to a 404."""
        from core.extension_index import build_index
        for gene in build_index()["genes"]:
            with self.subTest(gene=gene):
                response = self.client.get(
                    reverse("antibody_table", kwargs={"gene_name": gene}))
                self.assertEqual(response.status_code, 200)

    # --- the counters agree with each other ---

    def test_homepage_count_matches_the_public_set(self):
        from core.views import get_live_stats
        self.assertEqual(get_live_stats()["gene_count"], len(public_gene_names()))

    def test_homepage_does_not_list_uncharacterised_genes(self):
        response = self.client.get(reverse("home"))
        listed = [t.gene_name for t in response.context["genes"]]
        self.assertEqual(listed, ["SNCA"])


class SameSetAsBeforeTests(TestCase):
    """The refactor must not change *which* genes are live — only where the rule
    is written down.

    A gene moves through many pipeline stages and only goes live when the
    publication images land (via the cropper today, by other routes historically).
    Every earlier stage — antibodies logged, sessions run, even a recommendation
    set — must still count as not-live. The fixture below is shaped like the real
    dataset: genes that are live, genes that are live but have no recommendations
    (so they show on the site but not through the recommendations filter), and
    genes at earlier stages.
    """
    databases = {"default", "pipeline_db", "academy_db"}

    # The definition as it stood before pipeline/public.py existed. Kept literal
    # so a future change to the shared helper has to prove itself against it.
    @staticmethod
    def _old_definition():
        return (Target.objects
                .filter(gene_name__isnull=False,
                        antibodies__publication_images__isnull=False)
                .exclude(gene_name="")
                .distinct())

    def setUp(self):
        self.company = Company.objects.create(name="Abcam")

    def _antibody(self, target, cat, *, images=(), recommended=()):
        ab = Antibody.objects.create(
            target=target, company=self.company, catalogue_number=cat,
            **{f"{a.lower()}_recommended": True for a in recommended})
        for app in images:
            PublicationImage.objects.create(
                antibody=ab, application_type=app,
                image=f"publication_images/2026/{cat}_{app}.png")
        return ab

    def _seed(self):
        # --- live ---
        live = Target.objects.create(protein_name="P1", gene_name="LIVE1")
        self._antibody(live, "A1", images=["WB"], recommended=["wb"])

        # Live, but nothing recommended — on the site, absent from the MCP's
        # only_with_recommendations view. This is the real "4 of 159" case.
        norec = Target.objects.create(protein_name="P2", gene_name="LIVENOREC")
        self._antibody(norec, "A2", images=["WB", "IP", "ICC-IF"])

        # Live via two separate antibodies — must be counted once, not twice.
        two = Target.objects.create(protein_name="P3", gene_name="LIVETWOAB")
        self._antibody(two, "A3", images=["WB"])
        self._antibody(two, "A4", images=["FC"])

        # Live, where the recommended antibody is a *different* one from the
        # imaged antibody — matches how the old cross-join behaved.
        split = Target.objects.create(protein_name="P4", gene_name="LIVESPLIT")
        self._antibody(split, "A5", images=["WB"])
        self._antibody(split, "A6", recommended=["ip"])

        # --- earlier stages, all not live ---
        Target.objects.create(protein_name="P5", gene_name="STAGEBARE")
        logged = Target.objects.create(protein_name="P6", gene_name="STAGELOGGED")
        self._antibody(logged, "A7")
        flagged = Target.objects.create(protein_name="P7", gene_name="STAGEFLAGGED")
        self._antibody(flagged, "A8", recommended=["wb"])   # flagged, no image yet
        return {"live": ["LIVE1", "LIVENOREC", "LIVESPLIT", "LIVETWOAB"]}

    def test_new_definition_matches_the_old_one_exactly(self):
        expected = self._seed()["live"]
        old = sorted(self._old_definition().values_list("gene_name", flat=True))
        new = public_gene_names()
        self.assertEqual(new, old)
        self.assertEqual(new, expected)

    def test_counts_agree_and_do_not_double_count(self):
        self._seed()
        self.assertEqual(public_targets().count(), self._old_definition().count())
        self.assertEqual(public_targets().count(), 4)

    def test_a_recommendation_without_an_image_is_not_live(self):
        """Recommendation flags are set during the pipeline; images make it live."""
        self._seed()
        self.assertNotIn("STAGEFLAGGED", public_gene_names())
        response = self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "STAGEFLAGGED"}))
        self.assertEqual(response.status_code, 404)

    def test_live_gene_with_no_recommendations_is_still_on_the_site(self):
        """It must show on the site and in the extension even though the MCP's
        recommendations view skips it — losing these would hide real data."""
        self._seed()
        self.assertIn("LIVENOREC", public_gene_names())
        response = self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "LIVENOREC"}))
        self.assertEqual(response.status_code, 200)
        from core.extension_index import build_index
        self.assertIn("LIVENOREC", build_index()["genes"])

    def test_recommendations_filter_is_a_subset_of_live(self):
        """MCP list_targets(only_with_recommendations=True) narrows the live set,
        it does not reach outside it."""
        expected = self._seed()["live"]
        from mcp_servers.common import portal
        all_public = {r["gene_name"] for r in portal.list_targets()}
        recommended = {r["gene_name"]
                       for r in portal.list_targets(only_with_recommendations=True)}
        self.assertEqual(sorted(all_public), expected)
        self.assertTrue(recommended < all_public)
        self.assertEqual(sorted(recommended), ["LIVE1", "LIVESPLIT"])
        # The live-but-unrecommended genes are exactly the difference.
        self.assertEqual(sorted(all_public - recommended),
                         ["LIVENOREC", "LIVETWOAB"])


class TheVerdictsAreInThePageTests(TestCase):
    """The recommendations are in the HTML the server sends.

    They used to be fetched by page JavaScript from a referer-gated endpoint, so
    the one field this site exists to publish was the only one absent from the
    document: invisible to a search engine, to a crawler, to an assistant, and
    to anybody with JS off. Rendering them server-side is easy to undo by
    accident and **silent when it happens** — the page still returns 200, still
    draws every antibody and every figure, and simply says nothing about which
    ones passed. That is what these pin.

    The JSON-LD is checked by parsing it rather than by matching a string: a
    block that does not parse is worse than no block, because a consumer reads
    the failure as the page having no structured data at all.
    """
    databases = {"default", "pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="Proteintech")
        cls.target = Target.objects.create(protein_name="Synuclein", gene_name="SNCA")
        # Recommended for WB, and tested-but-not-recommended for IP. The second
        # one only reads as a verdict because the gene is curated at all — the
        # distinction core/recommendations.py exists for.
        cls.ab = Antibody.objects.create(
            target=cls.target, company=company, catalogue_number="10842-1-AP",
            wb_recommended=True)
        for app in ("WB", "IP"):
            PublicationImage.objects.create(
                antibody=cls.ab, application_type=app,
                image=f"pubs/snca_{app}.png")

    def _page(self):
        return self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "SNCA"}))

    def test_a_recommendation_is_in_the_html(self):
        body = self._page().content.decode()
        self.assertIn("Recommended Applications:", body)
        self.assertIn("Western Blot", body)

    def test_the_recommended_cell_is_marked_without_javascript(self):
        body = self._page().content.decode()
        # The green wash, applied by the server. Its absence is what made the
        # verdicts invisible; `data-app="wb"` alone is not evidence of a verdict.
        self.assertIn('class="experiment-box recommended" data-app="wb"', body)
        # IP was tested and not recommended, so its cell is not marked.
        self.assertIn('class="experiment-box" data-app="ip"', body)

    def test_the_filter_can_still_find_the_row(self):
        # The client-side application filter reads this off the row now that
        # nothing is fetched. A row whose verdicts render but whose attribute is
        # missing filters to nothing, with no error anywhere.
        self.assertContains(self._page(), 'data-recommended="wb"')

    def test_the_page_carries_parseable_jsonld(self):
        import json as _json
        import re

        body = self._page().content.decode()
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
        self.assertEqual(len(blocks), 1, "expected exactly one JSON-LD block")
        doc = _json.loads(blocks[0])
        self.assertEqual(doc["@type"], "Dataset")
        self.assertEqual(doc["license"],
                         "https://creativecommons.org/licenses/by/4.0/")
        self.assertIn("SNCA", doc["keywords"])

    def test_the_licence_is_on_the_page_as_well_as_in_the_markup(self):
        # The reuser who needs it most is the one copying a figure into a talk,
        # and they are not reading the <head>.
        self.assertContains(self._page(),
                            "https://creativecommons.org/licenses/by/4.0/")

    def test_the_recommendations_endpoint_answers_without_a_referer(self):
        # The gate is gone: the page publishes exactly this, so refusing a
        # client that does not send `Referer` only ever cost the well-behaved.
        response = self.client.get(reverse(
            "gene_recommendations", kwargs={"gene_name": "SNCA"}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["recommendations"][str(self.ab.id)]["wb"])
