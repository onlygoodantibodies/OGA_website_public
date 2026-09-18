"""The data feed's guards, and the several that were not running.

Every failure pinned here is silent. This API answers a machine: nothing turns
red, nobody reads the reply, and a guard that has stopped guarding looks exactly
like one that has simply never had to fire. The defects below were all found by
reading the code rather than by anything going wrong on a screen.

  * **The throttle was bypassed by the front door.** ``antibodies_feed`` skipped
    its only rate check whenever ``?preview=true`` — and ``?preview=true`` is
    what the portal's own front end asks for on connect, so the one guarded path
    was the one nobody called.
  * **The clock was also the cursor.** ``APIConsumer.last_queried_at`` is the
    delta cursor *and* was the rate-limit timer, so an ordinary read consumed it:
    a client that failed while parsing lost that delta permanently and got 429
    for the rest of the hour if it retried. ``genes_feed`` read the same field
    and never set it, so asking for antibodies and then genes returned 429 for
    the genes while the reverse order worked.
  * **A refusal that does not say when to come back** is a refusal a machine
    client can only answer by guessing, so it guesses wrong in whichever
    direction costs more.
  * **The feeds queried per gene.** One query per gene for "has this gene been
    curated", another per gene for its report link, and ``.count()`` on a
    prefetched manager, which ignores the prefetch. All of it invisible until the
    dataset is big, and the dataset only gets bigger.
  * **An uncurated gene was reported as a failed test.** ``verdict`` must answer
    ``not_tested`` for an antibody with a published figure on a gene nobody has
    curated yet — the normal state of a gene mid-pipeline. Answering
    ``not_recommended`` there tells a manufacturer their named product failed
    knockout-controlled testing that was never run on it.

``LocMemCache`` is the throttle's backing store and it lives for the life of the
process, so the counters leak from test to test. Every class here clears it in
``setUp``, and the two classes that deliberately hammer the endpoint are kept
apart from the rest.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest import mock
from urllib.parse import urlencode

from django.core.cache import cache
from django.db import connections
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pipeline.models import (
    Antibody, Company, PublicationImage, Report, Target,
)
from pipeline.public import public_targets

from core import api_throttle
from core.models import APIConsumer
from core.recommendations import (
    NOT_RECOMMENDED, NOT_TESTED, RECOMMENDED, curated_gene_ids,
    recommendation,
)

# The exact strings a client reads off a reply to pace itself. Spelled out
# rather than derived from ``api_throttle``, because these are the published
# contract: a rename is a break for every consumer, not an implementation detail.
BUDGET_HEADERS = (
    'X-RateLimit-Limit-Burst',
    'X-RateLimit-Remaining-Burst',
    'X-RateLimit-Limit-Sustained',
    'X-RateLimit-Remaining-Sustained',
)


class APIFeedTestCase(TestCase):
    """Fixture and helpers shared by everything below.

    Two published genes: SNCA has been curated (something on it is recommended),
    STMN2 has published figures and no recommendations at all — which is what a
    gene looks like between the cropper and the recommendations pass.
    """

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.consumer = APIConsumer.objects.create(
            name="Proteintech", consumer_type="manufacturer", tier="full")
        cls.company = Company.objects.create(name="Proteintech")

        cls.snca = Target.objects.create(
            protein_name="Alpha-synuclein", gene_name="SNCA")
        cls.stmn2 = Target.objects.create(
            protein_name="Stathmin-2", gene_name="STMN2")

        # Curated gene: one recommended, one assessed and not recommended.
        cls.recommended_ab = cls._antibody(
            cls.snca, "10842-1-AP", image="WB", wb_recommended=True)
        cls.not_recommended_ab = cls._antibody(cls.snca, "sc-12767", image="WB")
        # Uncurated gene: a published figure, no flags anywhere on the gene.
        cls.not_tested_ab = cls._antibody(cls.stmn2, "10586-1-AP", image="WB")
        # No figure — never in the feed, so it is what `matched` must exclude.
        cls.unpublished_ab = cls._antibody(cls.stmn2, "A-0000")

        cls.published_antibodies = 3
        cls.published_genes = 2

    @classmethod
    def _antibody(cls, target, catalogue, image=None, **flags):
        antibody = Antibody.objects.create(
            target=target, company=cls.company,
            catalogue_number=catalogue, **flags)
        if image:
            PublicationImage.objects.create(
                antibody=antibody, application_type=image,
                image=f"publication_images/2026/{catalogue}_{image}.png")
        return antibody

    def setUp(self):
        # LocMemCache outlives the test, and the throttle counts live in it.
        cache.clear()

    # --- helpers -------------------------------------------------------

    def _get(self, name, consumer=None, **params):
        url = reverse(name)
        if params:
            url = f"{url}?{urlencode(params)}"
        return self.client.get(
            url, HTTP_X_API_KEY=str((consumer or self.consumer).api_key))

    def _cursor(self):
        return APIConsumer.objects.get(pk=self.consumer.pk).last_queried_at

    def _hammer(self, name, **params):
        """Request until refused; returns the 429.

        The cap allows for the fixed window rolling over mid-loop, which resets
        the count and costs one more full window of requests. Two rollovers
        would take a minute of wall clock, which this loop does not have.
        """
        cap = api_throttle.BURST_LIMIT * 2 + 5
        for _ in range(cap):
            response = self._get(name, **params)
            if response.status_code == 429:
                return response
        self.fail(f"{cap} requests to {name} and never a 429 — the burst limit "
                  f"of {api_throttle.BURST_LIMIT} is not being applied")


class ThePreviewPathIsThrottledTests(APIFeedTestCase):
    """``?preview=true`` skipped the only rate check there was.

    It is also the exact request the portal's front end makes on every connect,
    so in practice nothing was throttled at all: a runaway script asking for
    preview could hold PostgreSQL open all day and no counter anywhere moved.
    """

    def test_preview_is_counted_and_eventually_refused(self):
        """A preview request used to cost nothing and be refused by nothing, so
        the burst ceiling could be passed forever without a single 429."""
        first = self._get("api:antibodies_feed", preview="true")
        self.assertEqual(first.status_code, 200)
        # Confirm we are on the preview path and not accidentally testing the
        # ordinary feed, which was throttled all along.
        self.assertIs(first.json()["preview"], True)

        refused = self._hammer("api:antibodies_feed", preview="true")
        self.assertEqual(refused.status_code, 429)
        self.assertIn("Rate limit exceeded", refused.json()["error"])


class ARefusalSaysWhenToComeBackTests(APIFeedTestCase):
    """A 429 with no ``Retry-After`` leaves a machine client guessing.

    There are two refusals on this endpoint and they are different things: the
    throttle (abuse) and the one-per-hour delta gate (the contract of the
    incremental feed). Both must answer with a header *and* a JSON key, because
    a client reads one or the other and neither should have to parse "try again
    in 42 minutes" out of English.
    """

    def _assert_says_when(self, response):
        self.assertEqual(response.status_code, 429)
        seconds = response.json()["retry_after_seconds"]
        self.assertIsInstance(seconds, int)
        # Not asserted as > 0: a fixed window can be a fraction of a second from
        # rolling over, and 0 is the honest answer then.
        self.assertGreaterEqual(seconds, 0)
        self.assertTrue(response.has_header("Retry-After"))
        self.assertEqual(response["Retry-After"], str(seconds))

    def test_both_refusals_say_when_to_come_back(self):
        """The wait was stated only inside an English sentence, so a client had
        to parse prose to find it — and most of them did not, and simply
        hammered until something changed."""
        with self.subTest("the one-per-hour delta gate"):
            APIConsumer.objects.filter(pk=self.consumer.pk).update(
                last_queried_at=timezone.now())
            gated = self._get("api:antibodies_feed")
            self._assert_says_when(gated)
            # An hour's gate, so this one really is a wait, not a rounding edge.
            self.assertGreater(gated.json()["retry_after_seconds"], 0)
            self.assertLessEqual(gated.json()["retry_after_seconds"], 3600)

        with self.subTest("the throttle"):
            self._assert_says_when(
                self._hammer("api:antibodies_feed", preview="true"))


class EveryReplyCarriesTheBudgetTests(APIFeedTestCase):
    """A reply that does not say what is left cannot be paced against.

    The headers used to be written at each ``return``, and the exits easiest to
    forget were the error paths — which are the replies that need them most,
    because a 400 still spent a request. They come from a decorator now, so a
    refused reply carries the same budget a served one does.
    """

    def test_a_400_carries_the_same_budget_headers_as_a_200(self):
        """An error reply left out the budget it had just spent, so a client
        whose request was malformed could not tell how close to the ceiling its
        retries were taking it."""
        served = self._get("api:antibodies_feed", preview="true")
        self.assertEqual(served.status_code, 200)

        refused = self._get("api:antibodies_feed", preview="true", limit="abc")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("limit", refused.json()["error"])

        for response, what in ((served, "200"), (refused, "400")):
            for header in BUDGET_HEADERS:
                with self.subTest(reply=what, header=header):
                    self.assertTrue(
                        response.has_header(header),
                        f"the {what} reply spent a request and did not say so")

        # The budget on the error reply reflects a request having been spent.
        # The exact decrement is not asserted: the window can roll over between
        # the two calls, which resets the count and is not a defect.
        self.assertLess(int(refused["X-RateLimit-Remaining-Burst"]),
                        api_throttle.BURST_LIMIT)


class TheGenesFeedDoesNotReadTheAntibodyCursorTests(APIFeedTestCase):
    """Two endpoints disagreed about one field, and the order of your calls
    decided whether you got data.

    ``antibodies_feed`` *wrote* ``last_queried_at`` and ``genes_feed`` *checked*
    it without ever writing it, so antibodies-then-genes inside the hour returned
    429 for the genes while genes-then-antibodies worked. Nothing on either
    endpoint explained that, and a client that happened to poll in the wrong
    order looked to its owner like an API that was down.
    """

    def test_genes_answers_immediately_after_antibodies(self):
        """This exact pair of calls, in this order, returned 429 for the genes:
        the antibody feed had moved a cursor the gene feed refused on but never
        set itself."""
        antibodies = self._get("api:antibodies_feed")
        self.assertEqual(antibodies.status_code, 200)
        # The call has to have moved the cursor, or this proves nothing.
        self.assertIsNotNone(self._cursor())

        genes = self._get("api:genes_feed")
        self.assertEqual(genes.status_code, 200)
        self.assertIn("genes", genes.json())

    def test_the_gene_catalogue_is_not_silently_emptied_by_the_cursor(self):
        """An endpoint whose first line says "returns all genes" returned none.

        ``_get_since`` falls back to ``last_queried_at`` when no ``?since=`` is
        given, so once a consumer had ever called the antibody feed, the gene
        catalogue answered ``{"count": 0, "genes": []}`` — a 200, no error, and
        nothing to distinguish "nothing new" from "this endpoint is broken".
        The catalogue is the default now and a delta is asked for explicitly.
        """
        self._get("api:antibodies_feed")           # moves the cursor to now
        self.assertIsNotNone(self._cursor())

        catalogue = self._get("api:genes_feed").json()
        self.assertEqual(catalogue["mode"], "full")
        self.assertIsNone(catalogue["since"])
        self.assertGreater(
            catalogue["count"], 0,
            "The gene catalogue came back empty because a cursor nobody asked "
            "about was applied to it.")

    def test_a_delta_is_still_available_by_asking(self):
        """Losing the accidental default must not lose the deliberate feature."""
        future = (timezone.now() + timedelta(days=1)).isoformat()
        delta = self._get("api:genes_feed", since=future).json()
        self.assertEqual(delta["mode"], "incremental")
        self.assertEqual(delta["count"], 0)


class TheCursorIsOnlySpentWhenAskedTests(APIFeedTestCase):
    """A read a computer makes has to be repeatable.

    Every read advanced the cursor, so a client that failed while parsing the
    reply lost that delta permanently — the rows were not resent, and a retry
    inside the hour was refused. ``?advance_cursor=false`` takes the same rows
    and leaves the cursor where it is.
    """

    def test_advance_cursor_false_returns_the_rows_and_spends_nothing(self):
        """There was no way to read the delta without consuming it, so a client
        that crashed halfway through the reply never saw those rows again — and
        the default still has to advance, because integrations depend on it."""
        first = self._get("api:antibodies_feed", advance_cursor="false")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["count"], self.published_antibodies)
        self.assertIs(first.json()["cursor_advanced"], False)
        self.assertIsNone(self._cursor())

        # The point of not spending it: ask again and get the same rows back.
        second = self._get("api:antibodies_feed")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["count"], first.json()["count"])
        self.assertIs(second.json()["cursor_advanced"], True)
        self.assertIsNotNone(self._cursor())


class PreviewNeverSpendsTheCursorTests(APIFeedTestCase):
    """Preview is "show me everything", not "give me the delta".

    It is what the portal asks for on connect, so a preview that moved the
    cursor would mark every antibody as reviewed the moment somebody opened the
    page — and the next real delta would come back empty.
    """

    def test_preview_leaves_the_cursor_where_it_was(self):
        """Preview asks for everything and reviews nothing, so moving the cursor
        here would silently mark the whole set as seen."""
        was = timezone.now() - timedelta(days=2)
        APIConsumer.objects.filter(pk=self.consumer.pk).update(last_queried_at=was)

        response = self._get("api:antibodies_feed", preview="true")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["preview"], True)
        self.assertIs(body["cursor_advanced"], False)
        # Preview is the whole set, so it reports no `since` at all.
        self.assertIsNone(body["since"])
        self.assertEqual(body["count"], self.published_antibodies)
        self.assertEqual(self._cursor(), was)


class APageSaysItIsAPageTests(APIFeedTestCase):
    """A truncated feed that does not say it is truncated reads as data loss.

    These consumers diff the reply against what they hold, so a URL missing from
    a silently limited page looks like a product that has been withdrawn. A page
    says it is one, and carries the size of the whole set beside it.

    Both calls pass ``advance_cursor=false`` so the first does not put the
    second behind the one-per-hour delta gate — that gate is tested elsewhere,
    and it is not what this is about.
    """

    def test_a_limited_reply_is_marked_incomplete_and_counts_the_whole_set(self):
        """A limited reply looked exactly like a complete one, so the rows it
        left out read as antibodies that had been withdrawn."""
        response = self._get(
            "api:antibodies_feed", advance_cursor="false", limit="1")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body["count"], 1)
        self.assertIs(body["complete"], False)
        self.assertEqual(body["matched"], self.published_antibodies)
        self.assertEqual(body["truncated"]["limit"], 1)
        self.assertEqual(body["truncated"]["offset"], 0)
        self.assertIn("warning", body["truncated"])

    def test_an_unpaged_reply_says_it_is_the_whole_set(self):
        """The other half: an unpaged reply must say it is complete, or every
        consumer has to treat every reply as possibly partial."""
        body = self._get("api:antibodies_feed", advance_cursor="false").json()
        self.assertIs(body["complete"], True)
        self.assertNotIn("truncated", body)
        self.assertEqual(body["count"], self.published_antibodies)
        self.assertEqual(body["matched"], self.published_antibodies)


class TheFeedsDoNotQueryPerGeneTests(APIFeedTestCase):
    """Cost is pinned as "the query count does not grow with the row count".

    Three N+1s lived in these two endpoints, and none of them was visible on a
    handful of dev rows: ``_target_has_recommendations`` called once per gene,
    ``.count()`` on a prefetched related manager (which issues a fresh COUNT and
    ignores the prefetch), and a ``Gene.objects.get`` per gene for the legacy
    report link. The third is gone outright — the legacy ``core`` layer and the
    ``default`` database it lived in were retired on 23 Aug 2026 — so two
    databases are counted here: pipeline for the genes, academy for the
    consumer.
    """

    ALIASES = ("pipeline_db", "academy_db")

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A gene whose report link comes from a pipeline Report. There used to
        # be a second one here exercising the legacy fallback; that branch no
        # longer exists.
        Report.objects.create(
            target=cls.snca, zenodo_doi="https://doi.org/10.5281/zenodo.1",
            zenodo_date=date(2026, 1, 1))

    def _more_genes(self, n=5):
        for i in range(n):
            target = Target.objects.create(
                protein_name=f"Protein {i}", gene_name=f"EXTRA{i}")
            antibody = Antibody.objects.create(
                target=target, company=self.company,
                catalogue_number=f"EX-{i}")
            PublicationImage.objects.create(
                antibody=antibody, application_type="WB",
                image=f"publication_images/2026/EX-{i}_WB.png")
            Report.objects.create(
                target=target,
                zenodo_doi=f"https://doi.org/10.5281/zenodo.{100 + i}",
                zenodo_date=date(2026, 2, 1))

    def _counts(self, name, **params):
        # Warm up first, so the baseline is a *steady-state* request.
        #
        # The usage counter (core/api_usage.py) writes one row per consumer per
        # endpoint per day and increments it in place, so the day's first
        # request costs an UPDATE that matches nothing plus an INSERT, and every
        # request after it costs the UPDATE alone. Both are constant in the
        # number of genes, which is what this class exists to pin — but a
        # baseline taken from the very first call is measuring the insert, and
        # the comparison call is not, so the two disagree by one query for a
        # reason that has nothing to do with row counts. Take the baseline where
        # the endpoint actually lives.
        self._get(name, **params)

        contexts = {alias: CaptureQueriesContext(connections[alias])
                    for alias in self.ALIASES}
        with contexts["pipeline_db"], contexts["academy_db"]:
            response = self._get(name, **params)
        self.assertEqual(response.status_code, 200)
        return {alias: len(ctx) for alias, ctx in contexts.items()}

    def _same_counts(self, name, baseline, **params):
        with self.assertNumQueries(baseline["pipeline_db"], using="pipeline_db"), \
             self.assertNumQueries(baseline["academy_db"], using="academy_db"):
            response = self._get(name, **params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_genes_feed_queries_per_request_not_per_gene(self):
        """Three queries per gene — the curation check, the legacy report link
        and a ``.count()`` that ignored its own prefetch — so this endpoint got
        slower every time OGA published anything. The legacy one is retired;
        the other two are what this still guards."""
        baseline = self._counts("api:genes_feed")
        self._more_genes(5)
        body = self._same_counts("api:genes_feed", baseline)
        # The second reply really did carry the extra genes — a constant query
        # count is worth nothing if the work was not done.
        self.assertEqual(body["count"], self.published_genes + 5)

    def test_the_antibodies_feed_queries_per_request_not_per_gene(self):
        """``_target_has_recommendations`` ran once per gene in the page, which
        is one query per gene on the one endpoint everybody polls."""
        # Preview, so the first call does not move the cursor and leave the
        # second one with a delta of nothing to serialise.
        baseline = self._counts("api:antibodies_feed", preview="true")
        self._more_genes(5)
        body = self._same_counts(
            "api:antibodies_feed", baseline, preview="true")
        self.assertEqual(body["count"], self.published_antibodies + 5)


class AnUncuratedGeneIsNotAFailedTestTests(APIFeedTestCase):
    """``not_recommended`` is a finding; the absence of one is not.

    A published figure says an application was assessed — but only once somebody
    has actually set that gene's recommendations, which happens later, in one
    pass, on ``/pipeline/recommendations/``. Reading the figure alone turns every
    antibody on every uncurated gene into a documented failure the moment its
    first figure goes up, which is a false accusation about a named commercial
    product.
    """

    @staticmethod
    def _applications(antibody):
        return {image.application_type
                for image in antibody.publication_images.all()}

    def test_a_figure_on_an_uncurated_gene_reads_as_not_tested(self):
        """Without the gene gate this answers ``not_recommended``, which reports
        a product as having failed testing nobody has run on it."""
        curated = curated_gene_ids([self.snca.pk, self.stmn2.pk])
        self.assertNotIn(self.stmn2.pk, curated)

        self.assertEqual(
            recommendation(self.not_tested_ab, "WB",
                    self._applications(self.not_tested_ab),
                    self.stmn2.pk in curated),
            NOT_TESTED)

    def test_the_same_row_on_a_curated_gene_is_a_finding(self):
        """The gate has to let the real verdict through, or it is just a wall."""
        curated = curated_gene_ids([self.snca.pk, self.stmn2.pk])
        self.assertIn(self.snca.pk, curated)

        self.assertEqual(
            recommendation(self.not_recommended_ab, "WB",
                    self._applications(self.not_recommended_ab),
                    self.snca.pk in curated),
            NOT_RECOMMENDED)
        self.assertEqual(
            recommendation(self.recommended_ab, "WB",
                    self._applications(self.recommended_ab),
                    self.snca.pk in curated),
            RECOMMENDED)


class TheCompetitorViewIsForManufacturersTests(APIFeedTestCase):
    """The one endpoint restricted by who is asking, and it was on the wrong axis.

    ``/v1/gene-detail/`` lists every antibody against a gene grouped by supplier,
    with a per-supplier recommended-versus-total summary. That is commercially
    sensitive in a way nothing else here is: it tells whoever holds the key how
    each of their competitors' products fared.

    It was gated on ``tier >= intel`` and on nothing else — so a registry or any
    other non-manufacturer with a high tier received the full competitor
    breakdown, while the entitlement rule the other three endpoints applied
    (``consumer_type == 'manufacturer'``) was the one thing this endpoint never
    checked. Tier is gone; the gate is the consumer type, and the refusal names
    it so the reader can tell "not for your kind of account" from "broken".
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.registry = APIConsumer.objects.create(
            name="Antibody Registry", consumer_type="rrid", tier="full")

    def test_a_registry_is_refused_however_high_its_tier(self):
        refused = self._get(
            "api:gene_detail", consumer=self.registry, gene="SNCA")
        self.assertEqual(refused.status_code, 403)
        body = refused.json()
        self.assertEqual(body["your_consumer_type"], "rrid")
        self.assertIn("manufacturer", body["error"])

    def test_a_manufacturer_is_served_on_any_tier(self):
        """Tier was a monetisation gate and there is no monetisation."""
        for tier in ("free", "data", "intel", "full"):
            with self.subTest(tier=tier):
                APIConsumer.objects.filter(pk=self.consumer.pk).update(tier=tier)
                served = self._get("api:gene_detail", gene="SNCA")
                self.assertEqual(served.status_code, 200)
                self.assertEqual(served.json()["gene"], "SNCA")


