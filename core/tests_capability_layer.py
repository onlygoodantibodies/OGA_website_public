"""The capability layer — a qualified negative, and what it must not become.

"Not recommended" has been carrying two different antibodies: one that showed
nothing, and one that did the job the application is *for* and fell short on the
rest. On live data (29 Aug 2026) the second group is 491 of the 1,833
not-recommended verdicts, so better than a quarter of every negative this
dataset publishes.

Three things are pinned, and all three are silent failures:

* **The verdict values do not change.** Four surfaces switch on them and the
  extension index encodes them as small ints in a file every install
  re-downloads daily, so a fourth value would reach the shipped build within
  about a day with no case for it.
* **A caveat is never attached to a positive or to an untested verdict**, where
  it would read as a qualification of something that was never in doubt.
* **The sentence never states a reason the data does not carry.** "but was not
  selective" appears only where selectivity was actually recorded as negative.
"""
from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from pipeline.tests_timeouts import DB

from core import recommendations as rec

# `SimpleTestCase`, not `TestCase`: these are pure functions over a dict, so a
# database is not only unnecessary but actively wrong here — an unqualified
# `TestCase` defaults to the `default` alias, which is deliberately `{}` and
# whose dummy backend raises in the teardown flush. It also asserts, for free,
# that the layer runs no query of its own: the callers batch, and one that
# looked its own answer up would be an N+1 on the extension index.


def _axes(detects=None, selective=None, enriches=None):
    """The shape `pipeline.services.outcomes` hands back."""
    def entry(value):
        return {"value": value, "source": None, "raw": "", "runs": 0,
                "conflict": False, "session_value": None, "review_value": None,
                "ratio": None, "band": None, "needs_grade": False,
                "settled": False, "overridden": False,
                "no_best_concentration": False}
    out = {}
    if detects is not None:
        out["detects"] = entry(detects)
    if selective is not None:
        out["selective"] = entry(selective)
    if enriches is not None:
        out["enriches"] = entry(enriches)
    return out


class TheVerdictValuesAreUnchangedTests(SimpleTestCase):
    def test_there_are_still_exactly_three(self):
        """The extension index encodes these as 0/1/2 and every install
        re-downloads it daily. A fourth would land in the shipped build with no
        case for it."""
        self.assertEqual(
            sorted(rec.MEANINGS),
            sorted([rec.RECOMMENDED, rec.NOT_RECOMMENDED, rec.NOT_TESTED]))


class ACaveatOnlyQualifiesANegativeTests(SimpleTestCase):
    def test_a_negative_with_a_capability_earns_one(self):
        """"Limited support — detects the target." """
        said = rec.qualifier("WB", rec.NOT_RECOMMENDED,
                             _axes(detects="yes", selective="no"))
        self.assertIn("detects the target", said)

    def test_a_supportive_verdict_earns_one_too(self):
        """The half the layer was missing. 312 of the 879 supportive western
        blot verdicts — 36% of the green on the site — are not selective, and
        they were drawn identically to the 566 that are clean."""
        said = rec.qualifier("WB", rec.RECOMMENDED,
                             _axes(detects="yes", selective="no"))
        self.assertEqual(said, "detects the target, but is not selective")

    def test_a_clean_supportive_verdict_earns_nothing(self):
        self.assertEqual(
            rec.qualifier("WB", rec.RECOMMENDED,
                          _axes(detects="yes", selective="yes")), "")

    def test_an_if_band_grades_a_supportive_verdict(self):
        for band, words in (("strongly_selective", "strongly selective"),
                            ("selective", "selective")):
            self.assertEqual(
                rec.qualifier("ICC-IF", rec.RECOMMENDED, _axes(selective=band)),
                words, band)

    def test_a_supportive_verdict_with_nothing_to_add_carries_nothing(self):
        """Selectivity unrecorded: the layer never states a reason the data
        does not carry."""
        self.assertEqual(
            rec.qualifier("WB", rec.RECOMMENDED, _axes(detects="yes")), "")

    def test_an_untested_verdict_never_carries_one(self):
        self.assertEqual(
            rec.qualifier("WB", rec.NOT_TESTED, _axes(detects="yes")), "")

    def test_a_negative_that_showed_nothing_stays_plain(self):
        self.assertEqual(
            rec.qualifier("WB", rec.NOT_RECOMMENDED, _axes(detects="no")), "")

    def test_an_unanswered_axis_earns_nothing(self):
        """Absence of a judgement is not evidence of capability."""
        self.assertEqual(rec.qualifier("WB", rec.NOT_RECOMMENDED, _axes()), "")
        self.assertEqual(rec.qualifier("WB", rec.NOT_RECOMMENDED, None), "")


