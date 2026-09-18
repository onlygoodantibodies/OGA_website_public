"""Judge outcomes — the two axes a recommendation flattens into one bit.

What is pinned here is what would be **silently** wrong.

The reviewers' feedback is that ``recommended`` cannot hold what the bench
recorded, and the evidence is that the bench recorded two things all along:
``WbResult.signal`` is the Access ``SpecificSignal`` column (does it detect the
target) and ``WbResult.rating`` is ``SelectiveSignal`` (is it selective). On the
1,584 antibodies with a published WB figure, 558 detect the target and are not
selective, and the boolean splits that band almost in half — 282 recommended
against 276 not — so identical recorded evidence produces opposite public
verdicts.

Three failures would be invisible on a screen:

* **A review judgement overwriting a bench reading.** The page fills blanks; it
  does not overrule the person who ran the blot. Nothing on the card would show
  it had happened.
* **A disagreement between two runs resolved by picking one.** 26 published
  antibodies have runs that disagree about ``signal`` and 32 about ``rating``.
  Choosing silently turns a question into a fact.
* **Free text read as a verdict.** ``rating`` has never had a stated vocabulary,
  so a cell may hold ``5`` or a sentence; finding a value in a string is not
  reading one.

The public surfaces are pinned too, in the other direction: nothing public reads
this table yet, by decision, and a leak would show up nowhere until somebody
noticed the website had changed.
"""
from __future__ import annotations

import json
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from pipeline.models import (Antibody, AntibodyOutcome, Company,
                             ExperimentSession, Member, PublicationImage, Site,
                             Target, WbResult)
from pipeline.services import outcomes as outcome_svc
from pipeline.tests_timeouts import DB, _member_client

PAGE = "/pipeline/outcomes/"
SAVE = "/pipeline/outcomes/save/"
CARDS = "/pipeline/outcomes/antibodies/"
GENES = "/pipeline/outcomes/genes/"
RECOMMEND = "/pipeline/outcomes/recommend/"


class _Fixture(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")

    def _antibody(self, catalogue, published=True):
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site)
        if published:
            PublicationImage.objects.using(DB).create(
                antibody=ab, application_type="WB",
                image=SimpleUploadedFile(f"{catalogue}.png", b"not-a-png"))
        return ab

    def _reading(self, ab, signal="", rating=""):
        member = Member.objects.using(DB).filter(site=self.site).first()
        session = ExperimentSession.objects.using(DB).create(
            procedure_type="WB", target=self.target, site=self.site,
            experimenter=member, date=date(2026, 5, 1))
        return WbResult.objects.using(DB).create(
            session=session, antibody=ab, signal=signal, rating=rating)

    def _axes(self, ab):
        return outcome_svc.for_gene(self.target.pk, "WB")[ab.pk]


class TheTwoAxesAreReadFromTheColumnsThatHoldThemTests(_Fixture):
    """``signal`` is SpecificSignal and ``rating`` is SelectiveSignal.

    ``rating`` is misnamed by the import and reads like a 1–5 score. Renaming a
    live column is the one migration a rollback cannot undo, so the name stays
    and ``services/outcomes.py`` is what says which question it answers. A
    future edit that swapped the two would be invisible: both are YES/NO.
    """

    def test_signal_is_detects_and_rating_is_selective(self):
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="NO")
        axes = self._axes(ab)
        self.assertEqual(axes["detects"]["value"], "yes")
        self.assertEqual(axes["selective"]["value"], "no")

    def test_the_middle_band_has_a_name_of_its_own(self):
        """Detects and not selective is not "not recommended" — it is the third
        of the dataset the single boolean cannot describe."""
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="NO")
        self.assertEqual(outcome_svc.label(self._axes(ab)),
                         "detects_not_selective")

    def test_a_verdict_needs_both_answers(self):
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="")
        self.assertIsNone(outcome_svc.label(self._axes(ab)))

    def test_free_text_is_not_read_as_a_verdict(self):
        """`rating` has never had a stated vocabulary. A `5` is not a yes."""
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="5")
        axes = self._axes(ab)
        self.assertIsNone(axes["selective"]["value"])
        # …and what it could not read is carried, so the page can print it
        # rather than reporting the cell as empty.
        self.assertEqual(axes["selective"]["raw"], "5")


class AJudgementWinsAndKeepsWhatItOverrodeTests(_Fixture):
    """Every change is made from the screen with the figure on it (owner,
    29 Aug 2026), which inverts the rule this module shipped with: the bench
    record used to win and the page filled only its blanks.

    What it buys is one place to do the work. What it costs is that a call made
    from a crop can sit on top of a reading typed at the bench — so the whole of
    the safety is that nothing is hidden. Every one of these asserts the
    overridden value survives, because a card that stopped printing it would be
    indistinguishable from a reading nobody questioned.
    """

    def test_a_reading_can_be_overridden_and_what_it_overrode_is_kept(self):
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="NO")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective", "value": "yes"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._axes(ab)
        self.assertEqual(axes["selective"]["value"], "yes")
        self.assertEqual(axes["selective"]["source"], "review")
        self.assertTrue(axes["selective"]["overridden"])
        self.assertEqual(axes["selective"]["session_value"], "no")
        self.assertEqual(axes["selective"]["raw"], "NO")

    def test_overriding_a_reading_changes_no_session_row(self):
        """The runs are the record of what happened on a day."""
        ab = self._antibody("ab1")
        row = self._reading(ab, signal="YES", rating="NO")
        self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective", "value": "yes"}),
            content_type="application/json")
        row.refresh_from_db()
        self.assertEqual((row.signal, row.rating), ("YES", "NO"))

    def test_agreeing_with_the_bench_is_not_an_override(self):
        """Confirmation is not disagreement, and a card calling it one would
        cry wolf on the rows that matter."""
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="NO")
        self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective", "value": "no"}),
            content_type="application/json")
        self.assertFalse(self._axes(ab)["selective"]["overridden"])

    def test_a_review_fills_a_blank(self):
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective", "value": "no"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._axes(ab)
        self.assertEqual(axes["selective"]["value"], "no")
        self.assertEqual(axes["selective"]["source"], "review")
        self.assertEqual(axes["detects"]["source"], "session")

    def test_an_antibody_with_no_reading_at_all_can_be_judged(self):
        """129 of the 1,584 published WB figures have no `WbResult` row of any
        kind. There is no session to type into, which is the whole reason this
        table exists."""
        ab = self._antibody("ab1")
        for axis, value in (("detects", "yes"), ("selective", "no")):
            resp = self.client.post(SAVE, json.dumps(
                {"antibody_id": ab.pk, "axis": axis, "value": value}),
                content_type="application/json")
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(outcome_svc.label(self._axes(ab)),
                         "detects_not_selective")

    def test_a_reading_arriving_after_a_judgement_is_shown_not_swallowed(self):
        """The judgement still stands — but the reading that turned up
        afterwards is on the entry, so the card can say the two disagree."""
        ab = self._antibody("ab1")
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="WB", selective="yes")
        self._reading(ab, signal="YES", rating="NO")
        axes = self._axes(ab)
        self.assertEqual(axes["selective"]["value"], "yes")
        self.assertTrue(axes["selective"]["overridden"])
        self.assertEqual(axes["selective"]["session_value"], "no")


