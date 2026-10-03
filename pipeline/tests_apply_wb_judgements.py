"""`apply_wb_judgements` (3 Oct 2026): a reviewed file lands whole, and only
where the live judgement still says what the file says it said."""
from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from unittest import skipIf

from django.test import TestCase

from pipeline.management.commands.apply_wb_judgements import RESULTS
from OGA_website.public_snapshot import REASON, withheld
from pipeline.models import Antibody, JudgementChange, PublicationImage, Target
from pipeline.services import outcomes as outcome_svc

DB = "pipeline_db"
SHIPPED = Path(__file__).resolve().parent / "data" / "wb_judgements_2026_10_03.json"
LIMITED = {"recommended": False, "detects": "yes", "selective": "no"}


class ApplyWbJudgementsTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        target = Target.objects.using(DB).create(gene_name="CTSB")
        cls.abs = []
        for cat in ("ZRB1635", "31718"):
            ab = Antibody.objects.using(DB).create(target=target, catalogue_number=cat)
            PublicationImage.objects.using(DB).create(
                antibody=ab, application_type="WB", image=f"{cat}.png")
            outcome_svc.record(ab.pk, "WB", "detects", "yes", actor="k")
            outcome_svc.record(ab.pk, "WB", "selective", "no", actor="k")
            cls.abs.append(ab)

    def _run(self, rows, *flags):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "j.json"
            path.write_text(json.dumps({"application": "WB", "judgements": rows}))
            out = StringIO()
            call_command("apply_wb_judgements", str(path), *flags, stdout=out)
            return out.getvalue()

    def _row(self, ab, result, was=LIMITED):
        return {"antibody_id": ab.pk, "gene": "CTSB",
                "catalogue": ab.catalogue_number, "was": was, "result": result}

    def _state(self, ab):
        ab = Antibody.objects.using(DB).get(pk=ab.pk)
        axes = outcome_svc.for_gene(ab.target_id, "WB")[ab.pk]
        return (ab.wb_recommended, axes["detects"]["value"], axes["selective"]["value"])

    def test_a_dry_run_writes_nothing(self):
        self._run([self._row(self.abs[0], "supportive")])
        self.assertEqual(self._state(self.abs[0]), (False, "yes", "no"))

    def test_apply_sets_the_flag_and_both_axes_and_logs_them(self):
        self._run([self._row(self.abs[0], "supportive"),
                   self._row(self.abs[1], "supportive_not_selective")], "--apply")
        self.assertEqual(self._state(self.abs[0]), (True, "yes", "yes"))
        self.assertEqual(self._state(self.abs[1]), (True, "yes", "no"))
        self.assertTrue(JudgementChange.objects.using(DB).filter(
            catalogue_number="ZRB1635", field="recommended", new_value="yes").exists())

    def test_a_row_changed_since_the_review_is_refused_and_left_alone(self):
        outcome_svc.record(self.abs[0].pk, "WB", "detects", "no", actor="someone")
        out = self._run([self._row(self.abs[0], "supportive")], "--apply")
        self.assertIn("changed since the review", out)
        self.assertEqual(self._state(self.abs[0]), (False, "no", "no"))

    @skipIf(withheld(SHIPPED), REASON)
    def test_the_shipped_file_names_only_results_the_command_knows(self):
        doc = json.loads(SHIPPED.read_text())
        self.assertEqual(len(doc["judgements"]), 94)
        self.assertLessEqual({r["result"] for r in doc["judgements"]}, set(RESULTS))
