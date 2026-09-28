"""IHC carries OGA's verdict — on HAP1 cell pellets, judged by eye (26 Sep 2026).

Pinned here are the ways a fifth application goes wrong **silently**:

* an application half-joining — in the tuple and missing from one of the maps
  every reader indexes, so one surface raises or, worse, answers from a default;
* the gene gate forking: one surface counting an IHC recommendation towards
  "this gene is curated" and another not, so the gene page and the API
  disagree about the same WB figure;
* the extension index growing an IHC key its shipped builds cannot draw, or
  re-dating itself on an IHC release;
* the MCP scoring a paper's (tissue) IHC against OGA's pellet result, or
  offering a pellet-IHC recommendation as an alternative for a paper's use;
* a merge dropping the losing row's IHC flag.

Not pinned: the wording of the Judge outcomes sentence or the MCP's context
notes. Those are the parts a Cowork run is most likely to change.
"""
from __future__ import annotations

from django.test import TestCase

from core import recommendations as R
from pipeline.models import (Antibody, Company, PendingPublicationImage,
                             PublicationImage, Site, Target)
from pipeline.services import outcomes as outcome_svc
from pipeline.services import review as review_svc
from pipeline.tests_timeouts import DB, _member_client


class EveryApplicationIsRegisteredEverywhereTests(TestCase):
    """The consistency pin: an application in `R.APPLICATIONS` is in every map
    a verdict reader indexes, or it has half-joined."""

    databases = set()

    def test_every_map_names_every_application(self):
        import importlib

        from core import views as core_views
        # The module, not the view of the same name `pipeline.views` exports.
        outcome_views = importlib.import_module("pipeline.views.outcomes")

        for app in R.APPLICATIONS:
            with self.subTest(app=app):
                self.assertIn(app, R._FLAG)
                self.assertIn(app, outcome_svc.REC_FIELD)
                if app != "FC":
                    # FC alone records no outcome axis, so no capability —
                    # deliberately (`outcomes.CAPABILITY_AXIS`).
                    self.assertIn(app, outcome_svc.CAPABILITY_AXIS)
                self.assertTrue(outcome_svc.APPLICATION_AXES.get(app))
                self.assertIn(app, review_svc.RECOMMENDATION_FIELD)
                self.assertIn(app, review_svc.APPLICATIONS)
                self.assertIn(app, outcome_views.APPLICATIONS)
                self.assertIn(app, outcome_views.APPLICATION_LABELS)
                for table in (core_views._TEMPLATE_APP, core_views._FILTER_APP,
                              core_views._APP_LABEL, core_views._APP_TECHNIQUE,
                              core_views._COLUMN_LABEL):
                    self.assertIn(app, table)
                self.assertEqual(R._FLAG[app], outcome_svc.REC_FIELD[app])
                self.assertEqual(R._FLAG[app],
                                 review_svc.RECOMMENDATION_FIELD[app])

    def test_the_model_and_the_reader_list_the_same_flags(self):
        self.assertEqual(Antibody.RECOMMENDATION_FIELDS, R.RECOMMENDATION_FIELDS)
        self.assertIn("ihc_recommended", R.RECOMMENDATION_FIELDS)


class TheIhcRungsTests(TestCase):
    """The four rungs, as ICC-IF has them: the band grades, the flag decides,
    and a judged "no selective signal" vetoes the flag."""

    databases = set()

    def _caption(self, flagged, band, curated=True):
        ab = Antibody(ihc_recommended=flagged)
        axes = {"selective": {"value": band}} if band else None
        value, _clause, _tempers = R.verdict_with_qualifier(
            ab, "IHC", {"IHC"}, curated, axes)
        return value, R.cell_caption("IHC", value, axes)

    def test_flagged_and_selective_is_supportive_with_the_band(self):
        self.assertEqual(self._caption(True, "selective"),
                         (R.RECOMMENDED, "Supportive — selective"))
        self.assertEqual(self._caption(True, "strongly_selective")[1],
                         "Supportive — strongly selective")

    def test_unflagged_but_selective_is_limited_support(self):
        self.assertEqual(self._caption(False, "selective"),
                         (R.NOT_RECOMMENDED,
                          "Limited support — some selective signal"))

    def test_a_flag_over_no_selective_signal_is_vetoed(self):
        value, caption = self._caption(True, "no_selective_signal")
        self.assertEqual(value, R.NOT_RECOMMENDED)
        self.assertEqual(caption, "Not supportive")

    def test_unclear_does_not_veto_and_adds_no_clause(self):
        self.assertEqual(self._caption(True, "unclear"),
                         (R.RECOMMENDED, "Supportive"))