class RunsThatDisagreeAreAQuestionTests(_Fixture):
    """213 published antibodies have two or more WB result rows; on 26 the runs
    disagree about `signal` and on 32 about `rating`. Picking one silently turns
    a disagreement into a fact."""

    def test_two_runs_that_disagree_produce_no_value(self):
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="YES")
        self._reading(ab, signal="NO", rating="YES")
        axes = self._axes(ab)
        self.assertIsNone(axes["detects"]["value"])
        self.assertTrue(axes["detects"]["conflict"])
        # The agreeing axis is unaffected — a disagreement about one question is
        # not a disagreement about the other.
        self.assertEqual(axes["selective"]["value"], "yes")

    def test_two_runs_that_agree_are_one_answer(self):
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="NO")
        self._reading(ab, signal="YES", rating="NO")
        axes = self._axes(ab)
        self.assertEqual(axes["detects"]["value"], "yes")
        self.assertFalse(axes["detects"]["conflict"])
        self.assertEqual(axes["detects"]["runs"], 2)

    def test_a_disagreement_is_settled_here_from_the_figure(self):
        """Refusing it made a dead end: the page counted disagreements as work
        to do and then turned them away at the click, with its own count
        pointing at rows it would not accept. The session record has no single
        answer to win with, and the figure on the screen is the evidence."""
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="YES")
        self._reading(ab, signal="NO", rating="NO")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "detects", "value": "yes"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._axes(ab)
        self.assertEqual(axes["detects"]["value"], "yes")
        self.assertEqual(axes["detects"]["source"], "review")
        # The disagreement is kept — a card that stopped printing it would make
        # a judged row look like an agreed one.
        self.assertTrue(axes["detects"]["conflict"])
        self.assertTrue(axes["detects"]["settled"])
        # `selective` disagrees too and has not been judged, so the antibody is
        # still a gap — one axis settled is not the row settled.
        self.assertTrue(outcome_svc.is_gap(axes, "WB"))
        self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective", "value": "no"}),
            content_type="application/json")
        self.assertFalse(outcome_svc.is_gap(self._axes(ab), "WB"))

    def test_settling_changes_no_session_row(self):
        """Each run still says what it said. This page writes the antibody's
        verdict, not the bench's record of a day's work."""
        ab = self._antibody("ab1")
        first = self._reading(ab, signal="YES", rating="YES")
        second = self._reading(ab, signal="NO", rating="NO")
        self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "detects", "value": "no"}),
            content_type="application/json")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.signal, second.signal), ("YES", "NO"))

    def test_a_settled_disagreement_stops_being_counted_as_work(self):
        """It is still drawn on its card. Counting it in the header again
        would point at a row with nothing left to do on it."""
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="NO")
        self._reading(ab, signal="NO", rating="NO")
        by_ab = outcome_svc.for_gene(self.target.pk, "WB")
        self.assertEqual(outcome_svc.summarise(by_ab, "WB")["conflicts"], 1)
        self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "detects", "value": "yes"}),
            content_type="application/json")
        by_ab = outcome_svc.for_gene(self.target.pk, "WB")
        stats = outcome_svc.summarise(by_ab, "WB")
        self.assertEqual(stats["conflicts"], 0)
        self.assertEqual(stats["gaps"], 0)

    def test_a_judgement_can_be_changed_and_cleared(self):
        """A judgement made here is a person's call, and people revise them.
        Nothing about it is one-way: press another answer to change it, or the
        one already set to take it back off."""
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="YES")
        self._reading(ab, signal="NO", rating="NO")

        for value in ("yes", "no", "unclear"):
            resp = self.client.post(SAVE, json.dumps(
                {"antibody_id": ab.pk, "axis": "detects", "value": value}),
                content_type="application/json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self._axes(ab)["detects"]["value"], value)
            # One row per antibody per application, revised — never a second.
            self.assertEqual(
                AntibodyOutcome.objects.using(DB).filter(antibody=ab).count(), 1)

        # And back off again, which returns it to the work list.
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "detects", "value": ""}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._axes(ab)
        self.assertIsNone(axes["detects"]["value"])
        self.assertFalse(axes["detects"]["settled"])
        self.assertTrue(axes["detects"]["conflict"])
        self.assertTrue(outcome_svc.is_gap(axes, "WB"))

    def test_an_axis_two_runs_agree_on_is_judgeable_too(self):
        """Every change from this page, not half here and half elsewhere."""
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="YES")
        self._reading(ab, signal="NO", rating="YES")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective", "value": "no"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._axes(ab)
        self.assertEqual(axes["selective"]["value"], "no")
        self.assertTrue(axes["selective"]["overridden"])