class OneReaderBehindEveryStatementOfAVerdictTests(TestCase):
    """`describe` is what the five surfaces say, so they cannot say five things.

    `recommendation()` kept the *verdict* from splitting into five answers and
    the words and the colour then split into five anyway — the gene page
    composing its own sentence, the portal its own pill wording, the connector
    its own prose, the extension its own labels.
    """
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        from pipeline.models import (Antibody, AntibodyOutcome, Company,
                                     PublicationImage, Target)
        company = Company.objects.create(name="Abcam")
        target = Target.objects.create(gene_name="ATP2B1")
        cls.ab = Antibody.objects.create(
            target=target, company=company, catalogue_number="ab190355",
            wb_recommended=True, if_recommended=True)
        for app in ("WB", "ICC-IF"):
            PublicationImage.objects.create(
                antibody=cls.ab, application_type=app,
                image=f"pubs/{app}.png")
        AntibodyOutcome.objects.create(
            antibody=cls.ab, application_type="WB",
            detects="yes", selective="no")
        AntibodyOutcome.objects.create(
            antibody=cls.ab, application_type="ICC-IF",
            selective="strongly_selective")

    def _describe(self, app):
        axes = rec.capability_axes([self.ab.pk])
        return rec.describe(self.ab, app, {"WB", "ICC-IF"}, True,
                            axes.get((self.ab.pk, app)))

    def test_the_controlled_value_is_untouched(self):
        """Everything else is additive. The API publishes this and the
        extension encodes it as 0/1/2 in a file every install re-downloads
        daily, so a shipped build must go on reading exactly these."""
        self.assertEqual(self._describe("WB")["verdict"], rec.RECOMMENDED)
        self.assertEqual(self._describe("IP")["verdict"], rec.NOT_TESTED)

    def test_the_words_are_the_evidence_frame(self):
        self.assertEqual(self._describe("WB")["words"], "Supportive")
        self.assertEqual(self._describe("IP")["words"], "Not tested")

    def test_the_sentence_is_composed_once(self):
        """No surface assembles its own dash."""
        self.assertEqual(self._describe("WB")["sentence"],
                         "Supportive — detects the target, but is not selective")
        self.assertEqual(self._describe("ICC-IF")["sentence"],
                         "Supportive — strongly selective")
        # An untested application still has words to print, even though the
        # gene page draws none — its cell carries a "No data available" image.
        self.assertEqual(self._describe("IP")["sentence"], "Not tested")

    def test_the_colour_and_the_tab_come_with_it(self):
        wb, icc = self._describe("WB"), self._describe("ICC-IF")
        self.assertEqual((wb["tone"], wb["tab"]), ("supportive", True))
        self.assertEqual((icc["tone"], icc["tab"]), ("supportive", False))
        self.assertEqual(self._describe("IP")["tone"], "")

    def test_describe_all_answers_for_every_application(self):
        described = rec.describe_all(self.ab, {"WB", "ICC-IF"}, True)
        self.assertEqual(set(described), set(rec.APPLICATIONS))
        self.assertEqual(described["WB"]["sentence"],
                         "Supportive — detects the target, but is not selective")


