"""Freeze-down batches: adding one, and showing the ones already there.

The lab freezes a line down, the batch takes the site's next C-number, and that
number is written on every tube in it. `CellLineVial` is that batch. Until now
nothing could create one except `bulk_cell_lines._ensure_vial`, reached only by
pasting a sheet — so recording a second batch meant re-pasting the line with a
different number, indistinguishable from correcting a typo.

Two things here would be silently wrong rather than loud, which is why they are
pinned:

* **One batch is one number.** The quantity typed is how many vials the
  freeze-down produced; they share the number. A number per vial would burn the
  rest of the run, and `next_number` never fills a gap — the box it was written
  on may still be in the freezer.
* **A range over a line's batches is a lie on half the live data.** C-numbers are
  issued per site in freeze order, not per line, so HeLa's 17 batches are
  15, 16, 79, 113, 422 … 742. `C-15–C-742` claims 728 numbers for a line that
  owns 17.
"""
from __future__ import annotations

from datetime import date

from django.test import TestCase

from pipeline.models import CellLine, CellLineVial, Member, Site
from pipeline.services import batches, cell_line_board
from pipeline.services import cell_lines as cell_lines_svc
from pipeline.tests_timeouts import DB, _member_client


class FormatBatchesTests(TestCase):
    """Pure, so it costs nothing and covers the case the live data is full of."""

    def test_one_batch_is_just_its_number(self):
        self.assertEqual(cell_lines_svc.format_batches([23]), "C-23")

    def test_three_consecutive_collapse_to_a_range(self):
        self.assertEqual(cell_lines_svc.format_batches([23, 24, 25]), "C-23–C-25")

    def test_two_consecutive_are_listed_not_ranged(self):
        """`C-23–C-24` is longer than `C-23, C-24` and says nothing more."""
        self.assertEqual(cell_lines_svc.format_batches([23, 24]), "C-23, C-24")

    def test_a_gappy_history_is_never_shown_as_one_range(self):
        """HeLa's real shape, trimmed. The whole point of the rule."""
        got = cell_lines_svc.format_batches([15, 16, 79, 113, 422])
        self.assertEqual(got, "C-15, C-16, C-79, C-113, C-422")
        self.assertNotIn("C-15–C-422", got)

    def test_runs_inside_a_gappy_history_still_collapse(self):
        self.assertEqual(
            cell_lines_svc.format_batches([15, 16, 17, 79, 240, 241, 242]),
            "C-15–C-17, C-79, C-240–C-242")

    def test_no_batches_is_empty_not_a_dash(self):
        self.assertEqual(cell_lines_svc.format_batches([]), "")


class AddBatchTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.other = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.line = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk, c_number=40)

    def test_one_press_adds_one_batch_with_one_number(self):
        """Six vials is one batch and one C-number, not six numbers."""
        before = CellLineVial.objects.using(DB).count()
        out = batches.add(self.line, member=self.member, vial_count=6)
        self.assertTrue(out["ok"], out)
        self.assertEqual(CellLineVial.objects.using(DB).count(), before + 1)
        batch = CellLineVial.objects.using(DB).get(pk=out["batch_id"])
        self.assertEqual(batch.vial_count, 6)
        self.assertEqual(batch.c_number, 41, "the number after the line's own")

    def test_the_number_is_issued_by_lab_numbers_not_by_this_path(self):
        """A batch created anywhere gets the site's next number, because the
        issuer is a pre_save receiver rather than eight write paths."""
        batch = CellLineVial(cell_line_id=self.line.pk, site_id=self.site.pk)
        batch.save(using=DB)
        self.assertEqual(batch.c_number, 41)

    def test_the_receipt_names_the_number_that_was_issued(self):
        out = batches.add(self.line, member=self.member, vial_count=3)
        self.assertIn("C-41", out["message"])
        self.assertIn("3 vials", out["message"])
        self.assertTrue(out["issued"])

    def test_the_check_says_a_number_will_be_given_without_promising_which(self):
        seen = batches.preview(self.line, vial_count=6)
        self.assertTrue(seen["will_be_numbered"])
        self.assertIn("next C-number", seen["note"])
        self.assertNotIn("C-41", seen["note"], "the check promised a number")
        self.assertIn("6 vials", seen["note"])
        self.assertIn("share that one number", seen["note"])

    def test_a_typed_number_is_kept(self):
        out = batches.add(self.line, member=self.member, c_number="C-900")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["c_number"], "C-900")
        self.assertFalse(out["issued"])

    def test_a_typed_number_somebody_already_holds_is_refused_by_name(self):
        out = batches.add(self.line, member=self.member, c_number="C-40")
        self.assertFalse(out["ok"])
        self.assertIn("C-40", out["error"])
        self.assertIn("already", out["error"])

    def test_an_unreadable_number_is_refused_with_what_is_accepted(self):
        out = batches.add(self.line, member=self.member, c_number="C-RUN11-01")
        self.assertFalse(out["ok"])
        self.assertIn("C-RUN11-01", out["error"])

    def test_a_vial_count_that_is_not_a_number_is_refused_by_name(self):
        out = batches.add(self.line, member=self.member, vial_count="a few")
        self.assertFalse(out["ok"])
        self.assertIn("a few", out["error"])

    def test_another_sites_line_is_refused(self):
        theirs = CellLine.objects.using(DB).create(
            name="U2OS", genotype="WT", site_id=self.other.pk)
        out = batches.add(theirs, member=self.member, vial_count=1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"])

    def test_a_line_with_no_site_is_refused_because_the_run_is_the_sites(self):
        homeless = CellLine.objects.using(DB).create(name="X", genotype="WT")
        out = batches.add(homeless, member=self.member, vial_count=1)
        self.assertFalse(out["ok"])
        self.assertIn("no site", out["error"])

    def test_the_lines_own_column_is_filled_only_when_blank(self):
        """It is the Access-era bridge carrying the *first* batch's number. The
        tube it names is still in the freezer, so a later batch never moves it."""
        blank = CellLine.objects.using(DB).create(
            name="RPE1", genotype="WT", site_id=self.site.pk)
        lab_first = blank.c_number          # issued on save
        batches.add(blank, member=self.member, vial_count=1)
        blank.refresh_from_db()
        self.assertEqual(blank.c_number, lab_first, "the bridge moved")

        batches.add(self.line, member=self.member, vial_count=1)
        self.line.refresh_from_db()
        self.assertEqual(self.line.c_number, 40, "a later batch moved the line's number")


class TheBoardShowsEveryBatchTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def test_a_row_lists_its_batches_so_a_search_hit_explains_itself(self):
        """Searching a second batch's number returns the row; without this the
        row shows only the *first* batch's number and reads as a wrong result."""
        site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        line = CellLine.objects.using(DB).create(
            name="HeLa", genotype="WT", site_id=site.pk, c_number=15)
        for n in (16, 79, 113):
            CellLineVial.objects.using(DB).create(
                cell_line_id=line.pk, c_number=n, site_id=site.pk)

        row = cell_line_board.row_for(line)
        self.assertEqual(row["c_number"], "C-15")
        self.assertEqual(row["batches"], "C-15, C-16, C-79, C-113")
        self.assertIsInstance(row["batches"], str, "board rows must be JSON")