class TheScreenThatSaidZeroRecommendedTests(APIFeedTestCase):
    """The reported defect, pinned as the request the portal actually makes.

    The portal showed "1645 antibodies · **0 recommended**" with every
    application badge grey, across all 159 genes. The data was fine — confirmed
    by the owner. ``_serialise_antibody`` took
    ``include_recs=(consumer.consumer_type == 'manufacturer')``, so a registry
    key received ``recommendations: {}`` for every row, and the portal, which
    reads neither ``consumer_type`` nor ``gene_has_recommendations``, had nothing
    to render but zero.

    That is the worst shape a bug can take here: a statement about the dataset,
    produced from a fact about the account, on a screen a manufacturer might
    reasonably read as "OGA rejected all of our products".

    The verdicts were never secret — the public gene pages serve the same four
    booleans to anyone, ungated (``core/views.py::gene_recommendations``) — so
    the rule protected nothing and broke the one screen that showed them.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.registry = APIConsumer.objects.create(
            name="Antibody Registry", consumer_type="rrid", tier="data")

    def _portal_feed(self, consumer):
        """Byte for byte the request `portal.html`'s connect() makes."""
        response = self._get(
            "api:antibodies_feed", consumer=consumer, preview="true")
        self.assertEqual(response.status_code, 200)
        return response.json()["antibodies"]

    def test_a_registry_key_receives_the_recommendations(self):
        rows = self._portal_feed(self.registry)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(
                row["recommendations"],
                f'{row["antibody_name"]} came back with no recommendations, '
                'which the portal renders as a grey badge indistinguishable '
                'from a failed test.')

    def test_the_portals_own_count_is_not_zero(self):
        """The assertion is the page's arithmetic, not a proxy for it.

        `portal.html` counts an antibody as recommended when any value in
        `recommendations` is exactly `true`. Asserting the key is present would
        pass on `{}`... which is precisely what shipped. So this runs the same
        expression the browser runs.
        """
        for consumer in (self.registry, self.consumer):
            with self.subTest(consumer=consumer.consumer_type):
                rows = self._portal_feed(consumer)
                recommended = [
                    row for row in rows
                    if any(value is True
                           for value in row["recommendations"].values())
                ]
                self.assertEqual(
                    len(recommended), 1,
                    "The header would read '0 recommended' for a "
                    f"{consumer.consumer_type} key over a dataset that has one.")

    def test_grey_is_no_longer_two_different_answers(self):
        """`recommendations` alone cannot separate these two, and `verdicts` must.

        One of these antibodies was assessed for WB and not recommended; the
        other is on a gene nobody has curated, so it has never been assessed at
        all. Both are `wb_recommended = False`. Telling a manufacturer the second
        one failed knockout-controlled testing is a claim about a named product
        that nothing in the database supports.
        """
        by_catalogue = {row["antibody_name"]: row
                        for row in self._portal_feed(self.registry)}

        assessed = by_catalogue[self.not_recommended_ab.catalogue_number]
        never = by_catalogue[self.not_tested_ab.catalogue_number]

        self.assertIs(assessed["recommendations"]["WB"], False)
        self.assertIs(never["recommendations"]["WB"], False)

        self.assertEqual(assessed["oga_recommendations"]["WB"], NOT_RECOMMENDED)
        self.assertEqual(never["oga_recommendations"]["WB"], NOT_TESTED)

    def test_a_registrys_filters_are_applied_rather_than_discarded(self):
        """Silently ignoring a filter is worse than refusing it.

        Both were forced off server-side for non-manufacturers while the portal
        went on drawing the controls — so "Recommended only" returned the whole
        dataset unfiltered and the application filter did nothing, with a 200
        and no message either way.
        """
        rows = self._get("api:antibodies_feed", consumer=self.registry,
                         preview="true", recommended_only="true").json()
        self.assertEqual(rows["count"], 1)
        self.assertEqual(rows["antibodies"][0]["antibody_name"],
                         self.recommended_ab.catalogue_number)

        by_app = self._get("api:antibodies_feed", consumer=self.registry,
                           preview="true", application="FC").json()
        self.assertEqual(
            by_app["count"], 0,
            "Nothing is recommended for FC, so an FC filter that returns rows "
            "is a filter the server threw away.")