class OnlyAQualifierThatHoldsTheVerdictBackIsAmberTests(SimpleTestCase):
    """The amber edge is a reservation, and a superlative is not one.

    `Supportive - strongly selective` shipped with the amber modifier on a live
    ATP2B1 page: the strongest thing this dataset can say about an antibody,
    drawn in the colour the site uses to mark a shortfall. The clause and the
    colour are two facts, and `qualified` answers both.
    """

    def test_the_best_if_result_is_not_marked_as_a_reservation(self):
        for band in ("strongly_selective", "selective"):
            clause, tempers = rec.qualified("ICC-IF", rec.RECOMMENDED,
                                            _axes(selective=band))
            self.assertTrue(clause, band)
            self.assertFalse(tempers, band)

    def test_a_supportive_blot_that_is_not_selective_is(self):
        clause, tempers = rec.qualified(
            "WB", rec.RECOMMENDED, _axes(detects="yes", selective="no"))
        self.assertEqual(clause, "detects the target, but is not selective")
        self.assertTrue(tempers)

    def test_every_clause_on_a_negative_softens_it(self):
        for app, axes in (("WB", _axes(detects="yes")),
                          ("IP", _axes(enriches="yes")),
                          ("ICC-IF", _axes(selective="selective"))):
            clause, tempers = rec.qualified(app, rec.NOT_RECOMMENDED, axes)
            self.assertTrue(clause, app)
            self.assertTrue(tempers, app)

    def test_nothing_to_say_is_never_amber(self):
        for verdict in (rec.RECOMMENDED, rec.NOT_RECOMMENDED, rec.NOT_TESTED):
            self.assertEqual(rec.qualified("WB", verdict, _axes()), ("", False))
            self.assertEqual(rec.qualified("WB", verdict, None), ("", False))

    def test_the_published_clause_is_the_one_qualified_returns(self):
        """`qualifier` is the API's half of the same answer, so the two cannot
        drift into two sentences for one cell."""
        for app, verdict, axes in (
                ("WB", rec.RECOMMENDED, _axes(detects="yes", selective="no")),
                ("ICC-IF", rec.RECOMMENDED, _axes(selective="strongly_selective")),
                ("IP", rec.NOT_RECOMMENDED, _axes(enriches="yes"))):
            self.assertEqual(rec.qualifier(app, verdict, axes),
                             rec.qualified(app, verdict, axes)[0], app)


class TheSentenceStatesOnlyWhatWasRecordedTests(SimpleTestCase):
    def test_the_negative_clause_says_only_what_it_did(self):
        """It names the capability and nothing else — why the verdict went the
        other way is a judgement no column holds."""
        said = rec.qualifier("WB", rec.NOT_RECOMMENDED,
                             _axes(detects="yes", selective="no"))
        self.assertEqual(said, "detects the target")

    def test_both_axes_positive_and_no_recommendation_explains_nothing(self):
        """A human judgement this module cannot explain, so it does not try."""
        said = rec.qualifier("WB", rec.NOT_RECOMMENDED,
                             _axes(detects="yes", selective="yes"))
        self.assertEqual(said, "detects the target")

    def test_each_application_names_its_own_question(self):
        self.assertIn("enriches the target, but not significantly", rec.qualifier(
            "IP", rec.NOT_RECOMMENDED, _axes(enriches="yes")))
        self.assertIn("some selective signal", rec.qualifier(
            "ICC-IF", rec.NOT_RECOMMENDED, _axes(selective="strongly_selective")))

    def test_an_if_band_counts_as_capable_at_either_grade(self):
        for band in ("selective", "strongly_selective"):
            self.assertTrue(rec.is_capable(_axes(selective=band), "ICC-IF"), band)
        self.assertFalse(
            rec.is_capable(_axes(selective="no_selective_signal"), "ICC-IF"))

    def test_flow_cytometry_has_no_capability_to_read(self):
        """`histogram_shift` is blank on all 22 live rows, so FC verdicts stay
        two-state. A gap in the data, not a judgement about the assay."""
        self.assertNotIn("FC", rec.CAPABILITY_WORDS)
        self.assertEqual(rec.qualifier("FC", rec.NOT_RECOMMENDED, _axes()), "")
        self.assertEqual(rec.qualifier("FC", rec.RECOMMENDED, _axes()), "")


