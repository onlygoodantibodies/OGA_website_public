"""Merging two Site rows that are the same real place.

"McGill" and "Montreal" are one lab under two names — the Access import used one,
everything since used the other. Overview reports the lab twice, the site filter
on all four boards splits its records, and a sheet uploaded under the wrong name
lands somewhere that looks empty.

This is a live-data command, so what is pinned is the shape of its safety: it is
a dry run unless told otherwise, it finds the foreign keys itself rather than
from a list that will go stale, and it refuses to turn two records into one.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             Member, Site, Target)
from pipeline.tests_timeouts import DB, _member_client


class MergeSitesTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.mcgill = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.montreal = Site.objects.using(DB).create(name="Montreal", short_code="MTL")
        self.client = _member_client(self, self.mcgill)
        self.member = Member.objects.using(DB).get(site_id=self.mcgill.pk)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")
        self.company = Company.objects.using(DB).create(name="Proteintech")
        # Montreal holds the real records; McGill holds the Access-era target.
        self.antibody = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="10586-1-AP", lot_number="20051",
            site_id=self.montreal.pk)
        self.line = CellLine.objects.using(DB).create(
            name="HAP1 STMN2 KO", genotype="KO", target_id=self.target.pk,
            site_id=self.montreal.pk)
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-07-30",
            site_id=self.montreal.pk, experimenter_id=self.member.pk)

    def _run(self, **kw):
        out = StringIO()
        call_command("merge_sites", "--from", "Montreal", "--into", "McGill",
                     stdout=out, **kw)
        return out.getvalue()

    def test_a_dry_run_is_the_default_and_moves_nothing(self):
        out = self._run()
        self.assertIn("DRY RUN", out)
        self.assertEqual(
            Antibody.objects.using(DB).get(pk=self.antibody.pk).site_id,
            self.montreal.pk)
        self.assertTrue(Site.objects.using(DB).filter(pk=self.montreal.pk).exists())

    def test_the_dry_run_counts_what_would_move(self):
        out = self._run()
        for word in ("Antibodies", "Cell Lines", "Experiment Sessions"):
            self.assertIn(word, out)

    def test_applying_moves_every_record_and_removes_the_empty_site(self):
        self._run(apply=True)
        for obj in (self.antibody, self.line, self.session):
            obj.refresh_from_db(using=DB)
            self.assertEqual(obj.site_id, self.mcgill.pk)
        self.assertFalse(Site.objects.using(DB).filter(pk=self.montreal.pk).exists())

    def test_it_finds_the_foreign_keys_rather_than_carrying_a_list(self):
        """Fifteen models point at Site today. A hand-written list would be
        wrong within a month, and a model left behind would point at a site row
        that no longer exists."""
        from pipeline.management.commands.merge_sites import _site_fks
        found = {m.__name__ for m, _f in _site_fks()}
        for expected in ("Antibody", "CellLine", "ExperimentSession", "Member",
                         "Target", "TargetNomination", "TargetAssignment"):
            self.assertIn(expected, found)

    def test_a_record_that_already_exists_at_the_survivor_is_left_alone(self):
        """unique_antibody_per_site_lot includes the site, so moving a vial onto
        a site that already has the same one would break the constraint. Deciding
        two rows are one antibody is a merge, and merges have their own command,
        their own evidence and their own dry run."""
        twin = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="10586-1-AP", lot_number="20051",
            site_id=self.mcgill.pk)
        out = self._run()
        self.assertIn("held back", out)
        self.assertIn("merge_duplicate_antibodies", out)

        self._run(apply=True)
        # The clashing row stays put; everything else still moves.
        self.assertEqual(
            Antibody.objects.using(DB).get(pk=self.antibody.pk).site_id,
            self.montreal.pk)
        self.assertEqual(
            Antibody.objects.using(DB).get(pk=twin.pk).site_id, self.mcgill.pk)
        self.session.refresh_from_db(using=DB)
        self.assertEqual(self.session.site_id, self.mcgill.pk)
        # And the site survives, because it is not empty.
        self.assertTrue(Site.objects.using(DB).filter(pk=self.montreal.pk).exists())

    def test_an_unknown_site_name_names_the_ones_that_exist(self):
        with self.assertRaises(CommandError) as caught:
            call_command("merge_sites", "--from", "Atlantis", "--into", "McGill")
        self.assertIn("McGill", str(caught.exception))
        self.assertIn("Montreal", str(caught.exception))

    def test_it_refuses_to_merge_a_site_into_itself(self):
        with self.assertRaises(CommandError):
            call_command("merge_sites", "--from", "McGill", "--into", "McGill")

    def test_members_move_too_so_nobody_is_left_at_a_deleted_site(self):
        """Member.site is PROTECT, so a member left behind would block the
        delete outright — and a curator whose site row had gone would be locked
        out of every board."""
        from django.contrib.auth.models import User
        other = User.objects.using(DB).create(username="sara")
        mtl_member = Member.objects.using(DB).create(
            user_id=other.pk, site_id=self.montreal.pk,
            role="experimenter", is_active=True)
        self._run(apply=True)
        mtl_member.refresh_from_db(using=DB)
        self.assertEqual(mtl_member.site_id, self.mcgill.pk)
        self.assertFalse(Site.objects.using(DB).filter(pk=self.montreal.pk).exists())

    def test_the_board_filter_then_shows_the_lab_once(self):
        """The point of the exercise: one lab, one entry in the site filter."""
        self._run(apply=True)
        body = self.client.get("/pipeline/antibodies/board/").content.decode()
        self.assertIn("McGill", body)
        self.assertNotIn(">Montreal<", body)
