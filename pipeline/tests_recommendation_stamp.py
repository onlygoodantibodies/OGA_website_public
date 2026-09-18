"""When a recommendation last changed.

These four booleans are the public verdict on a named commercial product — the
gene page, the API, the MCP and the browser extension all read them — and until
29 Aug 2026 nothing recorded when one moved.

Not even ``updated_at``. ``rec_toggle`` saves with ``update_fields=[flag]``, and
with ``update_fields`` set an ``auto_now`` column is computed and then never
written, so the newest ``Antibody.updated_at`` on live was eight days older than
a day's worth of curation. Asked "when was this recommendation last set", the
database had no answer for any row.

The stamp lives on ``save`` rather than in the six places that write these flags
— ``rec_toggle``, ``review.release``, ``review.withdraw``, the cropper's commit,
the session recorder and the dataset upload — for the reason ``lab_numbers``
issues a number on ``pre_save``: the seventh caller is the one that forgets.
"""
from __future__ import annotations

from django.test import TestCase

from pipeline.models import Antibody, Company, Site, Target
from pipeline.tests_timeouts import DB


class TheRecommendationStampTests(TestCase):
    databases = {"pipeline_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")

    def _antibody(self, **flags):
        return Antibody.objects.using(DB).create(
            catalogue_number="ab1", target=self.target, company=self.company,
            site=self.site, **flags)

    def test_a_row_created_without_one_carries_no_date(self):
        """A stamp on a row that never had a recommendation would date a
        decision nobody made."""
        self.assertIsNone(self._antibody().recommendations_set_at)

    def test_a_row_created_with_one_is_stamped(self):
        self.assertIsNotNone(
            self._antibody(wb_recommended=True).recommendations_set_at)

    def test_setting_a_recommendation_stamps_it(self):
        ab = self._antibody()
        ab = Antibody.objects.using(DB).get(pk=ab.pk)
        ab.wb_recommended = True
        ab.save(using=DB)
        ab.refresh_from_db()
        self.assertIsNotNone(ab.recommendations_set_at)

    def test_update_fields_does_not_swallow_the_stamp(self):
        """The half that would fail silently, and the exact way `updated_at`
        already failed: a caller writing one column drops any other the model
        set, so the date is computed and never stored."""
        ab = self._antibody()
        ab = Antibody.objects.using(DB).get(pk=ab.pk)
        ab.if_recommended = True
        ab.save(using=DB, update_fields=["if_recommended"])
        ab.refresh_from_db()
        self.assertIsNotNone(ab.recommendations_set_at,
                             "update_fields dropped the stamp")

    def test_taking_a_recommendation_off_stamps_it_too(self):
        """Withdrawing one is a decision with a date, as much as making one."""
        ab = self._antibody(wb_recommended=True)
        ab = Antibody.objects.using(DB).get(pk=ab.pk)
        before = ab.recommendations_set_at
        ab.wb_recommended = False
        ab.save(using=DB, update_fields=["wb_recommended"])
        ab.refresh_from_db()
        self.assertGreater(ab.recommendations_set_at, before)

    def test_saving_something_else_leaves_the_date_alone(self):
        """Or the date answers "when was this row last touched", which is
        `updated_at`'s job and not a verdict's."""
        ab = self._antibody(wb_recommended=True)
        ab = Antibody.objects.using(DB).get(pk=ab.pk)
        before = ab.recommendations_set_at
        ab.lot_number = "12345"
        ab.save(using=DB)
        ab.refresh_from_db()
        self.assertEqual(ab.recommendations_set_at, before)

    def test_re_saving_the_same_value_is_not_a_change(self):
        """Pressing the button twice does not re-date the decision."""
        ab = self._antibody(wb_recommended=True)
        ab = Antibody.objects.using(DB).get(pk=ab.pk)
        before = ab.recommendations_set_at
        ab.wb_recommended = True
        ab.save(using=DB)
        ab.refresh_from_db()
        self.assertEqual(ab.recommendations_set_at, before)

    def test_the_stamp_costs_no_extra_query(self):
        """A snapshot taken as the row loads, not a re-read: this fires on every
        antibody save in the app."""
        ab = self._antibody()
        loaded = Antibody.objects.using(DB).get(pk=ab.pk)
        loaded.wb_recommended = True
        with self.assertNumQueries(1, using=DB):
            loaded.save(using=DB, update_fields=["wb_recommended"])

    def test_every_flag_is_watched(self):
        """A fifth application added to the model without being added here
        would change a public verdict with no date, silently."""
        from core.recommendations import _FLAG
        self.assertEqual(set(Antibody.RECOMMENDATION_FIELDS),
                         set(_FLAG.values()))
