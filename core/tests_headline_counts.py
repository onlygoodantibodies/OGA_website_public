"""One dataset, one set of numbers — the parts must add up to the whole.

"159 genes · 1,645 antibodies · 4,321 tests" is the first thing the site says,
and until now only the gene third of it had a single definition. The other two
were written out by hand wherever they were needed, four ways:

* **distinct catalogue numbers** — the home page headline, About, the impact page;
* **antibody records** — the API's `/status/` manifest, `/gene-detail/`, the MCP
  connector, the August 2026 availability check;
* **every antibody on the gene, published or not** — the home page's gene cards
  and the API's `/genes/` feed. On live that is 1,920 against a headline of
  1,645: 88 of the 159 genes overstated, SOD1 reading 29 on its card and 9 on
  its own page;
* **distinct RRIDs** — the browser-extension index, deliberately (a record with
  no RRID cannot be matched on one), which is why its 1,611 is not a fourth
  answer to the same question and is not pinned here.

The first three are now one reader, `pipeline.public`. What these pin is the
property that makes the harmonisation worth anything and that no single-surface
test can see: **a reader who adds up the parts gets the whole.** Each surface
was individually self-consistent and would pass its own suite; the defect only
exists between them, which is why it survived.
"""
from django.test import TestCase
from django.urls import reverse

from core.models import APIConsumer
from pipeline.models import Antibody, Company, PublicationImage, Target
from pipeline.public import headline_counts, published_antibodies


class HeadlineCountsTestCase(TestCase):
    """A dataset holding every shape that used to make two surfaces disagree.

    ALPHA carries all four at once: an antibody with two figures (a join that
    double-counts if nothing is distinct), an antibody with none (public gene,
    private row — the card bug), a discontinued one (hidden by default on the
    gene page), and a plain one. BETA is the simple case. DARK is a target with
    an antibody and no figure, so it is not a public gene at all.
    """

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.consumer = APIConsumer.objects.create(
            name="Proteintech", consumer_type="manufacturer", tier="full")
        company = Company.objects.create(name="Proteintech")

        def antibody(target, catalogue, images=(), out_of_market=False):
            ab = Antibody.objects.create(
                target=target, company=company, catalogue_number=catalogue,
                out_of_market=out_of_market)
            for app in images:
                PublicationImage.objects.create(
                    antibody=ab, application_type=app,
                    image=f"pubs/{catalogue}_{app}.png")
            return ab

        cls.alpha = Target.objects.create(protein_name="P1", gene_name="ALPHA")
        antibody(cls.alpha, "A-1", images=("WB", "IP"))   # two figures, one antibody
        antibody(cls.alpha, "A-2", images=("WB",))
        antibody(cls.alpha, "A-3")                        # logged, nothing published
        antibody(cls.alpha, "A-4", images=("WB",), out_of_market=True)

        cls.beta = Target.objects.create(protein_name="P2", gene_name="BETA")
        antibody(cls.beta, "B-1", images=("FC",))

        cls.dark = Target.objects.create(protein_name="P3", gene_name="DARK")
        antibody(cls.dark, "D-1")

        # ALPHA: 3 published of 4 records, 4 figures. BETA: 1 and 1.
        cls.expected = {"genes": 2, "antibodies": 4, "figures": 5}
        cls.per_gene = {"ALPHA": 3, "BETA": 1}

    def _api(self, name, **params):
        return self.client.get(reverse(f"api:{name}"), params,
                               HTTP_X_API_KEY=str(self.consumer.api_key)).json()


class TheHeadlineIsTheDatasetTests(HeadlineCountsTestCase):

    def test_the_three_numbers(self):
        counts = headline_counts()
        self.assertEqual(counts["gene_count"], self.expected["genes"])
        self.assertEqual(counts["antibody_count"], self.expected["antibodies"])
        self.assertEqual(counts["experiment_count"], self.expected["figures"])

    def test_a_second_figure_does_not_mint_a_second_antibody(self):
        """A-1 has two. Without `.distinct()` the headline gains one antibody
        every time somebody publishes another panel."""
        self.assertEqual(published_antibodies().count(),
                         self.expected["antibodies"])

    def test_an_unpublished_antibody_is_not_in_any_of_them(self):
        self.assertNotIn("A-3", list(
            published_antibodies().values_list("catalogue_number", flat=True)))