class IssueReportingIsOpenToEveryConsumerTests(APIFeedTestCase):
    """Owner's decision: anybody may flag a problem with a record.

    It was `full` tier only. Somebody telling us a recommendation looks wrong,
    or that a product has been discontinued, is doing OGA a favour — and the
    dataset got worse the more that was withheld. It was also the last tier
    check in the API, so `_tier_at_least` and `TIER_ORDER` went with it.

    `APIConsumer.tier` is still a column and `/v1/status/` still reports it.
    Nothing reads it to decide anything, and this is what says so.
    """

    def _report(self, consumer):
        return self.client.post(
            reverse("api:report_issue"),
            data='{"catalogue": "10842-1-AP", "gene": "SNCA", '
                 '"issue_type": "discontinued", "details": "Delisted."}',
            content_type="application/json",
            HTTP_X_API_KEY=str(consumer.api_key))

    def test_every_tier_can_report(self):
        for tier in ("free", "data", "intel", "full"):
            with self.subTest(tier=tier):
                APIConsumer.objects.filter(pk=self.consumer.pk).update(tier=tier)
                response = self._report(self.consumer)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "sent")

    def test_a_registry_can_report_too(self):
        """The other axis. Only the competitor view asks who is calling."""
        registry = APIConsumer.objects.create(
            name="Antibody Registry", consumer_type="rrid", tier="free")
        self.assertEqual(self._report(registry).status_code, 200)

    def test_no_endpoint_reads_the_tier_to_decide_anything(self):
        """The cheapest guard against the next `tier ==` creeping back in.

        A tier check reintroduced anywhere here would be a product decision the
        owner has made the other way twice, and it would fail silently for
        exactly the consumers who are not testing it.
        """
        from pathlib import Path

        from django.conf import settings

        for name in ("core/api_views.py", "core/api_manifest.py"):
            source = (Path(settings.BASE_DIR) / name).read_text()
            # The word appears in comments and in the status payload; what must
            # not come back is a comparison that gates on it.
            for banned in ("_tier_at_least", "TIER_ORDER", "tier_level"):
                self.assertNotIn(
                    banned, source,
                    f"{name} has {banned} again — tiers decide nothing now.")


