"""Saving a session must never discard conditions the form doesn't know about.

Sessions created from the per-gene Excel template carry arbitrary spreadsheet
columns in ``session_conditions`` — ``services/session_import.py::_condition_cols``
folds in every header that is neither a context nor a result column. Any editing
surface only knows the 7–11 fixed keys in ``PROCEDURE_CONDITION_FIELDS``.

The session page's full-page save used to rebuild the dict from ``{}``, so every
spreadsheet-derived key was silently deleted the first time anyone pressed
"✓ Save Changes". Its AJAX save copied the dict first and kept them — the bug was
the two paths disagreeing.

**That page was retired on 31 Jul 2026**, and conditions moved onto the sessions
board as ``cond:<key>`` cells. These tests moved with them, because the guard is
about the data and not about the page: the surface changed, the way to lose those
keys did not. One editing path now instead of two, which is the real fix — but a
one-key-at-a-time patch is *more* exposed to this bug, not less, so it is pinned
harder than before.
"""
from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from django.test import Client, TestCase

from pipeline.models import ExperimentSession, Member, Site, Target

DB = "pipeline_db"

# A key the board renders, and one only the spreadsheet knows about.
KNOWN = "lysis_buffer"
FROM_SHEET = "antibody_incubation_time"


class SessionConditionsTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        site = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        self.member = Member.objects.create(
            user_id=pu.pk, site_id=site.pk, role="experimenter", is_active=True)
        target = Target.objects.create(protein_name="Superoxide dismutase", gene_name="SOD1")
        self.session = ExperimentSession.objects.create(
            procedure_type="WB", target=target, experimenter=self.member, site=site,
            date=date(2026, 2, 18), status="complete",
            session_conditions={KNOWN: "RIPA", FROM_SHEET: "overnight 4C"},
        )
        self.patch_url = "/pipeline/sessions/board/patch/"
        self.results_url = "/pipeline/sessions/board/results/"
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def _conditions(self):
        self.session.refresh_from_db()
        return self.session.session_conditions

    def _patch(self, field, value):
        return self.client.post(self.patch_url, {
            "session_id": self.session.pk, "field": field, "value": value})

    def test_editing_a_condition_preserves_the_ones_it_cannot_see(self):
        response = self._patch(f"cond:{KNOWN}", "NP-40")
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(self._conditions().get(FROM_SHEET), "overnight 4C")

    def test_a_known_field_still_updates(self):
        self._patch(f"cond:{KNOWN}", "NP-40")
        self.assertEqual(self._conditions().get(KNOWN), "NP-40")

    def test_clearing_a_known_field_still_removes_it(self):
        """Fill-only-blank does not apply here: clearing a field you can see is an
        instruction, so the key goes rather than being stored as ""."""
        self._patch(f"cond:{KNOWN}", "")
        conditions = self._conditions()
        self.assertNotIn(KNOWN, conditions)
        self.assertEqual(conditions.get(FROM_SHEET), "overnight 4C")

    def test_editing_a_session_field_does_not_disturb_the_conditions(self):
        """The other half of the old bug: a save that was not about conditions at
        all used to rewrite the whole dict on its way past."""
        before = dict(self._conditions())
        self.client.post(self.patch_url, {
            "session_id": self.session.pk, "field": "comments",
            "value": "nothing to do with conditions"})
        self.assertEqual(self._conditions(), before)

    def test_a_spreadsheet_key_is_shown_rather_than_hidden(self):
        """The reason the keys used to vanish unnoticed is that nothing displayed
        them. A value you cannot see is a value somebody overwrites."""
        response = self.client.get(self.results_url, {"session_id": self.session.pk})
        self.assertEqual(response.status_code, 200)
        extra = {c["key"]: c["value"] for c in response.json()["extra_conditions"]}
        self.assertEqual(extra.get(FROM_SHEET), "overnight 4C")

    def test_a_spreadsheet_key_cannot_be_edited_through_a_cond_patch(self):
        """It is not in this procedure's registry, so it is read-only — otherwise
        the surface that displays them becomes the surface that mangles them."""
        response = self._patch(f"cond:{FROM_SHEET}", "something else")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._conditions().get(FROM_SHEET), "overnight 4C")