class TheCardsAddUpToTheHeadlineTests(HeadlineCountsTestCase):
    """The home page states a total and then draws the parts underneath it."""

    def test_each_card_shows_the_genes_published_antibodies(self):
        response = self.client.get(reverse("home"))
        drawn = {t.gene_name: t.published_antibody_count
                 for t in response.context["genes"]}
        self.assertEqual(drawn, self.per_gene)

    def test_the_cards_sum_to_the_number_above_them(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(
            sum(t.published_antibody_count for t in response.context["genes"]),
            response.context["antibody_count"])

    def test_the_card_count_costs_no_query_per_gene(self):
        """Annotated, not `{{ gene.antibodies.count }}`. The old spelling was a
        COUNT per gene — 159 of them on live — as well as the wrong number.

        Pinned as "does not grow with the row count" rather than as a fixed
        number, the way `tests_timeouts.py` does it: the absolute figure is
        whatever the page happens to need and would make this a test about
        unrelated edits.
        """
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        def queries():
            with CaptureQueriesContext(connections["pipeline_db"]) as ctx:
                self.client.get(reverse("home"))
            return len(ctx)

        before = queries()
        company = Company.objects.get()
        for n in range(5):
            target = Target.objects.create(protein_name=f"X{n}",
                                           gene_name=f"EXTRA{n}")
            ab = Antibody.objects.create(target=target, company=company,
                                         catalogue_number=f"X-{n}")
            PublicationImage.objects.create(antibody=ab, application_type="WB",
                                            image=f"pubs/x{n}.png")
        self.assertEqual(queries(), before)


class TheGenePageAgreesWithTheHomePageTests(HeadlineCountsTestCase):

    def test_the_page_draws_what_its_card_promised(self):
        response = self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "ALPHA"}))
        self.assertEqual(response.context["published_total"],
                         self.per_gene["ALPHA"])

    def test_the_drawn_count_may_be_smaller_and_says_so(self):
        """The discontinued tick is a real distinction and is kept: A-4 is not
        drawn by default. What makes that honest is the number beside it."""
        response = self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "ALPHA"}))
        self.assertEqual(response.context["antibody_count"], 2)
        self.assertEqual(response.context["hidden_discontinued"], 1)

    def test_the_title_does_not_move_when_a_filter_does(self):
        """The <title> and meta description are read in a search result, with
        none of the page's own caveats next to them, so they carry the gene's
        published total rather than the reader's filtered view."""
        url = reverse("antibody_table", kwargs={"gene_name": "ALPHA"})
        plain = self.client.get(url).content.decode()
        filtered = self.client.get(url, {"host": "Rabbit"}).content.decode()
        title = "knockout-controlled results for 3 antibodies"
        self.assertIn(title, plain)
        self.assertIn(title, filtered)

    def test_the_jsonld_describes_the_gene_not_the_filters(self):
        """schema.org `Dataset` is the same claim to a machine, under a `url`
        that carries the query string — so two filters described two datasets."""
        import json

        url = reverse("antibody_table", kwargs={"gene_name": "ALPHA"})
        for params in ({}, {"discontinued": "show"}, {"host": "Rabbit"}):
            with self.subTest(params=params):
                doc = json.loads(
                    self.client.get(url, params).context["jsonld"])
                self.assertIn("3 commercially available ALPHA",
                              doc["description"])


class TheApiAgreesWithTheSiteTests(HeadlineCountsTestCase):
    """`/genes/` and `/status/` are one API and were 1,920 against 1,645."""

    def test_the_gene_feed_counts_published_antibodies(self):
        feed = {g["gene"]: g["antibody_count"]
                for g in self._api("genes_feed")["genes"]}
        self.assertEqual(feed, self.per_gene)

    def test_the_gene_feed_sums_to_its_own_status_manifest(self):
        feed = self._api("genes_feed")["genes"]
        self.assertEqual(sum(g["antibody_count"] for g in feed),
                         self._api("api_status")["total_antibodies"])
        self.assertEqual(sum(g["experiment_count"] for g in feed),
                         self._api("api_status")["total_experiments"])

    def test_the_status_manifest_is_the_homepage_headline(self):
        status = self._api("api_status")
        counts = headline_counts()
        self.assertEqual(status["total_genes"], counts["gene_count"])
        self.assertEqual(status["total_antibodies"], counts["antibody_count"])
        self.assertEqual(status["total_experiments"], counts["experiment_count"])

    def test_gene_detail_agrees_with_the_gene_feed(self):
        """Two endpoints, one gene: this pair was 29 against 11 for SOD1."""
        detail = self._api("gene_detail", gene="ALPHA")
        feed = {g["gene"]: g["antibody_count"]
                for g in self._api("genes_feed")["genes"]}
        self.assertEqual(detail["total_antibodies"], feed["ALPHA"])

    def test_recommendation_tallies_are_scoped_to_the_published_rows(self):
        """`recommendations_by_application` is counted over the same list, so a
        flag set on an antibody the feed does not publish cannot inflate it."""
        Antibody.objects.filter(catalogue_number="A-3").update(wb_recommended=True)
        feed = {g["gene"]: g["recommendations_by_application"]
                for g in self._api("genes_feed")["genes"]}
        self.assertEqual(feed["ALPHA"]["WB"], 0)


class TheOtherSurfacesUseTheSameReaderTests(HeadlineCountsTestCase):

    def test_about_shows_the_homepage_numbers(self):
        response = self.client.get(reverse("about"))
        self.assertEqual(response.context["gene_count"], self.expected["genes"])
        self.assertEqual(response.context["antibody_count"],
                         self.expected["antibodies"])

    def test_the_impact_page_dataset_block(self):
        from pipeline.services.impact import dataset
        self.assertEqual(dataset(), {"genes": self.expected["genes"],
                                     "antibodies": self.expected["antibodies"],
                                     "figures": self.expected["figures"]})

    def test_the_extension_index_says_what_it_could_not_carry(self):
        """1,611 against the site's 1,645 is a matching limit, not a smaller
        dataset — the index is keyed on RRID and 34 published records have
        none. Both the CHANGELOG and the roadmap quote the 1,611 as the size of
        the dataset, so the gap has to be in the artefact itself."""
        from core.extension_index import build_index

        counts = build_index()["counts"]
        self.assertEqual(
            counts["antibodies"] + counts["antibodies_without_rrid"],
            headline_counts()["antibody_count"])

    def test_the_mcp_connector_counts_the_same_set(self):
        """Server A serialises from the public API's own code, so its published
        set must be the site's — not a hand copy of the same filter."""
        from mcp_servers.common.portal import _published_antibodies
        self.assertEqual(_published_antibodies().count(),
                         self.expected["antibodies"])