class WhatTheScreenSaysTests(_Fixture):
    def test_the_cards_carry_both_axes_and_where_each_came_from(self):
        ab = self._antibody("ab1")
        self._reading(ab, signal="YES", rating="")
        data = self.client.get(CARDS, {"gene": "TRPA1"}).json()
        row = data["antibodies"][0]
        self.assertEqual(row["axes"]["detects"]["source"], "session")
        self.assertIsNone(row["axes"]["selective"]["source"])
        self.assertTrue(row["is_gap"])
        # Drawn as context, never as a control: this page does not write it.
        self.assertIn("recommended", row)

    def test_the_picker_is_ordered_by_what_is_left_to_do(self):
        """154 genes have published WB figures. Alphabetical makes somebody find
        the work by hand."""
        done = Target.objects.using(DB).create(gene_name="AAAA")
        ab_done = Antibody.objects.using(DB).create(
            catalogue_number="abDone", target=done,
            company=self.company, site=self.site)
        PublicationImage.objects.using(DB).create(
            antibody=ab_done, application_type="WB",
            image=SimpleUploadedFile("d.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab_done, application_type="WB",
            detects="yes", selective="yes")
        self._antibody("ab1")   # TRPA1, nothing judged

        genes = self.client.get(GENES).json()["genes"]
        self.assertEqual(genes[0]["name"], "TRPA1")
        self.assertEqual(genes[0]["gap_count"], 1)
        self.assertEqual(genes[1]["name"], "AAAA")
        self.assertEqual(genes[1]["gap_count"], 0)

    def test_the_picker_does_not_grow_a_query_per_gene(self):
        """Three queries for the whole picker, not three per gene — the boards'
        rule, and a picker over every public gene is a long table."""
        for n in range(6):
            t = Target.objects.using(DB).create(gene_name=f"GENE{n}")
            ab = Antibody.objects.using(DB).create(
                catalogue_number=f"cat{n}", target=t,
                company=self.company, site=self.site)
            PublicationImage.objects.using(DB).create(
                antibody=ab, application_type="WB",
                image=SimpleUploadedFile(f"{n}.png", b"x"))
        with self.assertNumQueries(3, using=DB):
            outcome_svc.by_gene("WB")

    def test_a_gene_with_no_wb_figures_says_so_rather_than_opening_empty(self):
        Target.objects.using(DB).create(gene_name="STMN2")
        resp = self.client.get(PAGE, {"gene": "STMN2"})
        self.assertEqual(resp.context["requested_gene"], "")
        self.assertIn("STMN2", resp.context["gene_note"])
        self.assertIn("no published WB figures", resp.context["gene_note"])

    def test_the_gene_a_link_carries_opens_whatever_its_case(self):
        self._antibody("ab1")
        resp = self.client.get(PAGE, {"gene": "trpa1"})
        self.assertEqual(resp.context["requested_gene"], "TRPA1")


class ThePublicLayerGoesThroughTheOneReaderTests(_Fixture):
    """The judgements reach readers now (owner, 29 Aug 2026), and the rule that
    replaced "nothing public reads this" is about *how*.

    Four public surfaces answer "what does OGA say about this antibody" — the
    gene page, the public API, the MCP and the extension index — and they got
    that wrong once already by each keeping their own copy of the rule, which is
    why ``core/recommendations.py`` exists. The capability layer is the same
    fact one level down, so it goes through the same door: a surface that read
    ``AntibodyOutcome`` itself would be the fifth copy, and it would drift.
    """

    def test_judging_still_does_not_touch_the_recommendation_flag(self):
        """The layering reads both; it does not fold one into the other. A
        judgement that silently set a recommendation would move a public verdict
        about a commercial product with nobody deciding it should."""
        ab = self._antibody("ab1")
        self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "detects", "value": "yes"}),
            content_type="application/json")
        ab.refresh_from_db()
        self.assertFalse(ab.wb_recommended)

    def test_no_public_surface_reads_the_outcome_table_directly(self):
        """Derived from the source rather than a list somebody keeps up to
        date. `core/recommendations.py` is the exception it exists to be."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        #: `core/recommendations.py` is the exception this rule exists to be —
        #: the one public reader that DERIVES a rung from the axes.
        #:
        #: `mcp_servers/seed_scratch.py` is a second entry and a different kind
        #: (14 Sep 2026). It is the local-only fixture seeder — `_guard_local_
        #: only()` refuses to run it anywhere else — and it WRITES two column
        #: values on a fixture antibody. It derives nothing, so it cannot become
        #: the fifth copy of the judgement this test is about; what it can do is
        #: make the middle rung reachable, which nothing in that fixture was
        #: until this date. A seeder that could not touch the table would leave
        #: `limited_support` untestable from the connector's suite, which is how
        #: three tests came to name a rung they never reached.
        allowed = {"core/recommendations.py", "mcp_servers/seed_scratch.py"}
        offenders = []
        for area in ("core", "mcp_servers", "browser-extension"):
            for path in (root / area).rglob("*.py"):
                rel = str(path.relative_to(root))
                if rel in allowed or "test" in path.name:
                    continue
                text = path.read_text()
                if "AntibodyOutcome" in text or "services import outcomes" in text:
                    offenders.append(rel)
        self.assertEqual(offenders, [],
                         f"these must go through core/recommendations.py: {offenders}")


class TheIFBandsComeFromTheBestConcentrationTests(_Fixture):
    """ICC-IF's one axis, derived from the WT/KO ratio with two stated cut-offs.

    Both numbers were measured against the lab's own visual calls rather than
    chosen round, and the choice of *which* ratio is the antibody's is the half
    that would be silently wrong: `best_concentration` is blank on 1,225 of
    1,817 live rows, and the obvious stand-in — take the higher of the two
    ratios — is what the lab named best only 58.2% of the time.

    **But a ratio that is not used must not vanish** (owner, 12 Sep 2026). 637
    of the 1,817 rows hold exactly one measurement — the other column blank or
    the `0` placeholder — and naming no best concentration made every one of
    them invisible. Taking them automatically was written and reverted the same
    afternoon: it moves 24 public cards to *some selective signal* and takes
    *strongly selective* off 5, with nobody deciding. So they are drawn on the
    card and gathered into a worklist, and `unused_ratio` is deliberately not
    `value` or `band`. The tests that pin the gap all use *two* measured
    ratios, which is why none of them could see the lone one.
    """

    def _if_reading(self, ab, c1="1/200", r1=None, c2="1/500", r2=None,
                    best="", specific=""):
        from pipeline.models import IfResult
        member = Member.objects.using(DB).filter(site=self.site).first()
        session = ExperimentSession.objects.using(DB).create(
            procedure_type="IF", target=self.target, site=self.site,
            experimenter=member, date=date(2026, 5, 1))
        return IfResult.objects.using(DB).create(
            session=session, antibody=ab, specific_signal=specific,
            concentration_1=c1, concentration_2=c2,
            wt_ko_ratio_1=r1, wt_ko_ratio_2=r2, best_concentration=best)

    def _published_if(self, catalogue):
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="ICC-IF",
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        return ab

    def _if_axes(self, ab):
        return outcome_svc.for_gene(self.target.pk, "ICC-IF")[ab.pk]

    def test_the_named_best_concentration_decides_which_ratio_is_used(self):
        """Not the higher one. The lab picks a working concentration on
        background and morphology, so `max()` is a coin toss — and it would
        read as a fact."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="6.0", r2="1.1", best="1/500")   # the LOWER one
        axes = self._if_axes(ab)
        self.assertEqual(str(axes["selective"]["ratio"]), "1.1000")
        self.assertEqual(axes["selective"]["band"], "no_selective_signal")

    def test_no_best_concentration_leaves_a_named_gap(self):
        """Two thirds of live rows. A band inferred from the wrong ratio is
        worse than no band, because nothing on the card would say so."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="6.0", r2="1.1", best="")
        axes = self._if_axes(ab)
        self.assertIsNone(axes["selective"]["ratio"])
        self.assertIsNone(axes["selective"]["band"])
        self.assertTrue(outcome_svc.is_gap(axes, "ICC-IF"))

    def test_ratios_with_no_best_named_are_a_different_gap_from_nothing(self):
        """"Nothing recorded" would be wrong: a session did record ratios, and
        the reason there is no band is that none of them is the one at the
        named best concentration. The card has to say which gap it is, or the
        fix — set the best concentration — is invisible."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="6.0", r2="1.1", best="")
        self.assertTrue(self._if_axes(ab)["selective"]["no_best_concentration"])

        bare = self._published_if("ab2")
        self._if_reading(bare, r1=None, r2=None, best="")
        self.assertFalse(self._if_axes(bare)["selective"]["no_best_concentration"])

    def test_a_lone_measured_ratio_is_shown_and_never_used(self):
        """637 of 1,817 live rows, and the whole of the fix. The number is on
        the card so somebody can act on it; the band is still None, because a
        derived answer appearing on 483 antibodies at once is a public
        recomputation nobody asked for."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="1.2", r2=None, best="")
        axes = self._if_axes(ab)
        self.assertEqual(str(axes["selective"]["unused_ratio"]), "1.2000")
        self.assertIsNone(axes["selective"]["ratio"])
        self.assertIsNone(axes["selective"]["band"])
        self.assertIsNone(axes["selective"]["value"])
        self.assertTrue(outcome_svc.is_gap(axes, "ICC-IF"))
        # And the half that matters most: the public capability layer reads
        # this, so an unused ratio must leave it exactly where it was.
        self.assertIsNone(outcome_svc.capability(axes, "ICC-IF"))

    def test_the_zero_placeholder_does_not_make_a_lone_ratio_a_pair(self):
        """The live shape: one real number beside the `0` sentinel. Counting
        the zero as a second measurement would make the row look ambiguous and
        hide the number again."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="1.2", r2="0", best="")
        self.assertEqual(
            str(self._if_axes(ab)["selective"]["unused_ratio"]), "1.2000")

    def test_a_best_naming_a_concentration_nobody_measured_still_shows_it(self):
        """RAB27B MA5-38617's shape: the lab named 1/500 best and recorded the
        ratio at 1/1000. The named best cannot answer, and the measurement is
        still a measurement."""
        ab = self._published_if("ab1")
        self._if_reading(ab, c1="1/1000", r1="1.2", c2="1/500", r2="0",
                         best="1/500")
        self.assertEqual(
            str(self._if_axes(ab)["selective"]["unused_ratio"]), "1.2000")

    def test_two_measurements_and_no_best_named_shows_no_number(self):
        """The caution survives exactly where it was measured — 2 live rows.
        Either number might be the wrong one, so the card asks instead of
        putting one on the screen."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="6.0", r2="1.1", best="")
        axes = self._if_axes(ab)
        self.assertIsNone(axes["selective"]["ratio"])
        self.assertIsNone(axes["selective"]["unused_ratio"])
        self.assertTrue(axes["selective"]["no_best_concentration"])

    def test_a_row_that_measured_nothing_is_not_a_missing_best(self):
        """"Set the best concentration" is the fix the card offers, and it
        fixes nothing on a row whose only ratio is the placeholder. Sending
        somebody to the sessions board for a number that was never recorded is
        a message that names the wrong control."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="0", r2="0", best="", specific="YES")
        axes = self._if_axes(ab)
        self.assertIsNone(axes["selective"]["ratio"])
        self.assertIsNone(axes["selective"]["unused_ratio"])
        self.assertFalse(axes["selective"]["no_best_concentration"])
        self.assertTrue(axes["selective"]["needs_grade"])

    def test_only_a_ratio_that_contradicts_the_record_is_worth_looking_at(self):
        """231 of the 269 the first version listed were a bench NO over a
        number also below the floor — two sources agreeing, and nothing for
        anybody to do. The pair that cannot both be right is the worklist."""
        agrees = self._published_if("ab1")
        self._if_reading(agrees, r1="1.2", r2=None, best="", specific="NO")
        self.assertNotIn(agrees.pk, outcome_svc.ratio_not_used("ICC-IF"))

        disagrees = self._published_if("ab2")
        self._if_reading(disagrees, r1="3.4", r2=None, best="", specific="NO")
        self.assertIn(disagrees.pk, outcome_svc.ratio_not_used("ICC-IF"))

    def test_a_bench_yes_is_contradicted_only_below_the_floor(self):
        """A visual call answered yes-or-no and claimed nothing about strength,
        so a ratio in the upper bands *supplies* the grade rather than
        disagreeing with it. Only a number under the floor contradicts it."""
        graded = self._published_if("ab1")
        self._if_reading(graded, r1="3.4", r2=None, best="", specific="YES")
        self.assertNotIn(graded.pk, outcome_svc.ratio_not_used("ICC-IF"))

        contradicted = self._published_if("ab2")
        self._if_reading(contradicted, r1="1.1", r2=None, best="", specific="YES")
        self.assertIn(contradicted.pk, outcome_svc.ratio_not_used("ICC-IF"))

    def test_a_judged_row_is_compared_band_for_band_and_not_left_out(self):
        """The half the first version had backwards. 234 live antibodies were
        graded by eye before the ratio was ever on the screen and 64 of those
        grades disagree with it, so leaving judged rows out hid exactly the
        rows worth a second look. A grade was picked from the three bands, so
        it is compared band for band: strongly selective over 2.1 is a real
        disagreement about how strongly, though both are positive."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="2.1", r2=None, best="", specific="YES")
        row = AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="ICC-IF",
            selective="strongly_selective")
        self.assertIn(ab.pk, outcome_svc.ratio_not_used("ICC-IF"))

        # And re-judging it to what the number says is what clears the row —
        # the comparison is against the answer in force, not the bench's.
        row.selective = "selective"
        row.save(using=DB)
        self.assertNotIn(ab.pk, outcome_svc.ratio_not_used("ICC-IF"))
        # The number stays on the card either way; settling hides nothing.
        self.assertEqual(
            str(self._if_axes(ab)["selective"]["unused_ratio"]), "2.1000")

    def test_a_row_with_nothing_recorded_is_not_a_disagreement(self):
        """A blank cannot contradict anything. Those are ordinary gaps and the
        gene view already opens filtered to them, now with the number drawn on
        the card — so nothing is stranded by narrowing the list."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="3.4", r2=None, best="")
        axes = self._if_axes(ab)
        self.assertEqual(str(axes["selective"]["unused_ratio"]), "3.4000")
        self.assertFalse(axes["selective"]["ratio_mismatch"])
        self.assertTrue(outcome_svc.is_gap(axes, "ICC-IF"))
        self.assertNotIn(ab.pk, outcome_svc.ratio_not_used("ICC-IF"))

    def test_the_unused_worklist_is_immunofluorescence_only(self):
        """It is the one application whose answer is a number, so it is the
        only one with a measurement to leave unused."""
        for application in ("WB", "IP"):
            with self.subTest(application=application):
                self.assertEqual(outcome_svc.ratio_not_used(application), {})

    def test_a_stored_zero_is_not_a_measurement(self):
        """601 of 1,817 live IF rows store 0.0000, and it is a placeholder:
        466 have no bench call, 38 sit on rows the bench called specific, and
        there is nothing at all between 0 and 0.5.

        Read as a measurement it is below every cut-off, so it banded 238
        published antibodies as "no selective signal" and caused 92 of the 101
        ratio conflicts on the review worklist. Under the owner's "the ratio
        decides" rule those would have become public negatives about named
        commercial products on the strength of an empty column.
        """
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="0", r2="0", best="1/200", specific="YES")
        axes = self._if_axes(ab)
        self.assertIsNone(axes["selective"]["ratio"],
                          "a zero must not be read as a measured ratio")
        self.assertIsNone(axes["selective"]["band"])
        # It falls back to what the bench actually said, which here is a
        # specific signal waiting to be graded.
        self.assertTrue(axes["selective"]["needs_grade"])

    def test_a_zero_does_not_manufacture_a_conflict(self):
        """The 92 false alarms. A figure nobody doubted would have been sent
        for re-judging because a column was empty."""
        ab = self._published_if("ab1")
        ab.if_recommended = True
        ab.save(using=DB, update_fields=["if_recommended"])
        self._if_reading(ab, r1="0", r2="0", best="1/200", specific="YES")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("ICC-IF"))

    def test_a_real_low_ratio_still_bands_and_still_conflicts(self):
        """Only 9 of the 101 were genuine. Those must survive the fix."""
        ab = self._published_if("ab1")
        ab.if_recommended = True
        ab.save(using=DB, update_fields=["if_recommended"])
        self._if_reading(ab, r1="1.1", r2="1.0", best="1/200")
        axes = self._if_axes(ab)
        self.assertEqual(axes["selective"]["band"], "no_selective_signal")
        self.assertIn(ab.pk, outcome_svc.conflicts("ICC-IF"))

    def test_a_best_concentration_naming_neither_tested_one_is_a_gap(self):
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="6.0", r2="1.1", best="1/9999")
        self.assertIsNone(self._if_axes(ab)["selective"]["ratio"])

    def test_the_three_bands_sit_where_the_cut_offs_say(self):
        cases = [("1.49", "no_selective_signal"), ("1.5", "selective"),
                 ("2.59", "selective"), ("2.6", "strongly_selective"),
                 ("6.0", "strongly_selective")]
        for ratio, band in cases:
            with self.subTest(ratio=ratio):
                self.assertEqual(outcome_svc.band_for(ratio), band)

    def test_the_cut_offs_are_the_measured_ones(self):
        """1.5 is where agreement with the lab's visual call peaks (90.6%);
        2.6 is the median of the ratios it called specific, and 0 of 145 it
        called non-specific reach it. A silent edit to either would move every
        public band later."""
        self.assertEqual(str(outcome_svc.SELECTIVE_FLOOR), "1.5")
        self.assertEqual(str(outcome_svc.STRONGLY_SELECTIVE), "2.6")

    def test_a_derived_band_can_be_overridden_and_the_ratio_is_kept(self):
        """The measurement is not deleted by disagreeing with it — the card
        goes on printing the ratio, so a reader can see what the judgement was
        made against."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="4.0", r2="1.1", best="1/200")
        resp = self.client.post(SAVE + "?app=ICC-IF", json.dumps(
            {"antibody_id": ab.pk, "axis": "selective",
             "value": "no_selective_signal"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._if_axes(ab)
        self.assertEqual(axes["selective"]["value"], "no_selective_signal")
        self.assertTrue(axes["selective"]["overridden"])
        self.assertEqual(str(axes["selective"]["ratio"]), "4.0000")

    def test_a_visual_yes_with_no_ratio_is_half_an_answer(self):
        """210 of the 1,148 published IF figures. It says the antibody is at
        least selective and nothing about how strongly, and reporting that as
        plain `selective` would put a measured 1.5–2.6 band and an ungraded
        "the lab said yes" under one word — the one-fact-two-answers shape this
        module exists to undo. So it stays a gap for a person to grade."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1=None, r2=None, best="", specific="YES")
        axes = self._if_axes(ab)
        self.assertTrue(axes["selective"]["needs_grade"])
        self.assertIsNone(axes["selective"]["value"])
        self.assertIsNone(outcome_svc.label(axes, "ICC-IF"))
        self.assertTrue(outcome_svc.is_gap(axes, "ICC-IF"))

    def test_a_visual_no_with_no_ratio_is_a_whole_answer(self):
        """Below the floor there is nothing to grade."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1=None, r2=None, best="", specific="NO")
        axes = self._if_axes(ab)
        self.assertEqual(axes["selective"]["value"], "no_selective_signal")
        self.assertFalse(axes["selective"]["needs_grade"])
        self.assertFalse(outcome_svc.is_gap(axes, "ICC-IF"))

    def test_an_ungraded_yes_can_be_graded_by_eye(self):
        ab = self._published_if("ab1")
        self._if_reading(ab, r1=None, r2=None, best="", specific="YES")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective",
             "value": "strongly_selective", "application": "ICC-IF"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._if_axes(ab)
        self.assertEqual(outcome_svc.label(axes, "ICC-IF"), "strongly_selective")
        self.assertEqual(axes["selective"]["source"], "review")
        # Graded by eye, so there is no ratio to print beside it — and nothing
        # should pretend there is one.
        self.assertIsNone(axes["selective"]["ratio"])

    def test_a_grade_may_contradict_the_bench(self):
        """The bench call is shown and can be disagreed with, like every other
        recorded answer on this page."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1=None, r2=None, best="", specific="YES")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective",
             "value": "no_selective_signal", "application": "ICC-IF"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = self._if_axes(ab)
        self.assertEqual(axes["selective"]["value"], "no_selective_signal")
        self.assertEqual(axes["selective"]["raw"], "YES")

    def test_if_takes_bands_and_not_a_bare_yes(self):
        """The vocabularies are per axis, not per model — a `yes` here would
        store a value `label` cannot read back as a band."""
        ab = self._published_if("ab1")
        self.assertEqual(outcome_svc.values_for("ICC-IF", "selective"),
                         ("no_selective_signal", "selective",
                          "strongly_selective", "unclear"))
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective", "value": "yes",
             "application": "ICC-IF"}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_a_fully_ungraded_row_offers_all_three_bands(self):
        ab = self._published_if("ab1")
        self._if_reading(ab, r1=None, r2=None, best="", specific="")
        data = self.client.get(CARDS, {"gene": "TRPA1", "app": "ICC-IF"}).json()
        offered = [o["value"] for o in data["axis_values"]["selective"]]
        self.assertEqual(offered[:3], list(outcome_svc.BANDS))
        self.assertFalse(data["antibodies"][0]["axes"]["selective"]["needs_grade"])

    def test_the_ratio_and_the_eye_disagreeing_is_shown(self):
        """They agree 90.6% of the time; the rest are the rows worth seeing."""
        ab = self._published_if("ab1")
        self._if_reading(ab, r1="1.1", r2="9.0", best="1/200", specific="YES")
        axes = self._if_axes(ab)
        # The ratio decides — and the band is the value, not a bare "no".
        self.assertEqual(axes["selective"]["value"], "no_selective_signal")
        self.assertTrue(axes["selective"]["conflict"])       # …and it says so

    def test_if_has_one_axis_not_two(self):
        """Nothing in the IF schema records "is there any signal at all"
        separately, so a `detects` control would be a box with no source."""
        self.assertEqual(outcome_svc.axes_for("ICC-IF"), ("selective",))
        ab = self._published_if("ab1")
        resp = self.client.post(SAVE + "?app=ICC-IF", json.dumps(
            {"antibody_id": ab.pk, "axis": "detects", "value": "yes"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)


class IPHasOneAxisTests(_Fixture):
    """`enrichment` is the whole of it. The three-fraction columns exist on the
    model and are blank in all 1,754 live rows (owner, 28 Aug 2026)."""

    def _published_ip(self, catalogue, enrichment=""):
        from pipeline.models import IpResult
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IP",
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        if enrichment:
            member = Member.objects.using(DB).filter(site=self.site).first()
            session = ExperimentSession.objects.using(DB).create(
                procedure_type="IP", target=self.target, site=self.site,
                experimenter=member, date=date(2026, 5, 1))
            IpResult.objects.using(DB).create(
                session=session, antibody=ab, enrichment=enrichment)
        return ab

    def test_enrichment_is_the_axis(self):
        self.assertEqual(outcome_svc.axes_for("IP"), ("enriches",))
        ab = self._published_ip("ab1", enrichment="YES")
        axes = outcome_svc.for_gene(self.target.pk, "IP")[ab.pk]
        self.assertEqual(axes["enriches"]["value"], "yes")
        self.assertEqual(outcome_svc.label(axes, "IP"), "enriches")

    def test_a_gap_can_be_judged(self):
        ab = self._published_ip("ab1")
        resp = self.client.post(SAVE + "?app=IP", json.dumps(
            {"antibody_id": ab.pk, "axis": "enriches", "value": "no"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        axes = outcome_svc.for_gene(self.target.pk, "IP")[ab.pk]
        self.assertEqual(outcome_svc.label(axes, "IP"), "does_not_enrich")


class EachApplicationKeepsItsOwnRowsTests(_Fixture):
    """One antibody, three applications, three independent judgements — the key
    `PublicationImage` carries. A row that leaked across would put a western
    blot's verdict on an IF figure."""

    def test_a_judgement_does_not_reach_another_application(self):
        ab = self._antibody("ab1")           # WB figure
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IP",
            image=SimpleUploadedFile("ip.png", b"x"))
        self.client.post(SAVE + "?app=IP", json.dumps(
            {"antibody_id": ab.pk, "axis": "enriches", "value": "yes"}),
            content_type="application/json")
        wb = outcome_svc.for_gene(self.target.pk, "WB")[ab.pk]
        self.assertIsNone(wb["detects"]["value"])
        self.assertEqual(
            AntibodyOutcome.objects.using(DB).filter(antibody=ab).count(), 1)

    def test_a_save_says_which_application_in_its_body(self):
        """What the page actually sends. It sent nothing at all at first, so an
        immunofluorescence judgement was checked against the antibody's western
        blot figures, refused for having none, and the card kept its old answer
        — a refusal that reads as a click that never registered."""
        ab = self._antibody("ab1")
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IP",
            image=SimpleUploadedFile("ip.png", b"x"))
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "enriches", "value": "yes",
             "application": "IP"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        row = AntibodyOutcome.objects.using(DB).get(antibody=ab)
        self.assertEqual((row.application_type, row.enriches), ("IP", "yes"))

    def test_an_axis_the_application_does_not_answer_is_refused(self):
        """`enriches` is IP's. Asking it of a western blot is a bug in the
        caller, and answering it would write a column nothing reads back."""
        ab = self._antibody("ab1")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "enriches", "value": "yes",
             "application": "WB"}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            AntibodyOutcome.objects.using(DB).filter(antibody=ab).exists())

    def test_fc_is_not_offered(self):
        """`histogram_shift` is blank on all 22 live rows, so a tab for it would
        be an empty box with no source behind it."""
        from pipeline.views.outcomes import APPLICATIONS
        self.assertNotIn("FC", APPLICATIONS)
        self.assertEqual(outcome_svc.axes_for("FC"), ())


class TheReviewListIsTheOrdinaryAmberTests(_Fixture):
    """Not recommended, and the data records on-target signal.

    `conflicts` leaves these out on purpose — 313 WB rows and 103 IP would bury
    the real disagreements — but they are the largest thing on the public site
    with no screen behind it, and the owner found IP examples on a gene page
    that the conflicts list had correctly never shown (29 Aug 2026).

    A review list, not a correction list: every row here draws the amber it
    should. The question is whether the negative is still the one somebody
    would make in front of the figure.
    """

    def _pub(self, catalogue, application, recommended, **axes):
        field = {"WB": "wb_recommended", "IP": "ip_recommended",
                 "ICC-IF": "if_recommended"}[application]
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site, **{field: recommended})
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type=application,
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type=application, **axes)
        return ab

    def test_an_ip_negative_that_enriches_is_listed(self):
        """The case the owner found on the public site and could not reach."""
        ab = self._pub("ab-ip", "IP", False, enriches="yes")
        self.assertIn(ab.pk, outcome_svc.unrecommended_but_capable("IP"))

    def test_it_answers_for_every_application_that_records_one(self):
        wb = self._pub("ab-wb", "WB", False, detects="yes", selective="no")
        icc = self._pub("ab-if", "ICC-IF", False, selective="selective")
        self.assertIn(wb.pk, outcome_svc.unrecommended_but_capable("WB"))
        self.assertIn(icc.pk, outcome_svc.unrecommended_but_capable("ICC-IF"))

    def test_a_recommended_row_is_not_on_it(self):
        ab = self._pub("ab-rec", "IP", True, enriches="yes")
        self.assertNotIn(ab.pk, outcome_svc.unrecommended_but_capable("IP"))

    def test_a_negative_that_showed_nothing_is_not_on_it(self):
        ab = self._pub("ab-none", "IP", False, enriches="no")
        self.assertNotIn(ab.pk, outcome_svc.unrecommended_but_capable("IP"))

    def test_an_unjudged_row_is_not_on_it(self):
        """Absence of an answer is not an answer."""
        ab = self._pub("ab-blank", "IP", False, enriches="")
        self.assertNotIn(ab.pk, outcome_svc.unrecommended_but_capable("IP"))

    def test_the_two_worklists_do_not_overlap(self):
        """They ask opposite questions, so a row on both would mean one of them
        is wrong about it."""
        for app in ("WB", "IP", "ICC-IF"):
            self._pub(f"a-{app}", app, False, **(
                {"detects": "yes", "selective": "no"} if app == "WB"
                else {"enriches": "yes"} if app == "IP"
                else {"selective": "selective"}))
            self.assertEqual(
                set(outcome_svc.conflicts(app))
                & set(outcome_svc.unrecommended_but_capable(app)), set(), app)

    def test_the_page_serves_it_with_its_own_heading(self):
        """The two lists count different things, so the words that fit one are
        a false claim about the other — the server sends the sentence."""
        self._pub("ab-ip2", "IP", False, enriches="yes")
        data = self.client.get(CARDS, {"review": "1", "app": "IP"}).json()
        self.assertEqual(len(data["antibodies"]), 1)
        self.assertIn("on-target signal", data["worklist_note"])
        conflicts = self.client.get(
            CARDS, {"conflicts": "1", "app": "IP"}).json()
        self.assertIn("disagree", conflicts["worklist_note"])

    def test_the_picker_counts_it(self):
        self._pub("ab-ip3", "IP", False, enriches="yes")
        self.assertEqual(
            self.client.get(GENES, {"app": "IP"}).json()["review_total"], 1)