class TheIhcWriterTests(TestCase):
    databases = {"pipeline_db"}

    @classmethod
    def setUpTestData(cls):
        target = Target.objects.using(DB).create(gene_name="TP53")
        cls.ab = Antibody.objects.using(DB).create(
            target=target, catalogue_number="ab1101")

    def test_the_band_is_recorded_and_read_back(self):
        outcome_svc.record(self.ab.pk, "IHC", "selective", "strongly_selective",
                           actor="x")
        axes = outcome_svc.for_antibodies([self.ab.pk], "IHC")[self.ab.pk]
        self.assertEqual(outcome_svc.label(axes, "IHC"), "strongly_selective")
        self.assertEqual(outcome_svc.capability(axes, "IHC"), outcome_svc.YES)

    def test_a_yes_no_answer_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            outcome_svc.record(self.ab.pk, "IHC", "selective", "yes", actor="x")
        self.assertIn("IHC", str(caught.exception))


class OneGeneGateOnEverySurfaceTests(TestCase):
    """A gene whose only recommendation is IHC is curated — everywhere at once.

    The WB figure on the other antibody was not picked, so every surface must
    call it Not supportive; one surface still asking the four flags would call
    it Not tested beside a gene page calling it Not supportive."""

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.using(DB).create(name="Abcam")
        cls.target = Target.objects.using(DB).create(gene_name="PPP2R5D")
        cls.ihc_only = Antibody.objects.using(DB).create(
            target=cls.target, company=company, catalogue_number="ab188323",
            rrid="AB_1", ihc_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=cls.ihc_only, application_type="IHC",
            image="pubs/ppp2r5d_ihc.png")
        cls.other = Antibody.objects.using(DB).create(
            target=cls.target, company=company, catalogue_number="ab2",
            rrid="AB_2")
        PublicationImage.objects.using(DB).create(
            antibody=cls.other, application_type="WB",
            image="pubs/ppp2r5d_wb.png")

    def test_the_gene_is_curated(self):
        self.assertIn(self.target.pk, R.curated_gene_ids())
        from core.api_views import _target_has_recommendations
        self.assertTrue(_target_has_recommendations(self.target))

    def test_the_gene_page_the_api_the_mcp_and_the_index_agree(self):
        body = self.client.get("/antibodies/PPP2R5D/").content.decode()
        # The WB cell's own caption, not the colour key.
        self.assertIn('experiment-caveat not-supportive">Not supportive</p>',
                      body)

        from core.api_views import _serialise_antibody
        record = _serialise_antibody(self.other, True, include_recs=True)
        self.assertEqual(record["oga_support"]["WB"], R.NOT_SUPPORTIVE)
        self.assertEqual(record["oga_support"]["IHC"], R.NOT_TESTED)

        from mcp_servers.common import portal
        enriched = portal._enrich(self.other, portal._axes_for([self.other]))
        self.assertEqual(enriched["assessment"]["WB"]["status"],
                         "not_recommended")

        from core.extension_index import NOT_RECOMMENDED, build_index
        self.assertEqual(build_index()["antibodies"]["AB_2"]["a"]["WB"],
                         NOT_RECOMMENDED)

    def test_the_ihc_verdict_is_on_the_gene_page_and_in_the_api(self):
        body = self.client.get("/antibodies/PPP2R5D/").content.decode()
        self.assertNotIn("no OGA verdict for IHC", body)
        self.assertIn('<option value="ihc">', body)
        self.assertIn("HAP1 cell pellets, not tissue", body)
        self.assertIn('experiment-caveat supportive">Supportive</p>', body)

        from core.api_views import _serialise_antibody
        record = _serialise_antibody(self.ihc_only, True, include_recs=True)
        self.assertEqual(record["oga_support"]["IHC"], R.SUPPORTIVE)
        self.assertTrue(record["recommendations"]["IHC"])
        self.assertEqual([e["experiment_type"] for e in record["experiments"]],
                         ["IHC"])
        self.assertNotIn("IHC", record["embed_urls"])

    def test_application_ihc_filters_the_feed(self):
        """It returned the whole feed, unfiltered, before IHC had a flag."""
        from core.models import APIConsumer
        key = str(APIConsumer.objects.create(
            name="ihc-filter", consumer_type="rrid", is_active=True).api_key)
        body = self.client.get("/api/v1/antibodies/",
                               {"preview": "true", "application": "IHC"},
                               HTTP_X_API_KEY=key).json()
        self.assertEqual([r["antibody_name"] for r in body["antibodies"]],
                         ["ab188323"])
        body = self.client.get("/api/v1/antibodies/",
                               {"preview": "true", "recommended_only": "true"},
                               HTTP_X_API_KEY=key).json()
        self.assertEqual([r["antibody_name"] for r in body["antibodies"]],
                         ["ab188323"])

    def test_the_headline_and_the_genes_feed_count_ihc(self):
        from pipeline.public import headline_counts
        self.assertEqual(headline_counts()["experiment_count"], 2)
        from core.models import APIConsumer
        key = str(APIConsumer.objects.create(
            name="ihc-genes", consumer_type="rrid", is_active=True).api_key)
        body = self.client.get("/api/v1/genes/", {"preview": "true"},
                               HTTP_X_API_KEY=key).json()
        gene = next(g for g in body["genes"] if g["gene"] == "PPP2R5D")
        self.assertEqual(gene["experiment_count"], 2)
        self.assertEqual(gene["recommendations_by_application"]["IHC"], 1)


