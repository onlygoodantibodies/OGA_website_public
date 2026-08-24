"""Merging two Member rows that are the same person.

The defect worth pinning is not the merge failing — that is loud. It is the
merge succeeding onto the **wrong row**: a scientist's work moved onto the
`access_` identity the historical import minted, whose password is deliberately
unusable. Everything looks right until they try to sign in, and by then the
sessions have moved.
"""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pipeline.models import (
    Site, Member, Target, ExperimentSession,
)

DB = "pipeline_db"


class MergeMembersTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.mcgill = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.ottawa = Site.objects.using(DB).create(name="Ottawa", short_code="OTT")
        self.target = Target.objects.using(DB).create(
            protein_name="Kinesin heavy chain", gene_name="KIF5A",
            site_id=self.mcgill.pk)

        # The row the Access import minted: no usable login, McGill.
        imported_user = User.objects.using(DB).create(username="access_carl_laflamme")
        self.imported = Member.objects.using(DB).create(
            user_id=imported_user.pk, site_id=self.mcgill.pk,
            role="experimenter", display_name="Carl Laflamme")

        # His real account — a superuser, and he has since moved bench.
        real_user = User.objects.using(DB).create(
            username="claflamme", is_superuser=True, is_staff=True)
        self.real = Member.objects.using(DB).create(
            user_id=real_user.pk, site_id=self.ottawa.pk,
            role="lead", display_name="Carl Laflamme")

        self.session = ExperimentSession.objects.using(DB).create(
            procedure_type="WB", target_id=self.target.pk,
            experimenter_id=self.imported.pk, date="2026-04-30",
            site_id=self.mcgill.pk, status="complete")

    def _run(self, **kwargs):
        out = StringIO()
        call_command("merge_members", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_the_survivor_is_the_account_that_can_sign_in(self):
        """Chosen, not guessed. Keeping the imported row would move his work
        onto a login with a deliberately unusable password.
        """
        self._run(name="Carl Laflamme", apply=True)
        self.session.refresh_from_db(using=DB)
        self.assertEqual(self.session.experimenter_id, self.real.pk)
        self.imported.refresh_from_db(using=DB)
        self.assertFalse(self.imported.is_active)
        self.real.refresh_from_db(using=DB)
        self.assertTrue(self.real.is_active)

    def test_a_dry_run_moves_nothing(self):
        output = self._run(name="Carl Laflamme")
        self.session.refresh_from_db(using=DB)
        self.assertEqual(self.session.experimenter_id, self.imported.pk)
        self.assertIn("DRY RUN", output)

    def test_it_says_when_the_two_rows_are_at_different_benches(self):
        """He moved from McGill to Ottawa. `Member.site` follows the person and
        the session keeps its own, but somebody running this should be told
        rather than discover it on a board later.
        """
        output = self._run(name="Carl Laflamme")
        self.assertIn("different benches", output)
        self.assertIn("McGill", output)
        self.assertIn("Ottawa", output)

    def test_past_sessions_keep_their_own_bench(self):
        """The work was done at McGill and stays filed there, whoever the
        experimenter is now.
        """
        self._run(name="Carl Laflamme", apply=True)
        self.session.refresh_from_db(using=DB)
        self.assertEqual(self.session.site_id, self.mcgill.pk)

    def test_one_member_of_that_name_is_refused_rather_than_merged(self):
        # The session has to go first: `ExperimentSession.experimenter` is
        # PROTECT, which is also why the command only deletes a losing row
        # once nothing points at it.
        self.session.delete(using=DB)
        self.imported.delete(using=DB)
        with self.assertRaises(CommandError) as caught:
            self._run(name="Carl Laflamme")
        self.assertIn("nothing to merge", str(caught.exception))