class TheRecommendationIsSetFromTheJudgementViewTests(_Fixture):
    """Both halves of the answer, on the screen that has the figure on it.

    The recommendation was drawn here as read-only context with the note
    pointing at Set recommendations, so settling a disagreement between the
    judgement and the recommendation meant holding two screens in your head and
    losing the figure on the way (owner, 29 Aug 2026). It is the same write
    `views/recommendations.py::rec_toggle` makes — two doors to one field, and
    one field map behind both.
    """

    def _published(self, catalogue, recommended=False):
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site, wb_recommended=recommended)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB",
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        return ab

    def _post(self, **body):
        return self.client.post(RECOMMEND, json.dumps(body),
                                content_type="application/json")

    def test_it_writes_the_flag(self):
        ab = self._published("ab-rec")
        resp = self._post(antibody_id=ab.pk, value=True, application="WB")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["recommended"])
        ab.refresh_from_db()
        self.assertTrue(ab.wb_recommended)

    def test_it_stamps_when_the_recommendation_was_set(self):
        """`Antibody.save` does the stamping, which is why this saves the row
        and not the field: `update_fields` is what stopped
        `recommendations_set_at` being written from the other door."""
        ab = self._published("ab-stamp")
        self.assertIsNone(ab.recommendations_set_at)
        self._post(antibody_id=ab.pk, value=True, application="WB")
        ab.refresh_from_db()
        self.assertIsNotNone(ab.recommendations_set_at)

    def test_the_reply_carries_the_conflict_the_write_just_created(self):
        """A recommendation and a judgement can each create or settle a
        disagreement with the other, so a reply carrying only the half that
        moved would leave the note on screen saying what was true a moment
        ago."""
        ab = self._published("ab-note")
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="WB", detects="yes", selective="no")
        self.assertIsNone(self._post(
            antibody_id=ab.pk, value=False,
            application="WB").json()["conflict_direction"])
        self.assertEqual(
            self._post(antibody_id=ab.pk, value=True,
                       application="WB").json()["conflict_direction"],
            "supportive_but_not_selective")

    def test_it_settles_a_conflict_as_well_as_making_one(self):
        ab = self._published("ab-settle", recommended=True)
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="WB", detects="no")
        self.assertIn(ab.pk, outcome_svc.conflicts("WB"))
        self._post(antibody_id=ab.pk, value=False, application="WB")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("WB"))

    def test_an_unpublished_antibody_is_refused_by_name(self):
        ab = self._antibody("ab-unpub", published=False)
        resp = self._post(antibody_id=ab.pk, value=True, application="WB")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ab-unpub", resp.json()["error"])

    def test_a_recommendation_is_yes_or_no_and_nothing_else(self):
        """A missing or string `value` used to be `bool("false") is True`
        territory. It is refused rather than coerced: this field is the public
        verdict on a named commercial product."""
        ab = self._published("ab-bad")
        for value in ("true", 1, None):
            resp = self._post(antibody_id=ab.pk, value=value, application="WB")
            self.assertEqual(resp.status_code, 400, value)
        ab.refresh_from_db()
        self.assertFalse(ab.wb_recommended)

    def test_a_missing_antibody_is_refused(self):
        self.assertEqual(
            self._post(antibody_id=99999, value=True,
                       application="WB").status_code, 404)

    def test_the_card_offers_it_as_a_control(self):
        ab = self._published("ab-card", recommended=True)
        row = self.client.get(CARDS, {"gene": "TRPA1"}).json()["antibodies"][0]
        self.assertEqual(row["id"], ab.pk)
        self.assertTrue(row["recommended"])


