"""A changed judgement is logged (`JudgementChange`, 30 Sep 2026).

Pinned: each of the three writers leaves a row naming before, after and who;
a press that changes nothing leaves none; and the history page draws them.
"""
from __future__ import annotations

from django.test import TestCase

from pipeline.models import (Antibody, JudgementChange,
                             PendingPublicationImage, Site, Target)
from pipeline.services import outcomes as outcome_svc
from pipeline.services import review as review_svc
from pipeline.tests_timeouts import DB, _member_client


class AChangedJudgementIsLoggedTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.target = Target.objects.using(DB).create(gene_name="SOD1")
        cls.ab = Antibody.objects.using(DB).create(
            target=cls.target, catalogue_number="ab13498")

    def _log(self):
        return list(JudgementChange.objects.using(DB)
                    .order_by("id").values_list(
                        "field", "old_value", "new_value", "changed_by",
                        "queued", "gene", "catalogue_number"))

    def test_an_axis_change_names_before_after_and_who(self):
        outcome_svc.record(self.ab.pk, "WB", "selective", "yes", actor="carl")
        outcome_svc.record(self.ab.pk, "WB", "selective", "no", actor="riham")
        self.assertEqual(self._log(), [
            ("selective", "", "yes", "carl", False, "SOD1", "ab13498"),
            ("selective", "yes", "no", "riham", False, "SOD1", "ab13498"),
        ])

    def test_a_press_that_changes_nothing_is_not_logged(self):
        outcome_svc.record(self.ab.pk, "WB", "detects", "yes", actor="carl")
        outcome_svc.record(self.ab.pk, "WB", "detects", "yes", actor="carl")
        self.assertEqual(len(self._log()), 1)

    def test_a_recommendation_moved_on_the_antibody_is_logged(self):
        ab = Antibody.objects.using(DB).get(pk=self.ab.pk)
        ab.wb_recommended = True
        ab.save(using=DB)
        ab.save(using=DB)  # no change, no row
        ab.wb_recommended = False
        ab.save(using=DB, update_fields=["wb_recommended"])
        self.assertEqual([r[:3] for r in self._log()], [
            ("recommended", "no", "yes"), ("recommended", "yes", "no")])
        self.assertEqual(
            set(JudgementChange.objects.using(DB)
                .values_list("application_type", flat=True)), {"WB"})

    def test_a_queued_recommendation_is_logged_as_queued(self):
        item = PendingPublicationImage.objects.using(DB).create(
            antibody=self.ab, application_type="IP", image="q.png")
        review_svc.set_recommended([item], True, actor="carl")
        self.assertEqual(self._log(), [
            ("recommended", "no", "yes", "carl", True, "SOD1", "ab13498")])

    def test_the_history_page_draws_the_change(self):
        site = Site.objects.using(DB).create(name="McGill")
        client = _member_client(self, site)
        outcome_svc.record(self.ab.pk, "ICC-IF", "selective",
                           "strongly_selective", actor="carl")
        page = client.get("/pipeline/outcomes/history/?gene=SOD1")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "ab13498")
        self.assertContains(page, "Strongly selective")
        self.assertContains(page, "1 change on SOD1")
        other = client.get("/pipeline/outcomes/history/?gene=TP53")
        self.assertContains(other, "No changes recorded for TP53")
