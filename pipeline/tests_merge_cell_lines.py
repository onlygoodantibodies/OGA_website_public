"""Merging two CellLine rows that are one line.

The loud failure — a refusal, a bad id — is not what this guards. What it
guards is the merge that *appears* to work: four of the eight foreign keys
pointing at `CellLine` cascade, so a delete before the move takes the losing
row's freeze-down batch with it, and the C-number written on those tubes stops
resolving. Nothing on any screen would say so; the freezer would simply read as
one line with half its vials.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pipeline.models import (
    Site, Target, CellLine, CellLineVial, ExperimentSession, Member,
)
from django.contrib.auth.models import User

DB = "pipeline_db"


class MergeCellLinesTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.other = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.gene = Target.objects.using(DB).create(
            protein_name="Glucosylceramidase", gene_name="GBA1",
            site_id=self.site.pk)

        # The two RPE-1 rows: one line, entered twice.
        self.old = self._line("RPEI", c_number=100, catalogue="CRL-4000",
                              origin="ATCC")
        self.new = self._line("RPE-1", c_number=690, origin="Academic Partner")

    def _line(self, name, *, c_number, catalogue="", origin="",
              genotype="WT", target_id=None, site=None):
        line = CellLine.objects.using(DB).create(
            name=name, genotype=genotype, target_id=target_id,
            c_number=c_number, catalogue_number=catalogue, origin=origin,
            site_id=(site or self.site).pk)
        CellLineVial.objects.using(DB).create(
            cell_line_id=line.pk, c_number=c_number, site_id=line.site_id)
        return line

    def _run(self, **kwargs):
        out = StringIO()
        call_command("merge_cell_lines", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    # ------------------------------------------------------------------
    def test_both_c_numbers_survive_as_batches(self):
        """The whole point. Two receipts of one line are two freeze-down
        batches under one record, and both numbers are written on tubes.
        """
        self._run(loser=str(self.old.pk), winner=str(self.new.pk), apply=True)
        self.assertFalse(CellLine.objects.using(DB).filter(pk=self.old.pk).exists())
        survivor = CellLine.objects.using(DB).get(pk=self.new.pk)
        numbers = sorted(CellLineVial.objects.using(DB)
                         .filter(cell_line_id=survivor.pk)
                         .values_list("c_number", flat=True))
        self.assertEqual(numbers, [100, 690])

    def test_a_session_keeps_the_line_it_was_run_against(self):
        """`ExperimentSession.cell_line_wt` is SET_NULL, so a delete would blank
        it and never mention it — a count of what goes cannot see a nulled row.
        """
        user = User.objects.using(DB).create(username="someone")
        member = Member.objects.using(DB).create(
            user_id=user.pk, site_id=self.site.pk, role="experimenter",
            display_name="Someone")
        session = ExperimentSession.objects.using(DB).create(
            procedure_type="WB", target_id=self.gene.pk,
            experimenter_id=member.pk, date="2026-04-30",
            site_id=self.site.pk, cell_line_wt_id=self.old.pk)
        self._run(loser=str(self.old.pk), winner=str(self.new.pk), apply=True)
        session.refresh_from_db(using=DB)
        self.assertEqual(session.cell_line_wt_id, self.new.pk)

    def test_what_the_survivor_lacks_is_filled_in(self):
        """Keeping the newer row must not lose the ATCC catalogue number the
        older one carried.
        """
        self._run(loser=str(self.old.pk), winner=str(self.new.pk), apply=True)
        survivor = CellLine.objects.using(DB).get(pk=self.new.pk)
        self.assertEqual(survivor.catalogue_number, "CRL-4000")
        # And nothing already recorded is overwritten.
        self.assertEqual(survivor.origin, "Academic Partner")

    def test_a_dry_run_changes_nothing(self):
        output = self._run(loser=str(self.old.pk), winner=str(self.new.pk))
        self.assertTrue(CellLine.objects.using(DB).filter(pk=self.old.pk).exists())
        self.assertIn("DRY RUN", output)

    # ------------------------------------------------------------------
    def test_a_wild_type_and_a_knockout_are_not_one_line(self):
        ko = self._line("RPE-1", c_number=701, genotype="KO",
                        target_id=self.gene.pk)
        with self.assertRaises(CommandError) as caught:
            self._run(loser=str(ko.pk), winner=str(self.new.pk), apply=True)
        self.assertIn("different reagents", str(caught.exception))

    def test_two_benches_sharing_a_name_are_not_a_duplicate(self):
        """HAP1 names hundreds of rows and the site is what tells them apart."""
        theirs = self._line("RPE-1", c_number=1, site=self.other)
        with self.assertRaises(CommandError) as caught:
            self._run(loser=str(theirs.pk), winner=str(self.new.pk), apply=True)
        self.assertIn("different benches", str(caught.exception))

    def test_a_line_can_be_named_by_the_number_on_its_tube(self):
        self._run(loser="C-100", winner="C-690", apply=True)
        self.assertFalse(CellLine.objects.using(DB).filter(pk=self.old.pk).exists())

    def test_an_own_number_no_batch_carries_is_rescued(self):
        """`CellLine.c_number` and `CellLineVial.c_number` are both written on
        tubes. A row whose own number is on no batch would lose it when the row
        goes, and 219 of 746 live vial numbers sit on no CellLine row — the two
        tables genuinely disagree.
        """
        CellLineVial.objects.using(DB).filter(cell_line_id=self.old.pk).delete()
        output = self._run(loser=str(self.old.pk), winner=str(self.new.pk),
                           apply=True)
        numbers = sorted(CellLineVial.objects.using(DB)
                         .filter(cell_line_id=self.new.pk)
                         .values_list("c_number", flat=True))
        self.assertEqual(numbers, [100, 690])
        self.assertIn("C-100", output)