class TheInterimWordingReachesInstalledBuildsTests(SimpleTestCase):
    """The one lever that moves before a store release.

    ``APPLICATION_SCOPE`` ships inside ``index.json`` and is drawn by the
    extension's card, so rewording it reaches every installed build at the next
    daily refresh with no review. The per-antibody caveat cannot: ``q`` is data
    the shipped build does not read, and its colours live in its JavaScript.

    What is pinned is the property that makes the interim safe — the card draws
    this under whichever application is open and **cannot tell a supportive
    verdict from a negative one**, so the sentence has to be true beside either.

    The quoted verdict tracks what the surfaces print: ‘not supportive’ since
    30 Aug 2026. The assertion is on the quoted word for that reason — a
    sentence quoting a verdict nobody shows any more is the failure this pins.
    """

    def test_every_application_with_a_capability_warns_about_its_negatives(self):
        for app in rec.CAPABILITY_WORDS:
            self.assertIn(app, rec.APPLICATION_SCOPE, app)
            self.assertIn('not supportive', rec.APPLICATION_SCOPE[app], app)

    def test_flow_cytometry_promises_no_nuance_it_cannot_deliver(self):
        """Nothing records an FC outcome, so there is no qualified negative to
        warn about."""
        self.assertNotIn('FC', rec.APPLICATION_SCOPE)

    def test_the_wording_is_about_the_vocabulary_not_this_antibody(self):
        """It is drawn under supportive verdicts too. "This antibody detected
        its target" would be a claim; "a 'not supportive' result may still
        mean" is a fact about what the word covers, and is true beside either."""
        for app, text in rec.APPLICATION_SCOPE.items():
            self.assertIn('may still mean', text, app)

    def test_each_application_names_its_own_capability(self):
        self.assertIn('detected', rec.APPLICATION_SCOPE['WB'])
        self.assertIn('enriched', rec.APPLICATION_SCOPE['IP'])
        self.assertIn('selective signal', rec.APPLICATION_SCOPE['ICC-IF'])

    def test_immunofluorescence_keeps_the_fixation_caveat_it_already_had(self):
        """Owner's instruction, 27 Aug 2026. Appended to, not replaced."""
        self.assertIn('fixation and permeabilisation',
                      rec.APPLICATION_SCOPE['ICC-IF'])


class TheIndexCarriesTheQualifiedNegativesTests(TestCase):
    """`q` on the record, beside the codes and never inside them.

    Every install re-downloads this file daily, so a fourth verdict value would
    have reached the shipped build long before a build that knew what to do with
    it. What is pinned is that the codes are untouched and the new key is
    sparse — a key on every row would cost the whole install base bytes for a
    fact about a minority of them.
    """

    databases = {"pipeline_db", "academy_db"}

    def _published(self, catalogue, rrid, gene, recommended, detects):
        """An antibody with a published WB figure and a recorded judgement.

        The judgement goes on `AntibodyOutcome` rather than through a session
        and a result row: it is the same path the capability is read by, and a
        session drags in an experimenter, a member and a login for no gain here.
        """
        from django.core.files.uploadedfile import SimpleUploadedFile
        from pipeline.models import (Antibody, AntibodyOutcome, Company,
                                     PublicationImage, Site, Target)
        site, _ = Site.objects.using(DB).get_or_create(
            name="Leicester", defaults={"short_code": "LEI"})
        company, _ = Company.objects.using(DB).get_or_create(name="Abcam")
        target, _ = Target.objects.using(DB).get_or_create(gene_name=gene)
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, rrid=rrid, target=target,
            company=company, site=site, wb_recommended=recommended)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB",
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="WB",
            detects=detects, selective="no")
        return ab

    def test_a_qualified_negative_is_listed_and_a_plain_one_is_not(self):
        from core.extension_index import build_index
        # A recommendation on the gene, so the negatives read as assessed
        # rather than as untested.
        self._published("abREC", "AB_1", "TRPA1", True, "yes")
        self._published("abQUAL", "AB_2", "TRPA1", False, "yes")
        self._published("abPLAIN", "AB_3", "TRPA1", False, "no")

        records = build_index()["antibodies"]
        # A map of two-letter codes since 0.3.0, not a bare list of
        # applications: the extension draws the clause, and on a supportive
        # immunofluorescence verdict it needs the *grade* — a list could not
        # tell "strongly selective" from a shortfall.
        # `abQUAL`: not supportive, and it does detect.
        self.assertEqual(records["AB_2"].get("q"), {"WB": "sd"})
        # `abREC`: supportive, and not selective — a qualifier on the other
        # side of the verdict, which the layer carries since 29 Aug 2026.
        self.assertEqual(records["AB_1"].get("q"), {"WB": "ns"})
        # Sparse: absent entirely where there is nothing to add.
        self.assertNotIn("q", records["AB_3"])

    def test_the_verdict_codes_are_untouched(self):
        """A build that has never heard of `q` must behave exactly as it does
        today, and it switches on these."""
        from core import extension_index as ext
        self.assertEqual(
            (ext.NOT_TESTED, ext.NOT_RECOMMENDED, ext.RECOMMENDED), (0, 1, 2))
        self._published("abQUAL", "AB_2", "TRPA1", False, "yes")
        self._published("abREC", "AB_1", "TRPA1", True, "yes")
        record = ext.build_index()["antibodies"]["AB_2"]
        self.assertEqual(record["a"]["WB"], ext.NOT_RECOMMENDED)