class TheExtensionIndexStaysFourTests(TestCase):
    """Shipped builds read four application keys and draw an unknown `img`
    key as an unlabelled figure, so IHC stays out of the index — and out of
    its date."""

    databases = {"pipeline_db"}

    @classmethod
    def setUpTestData(cls):
        target = Target.objects.using(DB).create(gene_name="TP53")
        cls.both = Antibody.objects.using(DB).create(
            target=target, catalogue_number="ab1", rrid="AB_10",
            ihc_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=cls.both, application_type="WB", image="pubs/wb.png")
        cls.ihc_only = Antibody.objects.using(DB).create(
            target=target, catalogue_number="760-2542", rrid="AB_11")

    def test_no_record_carries_an_ihc_key_and_ihc_only_is_counted(self):
        from core.extension_index import _dataset_stamp, build_index
        before = _dataset_stamp()
        PublicationImage.objects.using(DB).create(
            antibody=self.both, application_type="IHC", image="pubs/ihc.png")
        PublicationImage.objects.using(DB).create(
            antibody=self.ihc_only, application_type="IHC", image="pubs/ihc2.png")
        self.assertEqual(_dataset_stamp(), before)

        index = build_index()
        record = index["antibodies"]["AB_10"]
        four = {"WB", "IP", "IF", "FC"}
        for key in ("a", "q", "img"):
            self.assertLessEqual(set(record.get(key) or {}), four, key)
        self.assertNotIn("AB_11", index["antibodies"])
        self.assertEqual(index["counts"]["antibodies_without_indexed_application"], 1)


