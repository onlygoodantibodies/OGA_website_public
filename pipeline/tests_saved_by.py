"""Who added a row and who last saved it — `pipeline/saved_by.py`.

Pinned: the ways the stamp could be silently wrong or silently missing — a
board edit (a partial save) that leaves the old name standing, a bulk paste
that stamps nobody, and a stamp that leaks into the result columns, where it
would turn every row saved through the website into "a reading" and print as
a column on the bench sheet.
"""
from __future__ import annotations

from django.test import TestCase

from pipeline import saved_by
from pipeline.models import Antibody, Company, ExperimentSession, Site, Target, WbResult
from pipeline.services import session_board, sessions
from pipeline.tests_timeouts import DB, _member_client


class AWebSaveNamesWhoeverWasSignedInTests(TestCase):
    databases = {DB, "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        cls.target = Target.objects.using(DB).create(gene_name="MCOLN1")
        cls.company = Company.objects.using(DB).create(name="Proteintech")

    def test_a_board_edit_restamps_the_saver_and_keeps_the_adder(self):
        """The board's patch is a partial save, and `pre_save` only runs for
        the fields a partial save names — so without `SavedBy.save` adding
        itself, the row would go on naming whoever saved it before."""
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="15291-1-AP", site_id=self.site.pk,
            added_by="mickey", saved_by="mickey")
        client = _member_client(self, self.site)  # signs in as "carl"
        r = client.post("/pipeline/antibodies/board/patch/",
                        {"antibody_id": ab.pk, "field": "lot_number", "value": "L1"})
        self.assertEqual(r.status_code, 200, r.content)
        ab.refresh_from_db(using=DB)
        self.assertEqual((ab.lot_number, ab.added_by, ab.saved_by),
                         ("L1", "mickey", "carl"))
        self.assertIn("added by mickey", r.json()["row"]["provenance"])
        self.assertIn("last saved by carl", r.json()["row"]["provenance"])


class OutsideARequestTests(TestCase):
    databases = {DB}

    @classmethod
    def setUpTestData(cls):
        cls.target = Target.objects.using(DB).create(gene_name="GCG")
        cls.company = Company.objects.using(DB).create(name="Abcam")

    def test_bulk_create_is_stamped_too(self):
        """The paste and upload doors create rows in bulk, where no `pre_save`
        signal fires; a field's own `pre_save` is still called."""
        token = saved_by._request.set(_Signed("riham"))
        try:
            [ab] = Antibody.objects.using(DB).bulk_create([Antibody(
                target_id=self.target.pk, company_id=self.company.pk,
                catalogue_number="ab1")])
        finally:
            saved_by._request.reset(token)
        self.assertEqual((ab.added_by, ab.saved_by), ("riham", "riham"))

    def test_a_management_command_names_itself(self):
        """An import saves rows too, and 'a script did this' is an answer where
        a blank is not. Under the test runner the command is `test`."""
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab2")
        self.assertEqual(ab.added_by, "command:test")
        self.assertEqual(saved_by.describe(ab.added_by, ab.saved_by),
                         "added and last saved by a script (test)")


class AStampIsNotAReadingTests(TestCase):
    """The result tables carry the stamp, and every one of them is swept for
    its column names by the bench sheet, the importers and `reading_q`."""

    databases = {DB}

    def test_no_result_column_list_carries_the_stamp(self):
        for procedure in session_board.RESULT_MODELS:
            with self.subTest(procedure=procedure):
                names = set(session_board.result_field_names(procedure))
                self.assertFalse(names & set(saved_by.FIELDS))
                self.assertFalse(set(sessions._result_field_names(procedure))
                                 & set(saved_by.FIELDS))

    def test_a_blank_row_saved_on_the_website_is_still_not_a_reading(self):
        site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        target = Target.objects.using(DB).create(gene_name="MCOLN1")
        company = Company.objects.using(DB).create(name="Abcam")
        ab = Antibody.objects.using(DB).create(
            target_id=target.pk, company_id=company.pk, catalogue_number="ab3")
        from pipeline.models import Member
        from django.contrib.auth.models import User
        user = User(username="sara")
        user.save(using=DB)
        member = Member.objects.using(DB).create(user_id=user.pk, site_id=site.pk,
                                                 role="experimenter")
        session = ExperimentSession.objects.using(DB).create(
            target_id=target.pk, procedure_type="WB", experimenter_id=member.pk,
            site_id=site.pk, date="2026-09-24")
        token = saved_by._request.set(_Signed("sara"))
        try:
            WbResult.objects.using(DB).create(session_id=session.pk, antibody_id=ab.pk)
        finally:
            saved_by._request.reset(token)
        # Saved on the website by sara, and still a blank row: no reading,
        # and so nobody named as having saved readings.
        self.assertEqual(session_board.readings_and_savers([session.pk]), ({}, {}))
        row = WbResult.objects.using(DB).get(session_id=session.pk)
        self.assertEqual(row.saved_by, "sara")

        # The reading itself, written by somebody else, is what gets named.
        token = saved_by._request.set(_Signed("mickey"))
        try:
            row.rating = "YES"
            row.save(using=DB, update_fields=["rating"])
        finally:
            saved_by._request.reset(token)
        self.assertEqual(session_board.readings_and_savers([session.pk]),
                         ({session.pk: 1}, {session.pk: ["mickey"]}))