class TheApiOnlyPublishesPublicGenesTests(APIFeedTestCase):
    """A Target is an intention; a public gene has a figure behind it.

    `/v1/genes/` listed every named Target and `/v1/status/` counted them, so on
    live the API answered **582** where the homepage counter, the browser
    extension and the MCP connector all answered **159** — the three that ask
    `pipeline.public.public_targets()`. The 423 extra rows carried
    `antibody_count: 0` and were handed to manufacturers and registries: gene
    names for work that has not started, which is a list of what OGA intends to
    characterise next.

    That module's own docstring says these must not reach the site, the
    extension or the MCP dataset. The API was never added to the list, and it is
    the surface with paying-adjacent consumers on it.
    """

    def test_the_gene_list_excludes_a_target_with_no_figure(self):
        bare = Target.objects.create(protein_name="GAPDH", gene_name="GAPDH")
        Antibody.objects.create(
            target=bare, company=self.company, catalogue_number="A-0001")

        genes = [row["gene"] for row in self._get("api:genes_feed").json()["genes"]]
        self.assertNotIn(
            "GAPDH", genes,
            "A target with an antibody but no published figure is not public — "
            "it is the loading control the pipeline has never characterised.")
        self.assertCountEqual(genes, ["SNCA", "STMN2"])

    def test_total_genes_agrees_with_the_list_it_totals(self):
        """Two numbers on one API that disagree is the shape of every defect here."""
        Target.objects.create(protein_name="GAPDH", gene_name="GAPDH")

        total = self._get("api:api_status").json()["total_genes"]
        listed = self._get("api:genes_feed").json()["count"]
        self.assertEqual(total, listed)
        self.assertEqual(total, self.published_genes)

    def test_it_matches_the_shared_definition(self):
        """Derived from `pipeline.public`, not from a second copy of the rule."""
        Target.objects.create(protein_name="GAPDH", gene_name="GAPDH")

        self.assertEqual(self._get("api:api_status").json()["total_genes"],
                         public_targets().count())