class TheFourRungsAreOneDecisionTests(SimpleTestCase):
    """`support()` and `words()` must never come to different answers.

    They are the same question asked twice — which rung is this — and the whole
    promise `oga_support` makes to a consumer is *switch on this, print that*.
    If the two ever disagree the API hands somebody a value and a word that
    contradict each other, and nothing on any screen would say so: the word
    still renders, the value still validates against the enum, and the row is
    quietly wrong.

    Held by construction (`words` reads `SUPPORT_WORDS[support(...)]`), which is
    exactly why it is worth pinning — the cheap way to break it is to give
    `words` its own branch back.
    """

    def test_every_pair_agrees(self):
        for verdict in (rec.RECOMMENDED, rec.NOT_RECOMMENDED, rec.NOT_TESTED):
            for tempers in (True, False):
                with self.subTest(verdict=verdict, tempers=tempers):
                    self.assertEqual(
                        rec.SUPPORT_WORDS[rec.support(verdict, tempers)],
                        rec.words(verdict, tempers))

    def test_there_are_exactly_four_and_they_are_all_spoken_for(self):
        self.assertEqual(len(rec.SUPPORT_VALUES), 4)
        self.assertEqual(sorted(rec.SUPPORT_WORDS), sorted(rec.SUPPORT_VALUES))
        self.assertEqual(sorted(rec.SUPPORT_MEANINGS), sorted(rec.SUPPORT_VALUES))
        self.assertEqual(sorted(rec.LEGACY_VERDICT_OF), sorted(rec.SUPPORT_VALUES))

    def test_the_middle_rung_is_the_one_the_legacy_field_cannot_say(self):
        """Both negatives report `not_recommended`, which is the entire reason
        the new field exists. If this ever stops being true the mapping table in
        API.md, on `/data-access/` and in the OpenAPI description is wrong on
        three surfaces at once."""
        self.assertEqual(rec.LEGACY_VERDICT_OF[rec.LIMITED_SUPPORT],
                         rec.NOT_RECOMMENDED)
        self.assertEqual(rec.LEGACY_VERDICT_OF[rec.NOT_SUPPORTIVE],
                         rec.NOT_RECOMMENDED)
        self.assertNotEqual(rec.LIMITED_SUPPORT, rec.NOT_SUPPORTIVE)

    def test_a_negative_that_tempers_is_the_middle_rung_and_nothing_else_is(self):
        self.assertEqual(rec.support(rec.NOT_RECOMMENDED, True),
                         rec.LIMITED_SUPPORT)
        self.assertEqual(rec.support(rec.NOT_RECOMMENDED, False),
                         rec.NOT_SUPPORTIVE)
        # A supportive result the data fell short of stays supportive: the
        # antibody IS supported for the application and the shortfall rides as
        # the qualifier. Calling it `limited_support` would contradict the word
        # printed under it.
        self.assertEqual(rec.support(rec.RECOMMENDED, True), rec.SUPPORTIVE)
        self.assertEqual(rec.support(rec.NOT_TESTED, True), rec.NOT_TESTED)

    def test_untested_is_one_value_in_both_vocabularies(self):
        """Not a mapping — the same string. A consumer holding both fields must
        not have to handle two spellings of the one rung they agree about."""
        self.assertEqual(rec.NOT_TESTED, rec.LEGACY_VERDICT_OF[rec.NOT_TESTED])
        self.assertIn(rec.NOT_TESTED, rec.SUPPORT_VALUES)