class TheMcpNeverScoresAPapersIhcTests(TestCase):
    databases = {"pipeline_db"}

    def test_a_papers_ihc_resolves_to_nothing_while_the_assessment_has_ihc(self):
        from mcp_servers.common import portal
        self.assertEqual(portal._normalise_apps(["IHC", "ihc-p", "IHC-P"]), set())
        self.assertIn("IHC", portal._APPS)
        self.assertNotIn("IHC", portal._PAPER_APPS)

        target = Target.objects.using(DB).create(gene_name="TP53")
        ab = Antibody.objects.using(DB).create(
            target=target, catalogue_number="ab1", ihc_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IHC", image="pubs/ihc.png")
        enriched = portal._enrich(ab, portal._axes_for([ab]))
        self.assertEqual(enriched["assessment"]["IHC"]["support"], "supportive")
        self.assertIn("pellets", enriched["assessment"]["IHC"]["sample"])
        self.assertIn("HAP1 cell pellets", enriched["summary"])
        hit = portal._manuscript_hit("ab1", "catalogue", enriched)
        # The paper-scorable four only: an IHC-only reagent is no "concern"
        # and no "recommended" hit for a paper that blotted with it.
        self.assertNotIn("IHC", hit["applications"])
        self.assertEqual(portal._overall_bucket(enriched["assessment"]),
                         "not_tested")
        self.assertFalse(portal._is_concern_hit(hit["applications"], set()))

    def test_pellet_ihc_is_never_offered_as_an_alternative(self):
        """An alternative is offered FOR a paper's application, and a paper's
        IHC is tissue — so an IHC-only recommendation is no alternative, and
        IHC neither appears in `recommended_for` nor lifts a row's rank."""
        from mcp_servers.common import portal
        target = Target.objects.using(DB).create(gene_name="TP53")
        for cat, flags, apps in (("ihc-only", {"ihc_recommended": True}, ["IHC"]),
                                 ("wb-ihc", {"wb_recommended": True,
                                             "ihc_recommended": True},
                                  ["WB", "IHC"]),
                                 ("wb-only", {"wb_recommended": True}, ["WB"])):
            ab = Antibody.objects.using(DB).create(
                target=target, catalogue_number=cat, **flags)
            for app in apps:
                PublicationImage.objects.using(DB).create(
                    antibody=ab, application_type=app,
                    image=f"pubs/TP53_{cat}_{app}.png")

        # A paper that used the gene only for tissue IHC: `wanted` is empty.
        rows, total, matching = portal._recommended_alternatives(
            target, wanted=portal._normalise_apps(["IHC"]))
        self.assertEqual(sorted(r["antibody"] for r in rows),
                         ["wb-ihc", "wb-only"])
        self.assertEqual(total, 2)
        self.assertIsNone(matching)
        for r in rows:
            self.assertEqual(r["recommended_for"], ["WB"])

        # A gene whose ONLY recommendation is pellet IHC offers nothing.
        only = Target.objects.using(DB).create(gene_name="PPP2R5D")
        ab = Antibody.objects.using(DB).create(
            target=only, catalogue_number="p-ihc", ihc_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IHC", image="pubs/p.png")
        self.assertEqual(portal._recommended_alternatives(only),
                         ([], 0, None))


class TheAntibodiesBoardDrawsWhatItsFilterCountsTests(TestCase):
    """`?recommended=yes` asks `any_recommendation_q`, IHC included — so the
    row it lists must carry the IHC verdict, or an IHC-only antibody is listed
    under "Recommended" and drawn as recommended for nothing."""

    databases = {"pipeline_db"}

    def test_an_ihc_only_row_is_listed_and_says_so(self):
        from pipeline.services import antibody_board
        target = Target.objects.using(DB).create(gene_name="TP53")
        Antibody.objects.using(DB).create(
            target=target, catalogue_number="ab1", ihc_recommended=True)
        rows = antibody_board.board_rows(recommended="yes")
        self.assertEqual([r["catalogue"] for r in rows], ["ab1"])
        self.assertTrue(rows[0]["ihc_recommended"])
        self.assertFalse(any(rows[0]["recommended"].values()))
        self.assertNotIn("ihc_recommended", antibody_board.BOOLEAN_FIELDS)


class ReleaseAndWithdrawCarryTheIhcFlagTests(TestCase):
    databases = {"pipeline_db"}

    def test_release_writes_it_and_withdraw_carries_it_back(self):
        target = Target.objects.using(DB).create(gene_name="TP53")
        ab = Antibody.objects.using(DB).create(target=target,
                                               catalogue_number="ab1")
        item = PendingPublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IHC", image="pubs/TP53_ab1_IHC.png",
            recommended=True)
        self.assertEqual(review_svc.manifest([item])["recommended"], 1)
        review_svc.release([item], actor="owner")
        ab.refresh_from_db()
        self.assertTrue(ab.ihc_recommended)

        live = PublicationImage.objects.using(DB).get(antibody=ab)
        review_svc.withdraw([live], actor="owner")
        ab.refresh_from_db()
        self.assertFalse(ab.ihc_recommended)
        self.assertTrue(PendingPublicationImage.objects.using(DB)
                        .get(antibody=ab, status=review_svc.PENDING).recommended)


class AMergeKeepsTheIhcFlagTests(TestCase):
    databases = {"pipeline_db"}

    def test_the_losers_flag_survives_and_the_supplier_ticks_still_do(self):
        from pipeline import dedup_utils
        from pipeline.management.commands.merge_duplicate_antibodies import (
            Command)
        survivor = Antibody(catalogue_number="ab1")
        loser = Antibody(catalogue_number="ab1", ihc_recommended=True,
                         supplier_validated_ihc=True)
        self.assertTrue(dedup_utils.plan_backfill(survivor, [loser])
                        ["ihc_recommended"])
        self.assertTrue(dedup_utils.plan_backfill(survivor, [loser])
                        ["supplier_validated_ihc"])
        plan = Command().plan_backfill(survivor, [loser])
        self.assertEqual(plan["ihc_recommended"], (False, True))
        self.assertEqual(plan["supplier_validated_ihc"], (False, True))

        from pipeline.services import duplicates
        self.assertEqual(duplicates.recommendations(loser), ["IHC"])


class JudgeOutcomesJudgesIhcTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.client = _member_client(self, site)
        target = Target.objects.using(DB).create(gene_name="TP53")
        ab = Antibody.objects.using(DB).create(target=target,
                                               catalogue_number="ab1")
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IHC", image="pubs/ihc.png")

    def test_the_page_offers_ihc_and_the_cards_offer_the_bands(self):
        page = self.client.get("/pipeline/outcomes/", {"gene": "TP53",
                                                        "app": "IHC"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Immunohistochemistry", page.content.decode())

        data = self.client.get("/pipeline/outcomes/antibodies/",
                               {"gene": "TP53", "app": "IHC"}).json()
        self.assertEqual(data["application"], "IHC")
        self.assertEqual([v["value"] for v in data["axis_values"]["selective"]],
                         ["no_selective_signal", "selective",
                          "strongly_selective", "unclear"])
        self.assertEqual([a["name"] for a in data["antibodies"]], ["ab1"])

    def test_the_review_queue_draws_the_same_axis(self):
        from pipeline.views.review import _vocabulary
        self.assertEqual(_vocabulary()["axes"]["IHC"], ["selective"])


class ThePelletsSentenceOnlyUnderAnIhcColumnTests(TestCase):
    """`APPLICATION_FACT['IHC']` names an application; on a gene with no IHC
    column it would name one the page does not show."""

    databases = {"pipeline_db", "academy_db"}

    def test_a_gene_without_ihc_does_not_say_it(self):
        target = Target.objects.using(DB).create(gene_name="SOD1")
        ab = Antibody.objects.using(DB).create(target=target,
                                               catalogue_number="ab1",
                                               wb_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB", image="pubs/wb.png")
        body = self.client.get("/antibodies/SOD1/").content.decode()
        self.assertNotIn("HAP1 cell pellets, not tissue", body)
