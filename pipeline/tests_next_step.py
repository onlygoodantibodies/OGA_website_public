"""A pointer to the next step, on whichever board you are looking at.

The owner's review: *"Once someone has added the target, there should probably
be a way of guiding them sequentially to each step they need to do -> add cell
lines -> add antibodies -> plan experiments/ download worksheets -> upload
worksheets/ record results. This is not a demand for a complete redesign, just a
request to a pointer to the next step for each target."*

Everything is derived through `services/gene_progress.py`, so the strip cannot
disagree with the gene's own page — which is the failure this codebase keeps
paying for (`Target.status`, `Target.ko_validated`, Overview's Active count).
"""
from __future__ import annotations

from django.test import TestCase

from pipeline.models import Antibody, CellLine, Company, Site, Target, TargetNomination
from pipeline.services import gene_progress, next_step
from pipeline.tests_timeouts import DB, _member_client

BOARDS = ("/pipeline/antibodies/board/", "/pipeline/cell-lines/board/",
          "/pipeline/sessions/board/", "/pipeline/targets/board/")


class TheStripAppearsForOneGeneTests(TestCase):
    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="SNCA",
                                                     protein_name="Syn")

    def test_every_board_shows_it_when_filtered_to_a_gene(self):
        for url in BOARDS:
            with self.subTest(url=url):
                body = self.client.get(url, {"gene": "SNCA"}).content.decode()
                self.assertIn('data-next-step="SNCA"', body)
                self.assertIn("Next:", body)

    def test_no_gene_filter_means_no_strip(self):
        """"The next step" names a single thing or it names nothing — over an
        unfiltered board it would be about whichever gene sorted first."""
        for url in BOARDS:
            with self.subTest(url=url):
                self.assertNotIn("data-next-step",
                                 self.client.get(url).content.decode())

    def test_a_gene_that_is_not_on_file_shows_nothing_rather_than_an_empty_strip(self):
        body = self.client.get(BOARDS[0], {"gene": "NOTAGENE"}).content.decode()
        self.assertNotIn("data-next-step", body)

    def test_na_is_not_a_gene(self):
        """96 wild types point at the placeholder `NA` target. A progress strip
        for something that is not a gene would be nonsense."""
        Target.objects.using(DB).create(gene_name="NA", protein_name="Not applicable")
        body = self.client.get(BOARDS[0], {"gene": "NA"}).content.decode()
        self.assertNotIn("data-next-step", body)
        self.assertEqual(next_step.context("NA"), {})


class ItSaysTheSameThingAsTheGenesOwnPageTests(TestCase):
    """One reader, so the two cannot drift — which is exactly how
    `Target.status` came to say Not Started beside a strip showing two
    procedures run."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="SNCA")

    def test_the_strip_walks_forward_as_records_appear(self):
        ctx = next_step.context("SNCA")
        self.assertEqual(ctx["next_step"]["key"], "nominated")

        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)
        self.assertEqual(next_step.context("SNCA")["next_step"]["key"], "ko_line")

        wt = CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                               site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk,
            parent_line_id=wt.pk, site_id=self.site.pk)
        self.assertNotEqual(next_step.context("SNCA")["next_step"]["key"], "ko_line")

    def test_it_is_the_same_function_the_gene_page_reads(self):
        ctx = next_step.context("SNCA")
        page_steps = gene_progress.steps_for(self.target)
        self.assertEqual([s["key"] for s in ctx["next_step_steps"]],
                         [s["key"] for s in page_steps])
        self.assertEqual(ctx["next_step_headline"],
                         gene_progress.headline(page_steps))

    def test_nothing_reads_a_stored_status(self):
        from pathlib import Path
        src = Path("pipeline/services/next_step.py").read_text()
        self.assertNotIn("target.status", src)
        self.assertNotIn("Target.Status", src)


class TheStripDoesNotPretendToBeLiveTests(TestCase):
    """Adding a cell line on the very board it sits above changes what it should
    say, and nothing redraws a server-rendered strip. CLAUDE.md: never tell the
    reader to reload — offer the button instead."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        Target.objects.using(DB).create(gene_name="SNCA")

    def test_it_offers_a_refresh_rather_than_an_instruction(self):
        body = self.client.get(BOARDS[0], {"gene": "SNCA"}).content.decode()
        self.assertIn('id="next-step-refresh"', body)
        for nagging in ("Reload the page", "reload the page", "refresh the page"):
            self.assertNotIn(nagging, body)


class TheSequenceIsTheOrderTheWorkHappensInTests(TestCase):
    """Owner, 4 Aug, looking at TRPA1: *"KO confirmation happens after WB. and
    WB needs to happen first usually. but antibodies and cell lines are needed
    prior."*

    The strip had **KO confirmed** third, before the antibodies. So a gene with
    a knockout line and nothing else read *"Next: KO confirmed"* — a thing that
    cannot be done until there is an antibody to blot with and a WB to run it
    in. Confirmation is an *output* of the first western blot, not a
    prerequisite for it, and a sequence that says otherwise reads as the app not
    understanding the bench.
    """

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)

    def _keys(self):
        from pipeline.services import gene_progress
        return [s["key"] for s in gene_progress.steps_for(self.target)]

    def test_ko_confirmed_comes_after_the_western_blot(self):
        keys = self._keys()
        for earlier, later in (("ko_line", "antibodies"),
                               ("antibodies", "app_WB"),
                               ("app_WB", "ko_validated"),
                               ("ko_validated", "app_IP")):
            with self.subTest(pair=(earlier, later)):
                self.assertLess(keys.index(earlier), keys.index(later))

    def test_every_step_is_still_there_exactly_once(self):
        """Moving one is not dropping one — and the WB anchor is read out of
        `APPLICATIONS` rather than hard-coded, so this is what would catch a
        rename putting the step back on the end."""
        keys = self._keys()
        self.assertEqual(len(keys), len(set(keys)))
        for key in ("nominated", "ko_line", "antibodies", "app_WB",
                    "ko_validated", "app_IP", "app_IF", "app_FC", "reported"):
            self.assertIn(key, keys)

    def test_the_screenshots_gene_is_told_to_get_antibodies(self):
        """TRPA1: nominated, one knockout line, no antibodies. It said
        "Next: KO confirmed"."""
        from pipeline.models import CellLine
        from pipeline.services import gene_progress
        CellLine.objects.using(DB).create(
            name="U2OS", genotype="KO", target_id=self.target.pk,
            site_id=self.site.pk)
        nxt = gene_progress.next_step(gene_progress.steps_for(self.target))
        self.assertEqual(nxt["key"], "antibodies")

    def test_once_the_blot_is_run_it_asks_for_the_confirmation(self):
        from pipeline.models import (Antibody, CellLine, Company,
                                     ExperimentSession, Member, WbResult)
        from pipeline.services import gene_progress
        member = Member.objects.using(DB).get(site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="U2OS", genotype="KO", target_id=self.target.pk,
            site_id=self.site.pk)
        company = Company.objects.using(DB).create(name="Abcam")
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab1", site_id=self.site.pk)
        session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=member.pk)
        WbResult.objects.using(DB).create(session_id=session.pk,
                                          antibody_id=ab.pk)
        nxt = gene_progress.next_step(gene_progress.steps_for(self.target))
        self.assertEqual(nxt["key"], "ko_validated")

    def test_the_hint_says_the_blot_is_what_confirms_it(self):
        from pipeline.services import gene_progress
        step = [s for s in gene_progress.steps_for(self.target)
                if s["key"] == "ko_validated"][0]
        self.assertIn("western blot", step["hint"])