class AnUnauthenticatedRequestIsCountedTooTests(APIFeedTestCase):
    """The one unmetered way into this API was the way in without a key.

    Authentication runs before throttling, and the per-consumer counters are
    keyed on a consumer a bad key does not have — so a request with the wrong
    key was refused and counted nowhere, for ever, as fast as it could be sent.

    Guessing a key is hopeless and never was the risk: they are UUID4s. The cost
    is that each attempt is one `APIConsumer` lookup against the SQLite database
    that also holds the site's logins, and nothing put a ceiling on the rate.
    Publishing a page that tells the world the API exists is what made it worth
    closing.
    """

    def _no_key(self):
        return self.client.get(reverse("api:api_status"))

    def _bad_key(self):
        return self.client.get(
            reverse("api:api_status"),
            HTTP_X_API_KEY="00000000-0000-4000-8000-000000000000")

    def test_a_wrong_key_is_eventually_refused_for_being_too_frequent(self):
        first = self._bad_key()
        self.assertEqual(first.status_code, 403)

        for _ in range(api_throttle.ANON_BURST_LIMIT * 2 + 5):
            response = self._bad_key()
            if response.status_code == 429:
                break
        else:
            self.fail("A wrong key can be retried without limit.")

        self.assertIn("without a valid API key", response.json()["error"])
        self.assertTrue(response["Retry-After"])

    def test_a_missing_key_is_counted_on_the_same_budget(self):
        """Sending no header at all must not be the cheaper way to do it."""
        for _ in range(api_throttle.ANON_BURST_LIMIT * 2 + 5):
            response = self._no_key()
            if response.status_code == 429:
                break
        else:
            self.fail("A request with no key can be repeated without limit.")

    def test_a_real_key_is_unaffected_by_a_neighbours_bad_ones(self):
        """The anonymous counter is keyed on the caller, not on the endpoint.

        If it leaked into the authenticated path, one script with a stale key
        would lock every legitimate consumer out of the API.
        """
        for _ in range(api_throttle.ANON_BURST_LIMIT + 5):
            self._bad_key()

        self.assertEqual(
            self._get("api:api_status").status_code, 200,
            "A valid key was refused because somebody else sent a bad one.")