class _Signed:
    """The least of a request `current()` reads."""

    def __init__(self, username):
        self.user = _User(username)


class _User:
    is_authenticated = True

    def __init__(self, username):
        self._name = username

    def get_username(self):
        return self._name


class TheExperimenterPickerSaysWhichProfileIsOldTests(TestCase):
    """Mickey filed three sessions under the Access-era `access_congyao_zha`,
    drawn as a plain "Congyao Zha" beside "Mickey". The pickers now group the
    imported profiles under their own heading, and still start on the person
    signed in."""

    databases = {DB, "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")

    def test_the_old_profile_is_under_its_own_heading_and_you_are_preselected(self):
        from django.contrib.auth.models import User
        from pipeline.models import Member
        from pipeline.services import members
        client = _member_client(self, self.site)  # "carl"
        old = User(username="access_congyao_zha")
        old.save(using=DB)
        Member.objects.using(DB).create(user_id=old.pk, site_id=self.site.pk,
                                        role="experimenter", display_name="Congyao Zha")
        carl = Member.objects.using(DB).get(user__username="carl")

        page = client.get("/pipeline/session/new/").content.decode()
        heading = page.index(f'<optgroup label="{members.IMPORTED_GROUP}">')
        self.assertGreater(page.index(">Congyao Zha"), heading)
        self.assertLess(page.index(f'value="{carl.pk}" selected'), heading)

        board = client.get("/pipeline/sessions/board/").content.decode()
        self.assertIn(members.IMPORTED_GROUP, board)


class TheBackfillWritesOnlyWhatItsEvidenceSaysTests(TestCase):
    """`backfill_saved_by`: a rule whose count differs from the documented one
    writes nothing, a name already there is kept, `saved_by` goes only on a row
    nobody has saved since, and the command does not stamp itself."""

    databases = {DB}

    @classmethod
    def setUpTestData(cls):
        cls.target = Target.objects.using(DB).create(gene_name="NR4A2")
        cls.company = Company.objects.using(DB).create(name="Abcam")

    def _ab(self, cat, **stamps):
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk, catalogue_number=cat)
        Antibody.objects.using(DB).filter(pk=ab.pk).update(
            **{"added_by": "", "saved_by": "", **stamps})
        return ab

    def _run(self, rules, apply=True):
        from io import StringIO
        from unittest import mock
        from django.core.management import call_command
        from pipeline.management.commands import backfill_saved_by as cmd
        out = StringIO()
        with mock.patch.object(cmd, "RULES", rules):
            call_command("backfill_saved_by", *(["--apply"] if apply else []), stdout=out)
        return out.getvalue()

    def _rule(self, expected):
        from django.db.models import Q
        from pipeline.management.commands.backfill_saved_by import Rule
        return Rule("command:import_x", "test", Antibody,
                    Q(catalogue_number__startswith="bf-"), expected)

    def test_it_fills_blanks_from_the_evidence_and_nothing_else(self):
        from datetime import timedelta
        from django.db.models import F
        untouched = self._ab("bf-1")
        edited = self._ab("bf-2")
        Antibody.objects.using(DB).filter(pk=edited.pk).update(
            updated_at=F("created_at") + timedelta(days=3))
        named = self._ab("bf-3", added_by="riham")
        before = {a.pk: a.updated_at for a in Antibody.objects.using(DB)}

        self._run([self._rule(expected=3)], apply=False)
        self.assertFalse(Antibody.objects.using(DB).exclude(added_by="").exclude(pk=named.pk))

        self._run([self._rule(expected=3)])
        got = {a.pk: (a.added_by, a.saved_by) for a in Antibody.objects.using(DB)}
        self.assertEqual(got[untouched.pk], ("command:import_x", "command:import_x"))
        self.assertEqual(got[edited.pk], ("command:import_x", ""))
        self.assertEqual(got[named.pk][0], "riham")
        self.assertEqual({a.pk: a.updated_at for a in Antibody.objects.using(DB)}, before)

    def test_a_count_that_differs_from_the_documented_one_writes_nothing(self):
        self._ab("bf-1")
        out = self._run([self._rule(expected=41)])
        self.assertIn("NOT applied", out)
        self.assertFalse(Antibody.objects.using(DB).exclude(added_by=""))

    def test_every_real_rule_is_a_query_the_database_accepts(self):
        """The rules only ever run against live data, so a query that does not
        compile would first fail there. An empty database answers every rule
        with 0, which only the rules documented as 0 accept."""
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("backfill_saved_by", stdout=out)
        self.assertIn("Dry run", out.getvalue())