class TheNewFieldIsAdditiveOnTheWireTests(TestCase):
    """`oga_support` arrives beside the legacy fields, never instead of them.

    This is the whole of the promise made to the API's readers, and the reason
    it has to be a test rather than an intention: on 14 Sep 2026 six named
    organisations held keys and **269 of the API's 357 all-time requests were
    keyless**. A keyless caller cannot be told, surveyed, or checked up on
    afterwards, so a field that quietly stopped arriving would surface as
    somebody's dashboard misreporting their own products, months later, with
    nothing on any screen to catch it by.

    So: the legacy keys are asserted present and unchanged in the same breath as
    the new one. A future edit that "tidies up" by dropping either fails here
    and names the reason.
    """

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from core.models import APIConsumer
        from pipeline.models import (Antibody, AntibodyOutcome, Company,
                                     PublicationImage, Site, Target)

        site, _ = Site.objects.using(DB).get_or_create(
            name="Leicester", defaults={"short_code": "LEI"})
        company, _ = Company.objects.using(DB).get_or_create(name="Abcam")
        target, _ = Target.objects.using(DB).get_or_create(gene_name="TRPA1")
        # One supportive antibody, so the gene reads as curated and the
        # negatives below are assessed rather than untested.
        Antibody.objects.using(DB).create(
            catalogue_number="abREC", rrid="AB_1", target=target,
            company=company, site=site, wb_recommended=True)
        # The middle rung: not supportive overall, and it did detect.
        cls.limited = Antibody.objects.using(DB).create(
            catalogue_number="abQUAL", rrid="AB_2", target=target,
            company=company, site=site, wb_recommended=False)
        PublicationImage.objects.using(DB).create(
            antibody=cls.limited, application_type="WB",
            image=SimpleUploadedFile("q.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=cls.limited, application_type="WB",
            detects="yes", selective="no")
        cls.key = str(APIConsumer.objects.create(
            name="additive-contract", consumer_type="rrid",
            is_active=True).api_key)

    def _row(self):
        body = self.client.get("/api/v1/antibodies/", {"preview": "true"},
                               HTTP_X_API_KEY=self.key).json()
        rows = [r for r in body["antibodies"] if r["antibody_name"] == "abQUAL"]
        self.assertEqual(len(rows), 1, "the fixture antibody is not in the feed")
        return rows[0]

    def test_the_new_field_names_the_rung_the_legacy_one_cannot(self):
        row = self._row()
        self.assertEqual(row["oga_support"]["WB"], rec.LIMITED_SUPPORT)
        # The same row, through the field a consumer reads today: both negatives
        # look like this, which is exactly why the line above exists.
        self.assertEqual(row["oga_recommendations"]["WB"], rec.NOT_RECOMMENDED)

    def test_the_legacy_fields_still_arrive_unchanged(self):
        row = self._row()
        self.assertIn("recommendations", row)
        self.assertIs(row["recommendations"]["WB"], False)
        self.assertIn("oga_recommendations", row)
        self.assertEqual(sorted(row["oga_recommendations"]),
                         sorted(rec.APPLICATIONS))

    def test_every_application_has_a_rung(self):
        """Not sparse, unlike `oga_qualifiers`. An absent key would be read as
        "no answer" where the answer is `not_tested`, which is the one
        distinction this whole module exists to protect."""
        row = self._row()
        self.assertEqual(sorted(row["oga_support"]), sorted(rec.APPLICATIONS))
        for app, value in row["oga_support"].items():
            with self.subTest(app=app):
                self.assertIn(value, rec.SUPPORT_VALUES)

    def test_the_display_block_no_longer_carries_the_banned_word(self):
        """`verdict` was removed from the public contract on 7 Aug 2026 and
        reached the wire again on 12 Sep when `oga_display` was published as
        `describe()`'s own dict. The legacy value is one key away in
        `oga_recommendations`, so nothing is lost by its absence."""
        row = self._row()
        self.assertNotIn("verdict", row["oga_display"]["WB"])
        self.assertEqual(row["oga_display"]["WB"]["support"],
                         rec.LIMITED_SUPPORT)
        self.assertEqual(row["oga_display"]["WB"]["words"], rec.LIMITED_WORDS)