class EveryResponseThatCarriesRecommendationsSaysWhatTheyCoverTests(
        APIFeedTestCase):
    """The caveat travels with the data, not only with the documentation.

    Owner's instruction, 12 Aug 2026, applied to the API. `SCOPE_NOTE` was on
    the OpenAPI contract, the bulk-archive manifest and `API.md` — all of them
    things a *human* reads once, while `api_views.py` imported the constant and
    never sent it. So a partner's nightly sync received `oga_recommendations`
    with nothing beside it saying what those values cover.

    It matters more than the page version because a machine caller never reads
    a page, and it will matter more again if the API ever goes keyless: a
    keyless caller is by definition one who had no conversation with OGA.

    On the **envelope**, deliberately. Per row it would be a caveat nobody
    reads (the reasoning in `recommendations.SCOPE_NOTE`) and, less obviously,
    it is what made this safe to add to a live API — the per-antibody and
    per-gene objects are untouched, so anything iterating `antibodies` sees no
    change at all.
    """

    #: The three endpoints that serialise a recommendation. `/status/`,
    #: `/portal-config/` and the reviewed-antibody endpoints carry none, so the
    #: sentence would be noise on them.
    CARRIES_RECOMMENDATIONS = (
        ('api:antibodies_feed', {}),
        ('api:genes_feed', {}),
        ('api:gene_detail', {'gene': 'SNCA'}),
    )

    def test_each_one_carries_the_scope_note(self):
        from core import recommendations as R

        for name, params in self.CARRIES_RECOMMENDATIONS:
            with self.subTest(endpoint=name):
                body = self._get(name, **params).json()
                self.assertEqual(body.get('recommendation_scope'),
                                 R.SCOPE_NOTE)

    def test_the_schema_names_the_field_on_each_of_them(self):
        """The anti-phantom rule, run the other way round.

        `tests_apimd_drift` fails a key the documentation promises and the API
        does not send. This is the benign direction — sent and undocumented —
        which breaks nobody and means nobody generating a client discovers the
        field exists.
        """
        from core.api_schema import document

        paths = document()['paths']
        for path in ('/antibodies/', '/genes/', '/gene-detail/'):
            with self.subTest(path=path):
                schema = (paths[path]['get']['responses']['200']
                          ['content']['application/json']['schema'])
                self.assertIn('recommendation_scope', schema['properties'])

    def test_it_is_not_called_scope(self):
        """`scope` already means something else on this API.

        `/api/v1/manifest/` uses `scope` for which *part of the dataset* a key
        covers — genes, supplier filter, whether recommendations are included.
        A second meaning of one word on one API is exactly the drift the
        manifest's own schema test exists to catch, so the name is the one
        `api_manifest.py` had already chosen for this string.
        """
        body = self._get('api:antibodies_feed').json()
        self.assertNotIn('scope', body)