class TheConflictWorklistTests(_Fixture):
    """Where the recommendation and the recorded data disagree.

    On live data (29 Aug 2026) that is 113 ICC-IF, 20 IP and a handful of WB —
    spread across 154 genes, so working them a gene at a time means opening a
    page per gene to find one or two rows. This is the worklist.

    What is pinned is which disagreements count. The ordinary qualified
    negative — not recommended, with on-target signal — is 313 WB
    rows and 103 IP rows, and listing those here would bury the real ones.
    """

    def _if(self, catalogue, recommended, band):
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site, if_recommended=recommended)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="ICC-IF",
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="ICC-IF", selective=band)
        return ab

    def _wb(self, catalogue, recommended, detects):
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site, wb_recommended=recommended)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB",
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="WB", detects=detects)
        return ab

    def _wb_pair(self, catalogue, recommended, detects, selective):
        ab = Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site, wb_recommended=recommended)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="WB",
            image=SimpleUploadedFile(f"{catalogue}.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="WB",
            detects=detects, selective=selective)
        return ab

    def test_recommended_and_not_selective_is_a_standing_check(self):
        """Not a contradiction the way the first one is — the gene page prints
        `Supportive — detects the target, but is not selective` and means it — but it is the pair that
        most often turns out to be a rating nobody has revisited. ab190355 on
        ATP2B1 was exactly this shape: two runs recorded NO, the published blot
        shows one clean band, and it was re-judged from the figure (owner,
        29 Aug 2026)."""
        ab = self._wb_pair("ab-nonsel", True, "yes", "no")
        self.assertEqual(outcome_svc.conflicts("WB")[ab.pk]["direction"],
                         "supportive_but_not_selective")

    def test_recommended_and_selective_is_not(self):
        ab = self._wb_pair("ab-sel", True, "yes", "yes")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("WB"))

    def test_not_recommended_and_not_selective_is_not(self):
        """The verdict and the data agree — this is the ordinary amber, 313 WB
        rows of it, and listing those would bury the rows worth looking at."""
        ab = self._wb_pair("ab-agree", False, "yes", "no")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("WB"))

    def test_showing_nothing_outranks_not_being_selective(self):
        """Both are true of a row that detects nothing and is flagged anyway.
        The stronger statement wins, or the worklist would report the milder
        one and the reader would judge a different question."""
        ab = self._wb_pair("ab-neither", True, "no", "no")
        self.assertEqual(outcome_svc.conflicts("WB")[ab.pk]["direction"],
                         "flagged_but_not_capable")

    def test_only_western_blot_asks_it(self):
        """ICC-IF's selectivity axis *is* its capability axis, so the same
        shape is already the first direction there; IP records no selectivity
        at all. This is the data, not a choice about which to bother with."""
        ab = self._if("ab-if-sel", True, "selective")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("ICC-IF"))

    def test_recommended_but_the_data_says_otherwise_is_a_conflict(self):
        """A scientist judged it worth using and the bench record says it did
        not clear the floor. Worth a human look in any application."""
        ab = self._if("ab1", True, "no_selective_signal")
        self.assertIn(ab.pk, outcome_svc.conflicts("ICC-IF"))
        self.assertEqual(
            outcome_svc.conflicts("ICC-IF")[ab.pk]["direction"],
            "flagged_but_not_capable")

    def test_an_ordinary_qualified_negative_is_not_a_conflict(self):
        """313 WB rows are 'not recommended, and it does detect'. That is the
        designed middle state, not a disagreement — listing them would bury the
        rows that need somebody."""
        ab = self._wb("ab1", False, "yes")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("WB"))

    def test_clearing_the_floor_is_not_a_conflict_it_is_the_amber(self):
        """Corrected 29 Aug 2026, and this is the correction.

        Not recommended, and the ratio clears 1.5, is the ordinary qualified
        negative — "not supportive, however some selective signal". Clearing
        the floor does not make the data supportive; the ratio **vetoes** a
        supportive verdict below the floor and never confers one above it.
        Reading it the other way listed 58 ordinary ambers as conflicts and
        sent somebody to re-judge figures that were saying the right thing.
        """
        ab = self._if("ab1", False, "selective")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("ICC-IF"))

    def test_the_evidence_at_its_strongest_and_still_no_recommendation(self):
        """The second review group, added once the first had been worked
        through. Strongly selective and not recommended: everything ICC-IF
        records is positive and the verdict is still negative. That may be
        deliberate — a judgement can rest on something no column holds — but it
        is 5 rows, small enough to confirm rather than assume."""
        ab = self._if("ab1", False, "strongly_selective")
        self.assertEqual(outcome_svc.conflicts("ICC-IF")[ab.pk]["direction"],
                         "strongest_but_not_flagged")

    def test_on_wb_the_strongest_evidence_is_both_axes(self):
        """There is no "strongly" on a western blot; detects *and* selective is
        everything its two axes record. 20 live rows are that and not
        recommended."""
        ab = self._wb("ab1", False, "yes")
        AntibodyOutcome.objects.using(DB).filter(
            antibody=ab, application_type="WB").update(selective="yes")
        self.assertEqual(outcome_svc.conflicts("WB")[ab.pk]["direction"],
                         "strongest_but_not_flagged")

    def test_ip_has_no_strongest_evidence_to_disagree_with(self):
        """Its flag is the finer judgement — enriched *significantly* — on its
        only axis, so "enriches but not flagged" is the ordinary amber and there
        is nothing stronger for a negative to contradict."""
        ab = Antibody.objects.using(DB).create(
            catalogue_number="abIP", target=self.target, company=self.company,
            site=self.site, ip_recommended=False)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="IP",
            image=SimpleUploadedFile("ip.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=ab, application_type="IP", enriches="yes")
        self.assertNotIn(ab.pk, outcome_svc.conflicts("IP"))

    def test_agreement_is_never_listed(self):
        for catalogue, rec, band in (("ab1", True, "selective"),
                                     ("ab2", False, "no_selective_signal")):
            self._if(catalogue, rec, band)
        self.assertEqual(outcome_svc.conflicts("ICC-IF"), {})

    def test_an_unanswered_axis_is_not_a_conflict(self):
        """Absence of a judgement is not disagreement with one."""
        ab = Antibody.objects.using(DB).create(
            catalogue_number="ab1", target=self.target, company=self.company,
            site=self.site, if_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=ab, application_type="ICC-IF",
            image=SimpleUploadedFile("x.png", b"x"))
        self.assertEqual(outcome_svc.conflicts("ICC-IF"), {})

    def test_the_worklist_spans_every_gene(self):
        """The whole reason it is not the gene grid."""
        other = Target.objects.using(DB).create(gene_name="STMN2")
        far = Antibody.objects.using(DB).create(
            catalogue_number="abFAR", target=other, company=self.company,
            site=self.site, if_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=far, application_type="ICC-IF",
            image=SimpleUploadedFile("far.png", b"x"))
        AntibodyOutcome.objects.using(DB).create(
            antibody=far, application_type="ICC-IF",
            selective="no_selective_signal")
        near = self._if("abNEAR", True, "no_selective_signal")

        data = self.client.get(CARDS, {"conflicts": "1", "app": "ICC-IF"}).json()
        self.assertTrue(data["conflicts_view"])
        names = {r["name"] for r in data["antibodies"]}
        self.assertEqual(names, {"abFAR", "abNEAR"})
        # Each row says which gene it belongs to, or a cross-gene list is
        # unreadable.
        self.assertEqual({r["gene"] for r in data["antibodies"]},
                         {"STMN2", "TRPA1"})

    def test_the_picker_counts_them(self):
        self._if("ab1", True, "no_selective_signal")
        data = self.client.get(GENES, {"app": "ICC-IF"}).json()
        self.assertEqual(data["conflict_total"], 1)

    def test_a_conflict_card_is_judgeable_like_any_other(self):
        """It is the same card — the point is to re-judge it in front of the
        figure, not to read a list and go somewhere else."""
        ab = self._if("ab1", True, "no_selective_signal")
        row = self.client.get(
            CARDS, {"conflicts": "1", "app": "ICC-IF"}).json()["antibodies"][0]
        self.assertEqual(row["conflict_direction"], "flagged_but_not_capable")
        resp = self.client.post(SAVE, json.dumps(
            {"antibody_id": ab.pk, "axis": "selective",
             "value": "selective", "application": "ICC-IF"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        # Judged the other way, it leaves the list.
        self.assertEqual(outcome_svc.conflicts("ICC-IF"), {})