class TheReadFeedsNeedNoKeyTests(APIFeedTestCase):
    """Keyless reads on `/antibodies/` and `/genes/` (owner, 12 Aug 2026).

    The key was never protecting the data — `core/views.py::gene_recommendations`
    serves the same recommendations with no gate and the gene pages draw them,
    so anyone who wanted the dataset could take it off the pages. What a key
    bought was metering and attribution, and both survive: it still works, still
    names the caller, still applies a supplier filter and still moves a cursor.

    Roadmap #60 is why this is safe rather than merely convenient. It closed the
    Cloudflare rate-limiting rule as **unnecessary**, not unavailable: Managed
    Rules and Bot Fight Mode are what hold, both free and automatic, and Bot
    Fight Mode was checked against the failure that would matter here — it
    challenges datacentre IPs, which is what a partner's nightly sync looks
    like — and answered 200 from an Azure address.

    What is pinned below is the traps, not the happy path. A keyless read that
    quietly wrote somebody's cursor, or a cached reply crossing between callers,
    would both be silent.
    """

    def _keyless(self, name, **params):
        url = reverse(name)
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"
        return self.client.get(url)

    def test_both_feeds_answer_without_a_key(self):
        for name in ('api:antibodies_feed', 'api:genes_feed'):
            with self.subTest(endpoint=name):
                self.assertEqual(self._keyless(name).status_code, 200)

    def test_a_keyless_reader_gets_the_published_set(self):
        body = self._keyless('api:antibodies_feed').json()
        self.assertEqual(body['count'], self.published_antibodies)
        self.assertIsNone(body['consumer'])

    def test_gene_detail_still_needs_a_key(self):
        """The owner's decision, 12 Aug: the competitor view is not opened.

        It groups a gene's antibodies by vendor with a per-supplier recommended
        tally, which is the one thing on this API that is not already on a page.
        """
        self.assertEqual(
            self._keyless('api:gene_detail', gene='SNCA').status_code, 401)

    def test_a_wrong_key_is_still_refused(self):
        """The trap that would be worst to get wrong.

        Falling back to anonymous access on a bad key would hand a manufacturer
        whose key had expired **every** supplier's rows instead of their own,
        with a 200 and nothing said. Absence of a key is allowed; a wrong one is
        not.
        """
        response = self.client.get(
            reverse('api:antibodies_feed'),
            HTTP_X_API_KEY='00000000-0000-4000-8000-000000000000')
        self.assertEqual(response.status_code, 403)

    def test_a_keyless_read_moves_nobody_s_cursor(self):
        """A keyless caller has no consumer, so it must not write to one."""
        before = self._cursor()
        self._keyless('api:antibodies_feed')
        self.assertEqual(self._cursor(), before)

    def test_a_keyless_read_is_not_delta_gated(self):
        """The hourly gate is the contract of a cursor nobody keyless has.

        A keyed consumer that has just read the feed is inside the gate; a
        keyless one is a different caller entirely and must not inherit it.
        """
        self._get('api:antibodies_feed')          # consumes the keyed cursor
        self.assertEqual(
            self._keyless('api:antibodies_feed').status_code, 200)

    def test_a_shared_cache_is_told_the_reply_depends_on_the_key(self):
        """Without `Vary`, an edge cache can serve one caller's rows to another.

        The bodies genuinely differ — a manufacturer's supplier filter, a demo
        account's gene list — so this is the header that stops the CDN turning
        an optimisation into a data leak. Asserted on both branches, because it
        is the keyed reply being cached as if public that would do the damage.

        Asked as membership, not equality: `Vary` is a list and anything that
        legitimately varies the bytes adds to it — `GZipMiddleware` appends
        `Accept-Encoding`, and a cache needs that name as much as this one. What
        must never happen is `X-API-Key` going missing, and that is what this
        asks. Equality would fail the next time something correct is added.
        """
        def vary(response):
            return {v.strip() for v in response['Vary'].split(',')}

        keyless = self._keyless('api:antibodies_feed')
        keyed = self._get('api:antibodies_feed')
        self.assertIn('X-API-Key', vary(keyless))
        self.assertIn('X-API-Key', vary(keyed))
        self.assertIn('public', keyless['Cache-Control'])
        self.assertIn('private', keyed['Cache-Control'])

    def test_a_keyed_reader_still_gets_everything_a_key_buys(self):
        """Opening the door must not quietly change what a key does."""
        body = self._get('api:antibodies_feed').json()
        self.assertEqual(body['consumer'], self.consumer.name)
        self.assertEqual(body['consumer_type'], 'manufacturer')

    def test_the_anonymous_ceiling_still_applies(self):
        """Open is not unmetered. `check_anonymous` is 30/minute per caller.

        **The clock is pinned, because the window is wall-clock.**
        `check_anonymous` counts into `int(now // BURST_WINDOW)`, so a loop of
        35 requests that crosses a minute boundary splits them between two
        windows and neither reaches 31 — the test then fails while the throttle
        is working perfectly. Any split leaving **5 to 30** requests in the
        first window does it, which is 26 of the 36 possible splits, and the
        loop crosses a boundary roughly as often as its own duration divided by
        sixty. It went red once on 5 Sep 2026, on a change that touched no part
        of this, and cost a merge.

        Freezing `time.time` removes the boundary rather than the coverage: the
        counter still has to reach 31 for a 429, and the cache is cleared first
        so an earlier test in the same process cannot lend this one its hits.
        """
        cache.clear()
        with mock.patch('core.api_throttle.time.time', return_value=1_757_000_000.0):
            for _ in range(api_throttle.ANON_BURST_LIMIT + 5):
                response = self._keyless('api:genes_feed')
                if response.status_code == 429:
                    break
            else:
                self.fail("A keyless caller can read the feed without any limit.")


class HeadIsAnsweredNotRefusedTests(APIFeedTestCase):
    """`curl -I` on a public API answered 405 (owner, 12 Aug 2026).

    `require_GET` is `require_http_methods(["GET"])`, and HEAD is not in that
    list — so every read endpoint refused it. Pre-existing, and it stopped being
    academic when `/antibodies/` and `/genes/` opened: a keyed partner writes
    deliberate GETs, but a public URL is checked by uptime monitors, link
    checkers, CDN revalidation and crawlers, and all of them start with HEAD.
    A 405 to those reads as a broken endpoint, watched by nobody at OGA.

    `require_safe` is Django's own name for `["GET", "HEAD"]`, so this is the
    idiom rather than a hand-rolled list. Django empties the body itself; the
    headers — including the `Cache-Control` and `Vary` a cache acts on — are the
    same ones GET returns, which is the whole point of the method.
    """

    #: Every read endpoint, and whether it needs a key. The manifest pair still
    #: does; the four feeds and metadata endpoints do not.
    READ_ENDPOINTS = (
        ('api:genes_feed', False),
        ('api:antibodies_feed', False),
        ('api:api_index', False),
        ('api:openapi', False),
        ('api:manifest', True),
        ('api:api_status', True),
    )

    def test_head_is_not_refused_anywhere(self):
        for name, needs_key in self.READ_ENDPOINTS:
            with self.subTest(endpoint=name):
                url = reverse(name)
                response = (
                    self.client.head(url, HTTP_X_API_KEY=str(self.consumer.api_key))
                    if needs_key else self.client.head(url))
                self.assertNotEqual(
                    response.status_code, 405,
                    f"{name} refuses HEAD, so every monitor reads it as broken.")

    def test_head_says_what_get_says(self):
        """Same status and the same cache headers — a HEAD that reported
        something different from the GET beside it would be worse than a 405,
        because a cache would act on it."""
        for name in ('api:genes_feed', 'api:antibodies_feed'):
            with self.subTest(endpoint=name):
                head = self.client.head(reverse(name))
                get = self.client.get(reverse(name))
                self.assertEqual(head.status_code, get.status_code)
                self.assertEqual(head['Cache-Control'], get['Cache-Control'])
                self.assertEqual(head['Vary'], get['Vary'])
                self.assertEqual(head.content, b'')

    def test_a_write_endpoint_still_refuses_get_and_head(self):
        """`require_safe` must not have been sprayed onto the writers."""
        response = self.client.head(
            reverse('api:mark_reviewed'),
            HTTP_X_API_KEY=str(self.consumer.api_key))
        self.assertEqual(response.status_code, 405)
