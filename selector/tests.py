"""End-to-end tests for the selection tool.

These run against Django's test databases (empty pipeline_db), so they also
assert the *graceful degradation* the tool promises: no OGA/DepMap/Horizon data
and a dead UniProt must still yield a usable JSON payload and a downloadable
PDF — never a 500.

The outage is **simulated, not assumed**. This file used to rely on dev's
outbound network being blocked, which made it a test of the environment: it
passed locally and failed the first time CI ran it with a working network.
"""
import json
from unittest import mock

import requests
from django.test import Client, TestCase
from django.urls import reverse

from .models import SelectionRecord


class SelectorFlowTests(TestCase):
    # gene_lookup/record touch multiple routed DBs.
    databases = {"academy_db", "pipeline_db"}

    def setUp(self):
        self.c = Client()

    def test_tool_page_renders(self):
        r = self.c.get(reverse("selector:tool"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Antibody &amp; Controls Selection Support")
        # CSRF cookie is set so the client-side POST can authenticate.
        self.assertIn("csrftoken", r.cookies)

    def test_gene_lookup_degrades_gracefully(self):
        """A dead UniProt still answers with every key, and `found` is False.

        This asserted `found` was False with the comment `# network blocked`,
        and that is what it was really testing: it passed in dev, where outbound
        HTTP is blocked, and failed the first time it ran anywhere with a
        working network — CI, where UniProt answered and SNCA was found. The
        assertion was right about the degraded path and had no way to reach it
        on purpose.

        The outage is simulated now, so the test means the same thing wherever
        it runs. It patches `requests` rather than `lookup_gene`, because
        `lookup_gene` catches `RequestException` itself: patching the function
        would exercise a branch the real call cannot reach — the same trap
        CLAUDE.md records against the earlier outage test.
        """
        def dead(*args, **kwargs):
            raise requests.RequestException("no route to host")

        with mock.patch.multiple("requests", get=dead, post=dead, request=dead):
            r = self.c.get(reverse("selector:gene_lookup"), {"gene": "SNCA"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for key in ("oga", "uniprot", "depmap", "horizon_ko", "proteomics"):
            self.assertIn(key, data)
        self.assertFalse(data["oga"]["in_dataset"])          # empty test pipeline_db
        self.assertFalse(data["uniprot"].get("found"))

    def test_gene_lookup_requires_gene(self):
        r = self.c.get(reverse("selector:gene_lookup"))
        self.assertEqual(r.status_code, 400)

    def _sample_plan(self):
        return {
            "target_gene": "TERT",
            "species": "human",
            "protein_name": "Telomerase reverse transcriptase",
            "application": "FC",
            "sample_types": ["cell_line"],
            "cell_line": "HAP1",
            "location": "intracellular",
            "evidence_status": "none",
            "feasible_pillars": ["knockdown", "tagged", "orthogonal"],
            "ptm": True,
            "ptm_control": "Lambda phosphatase abolishes the band; phospho-dead mutant is negative.",
            "budget": "siRNA ~£1,000; OriGene lysate ~£200.",
            "antibody_identity": {"vendor": "Abcam", "catalogue": "ab32020",
                                  "rrid": "AB_778850", "clone": "Y182"},
            "recommended_protocol": {
                "stage1": [{"title": "Screen against a knockdown",
                            "detail": "siRNA to reduce the target; confirm by RT-qPCR."}],
                "stage2": [{"title": "Orthogonal — correlate with omics",
                            "detail": "Compare staining across cell lines of differing expression."}],
                "technique_note": "Intracellular flow: test 3 fix/perms.",
                "find_first": ["YCharOS open characterisation data"],
                "gaps": [],
            },
        }

    def test_record_and_pdf(self):
        payload = {"plan": self._sample_plan(),
                   "guidance": ["Start with OGA.", "KO is the gold-standard negative control."],
                   "name": "Dr Test", "email": "test@university.ac.uk"}
        r = self.c.post(reverse("selector:record_plan"),
                        data=json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("code", body)
        self.assertIn("pdf_url", body)

        rec = SelectionRecord.objects.get(public_code=body["code"])
        self.assertEqual(rec.target_gene, "TERT")
        self.assertEqual(rec.application, "FC")
        self.assertTrue(rec.completed)
        self.assertEqual(rec.name, "Dr Test")
        self.assertIn("knockdown", rec.pillars_summary)

        self.assertTrue(rec.plan.get("ptm"))
        self.assertEqual(rec.plan.get("budget"), "siRNA ~£1,000; OriGene lysate ~£200.")
        self.assertEqual(rec.plan.get("cell_line"), "HAP1")   # cell-model step persists

        # PDF renders
        pr = self.c.get(body["pdf_url"])
        self.assertEqual(pr.status_code, 200)
        self.assertEqual(pr["Content-Type"], "application/pdf")
        self.assertTrue(pr.content.startswith(b"%PDF"))
        self.assertGreater(len(pr.content), 1500)

        # record view + identity attach
        rv = self.c.get(body["record_url"])
        self.assertEqual(rv.status_code, 200)
        ir = self.c.post(reverse("selector:attach_identity", args=[body["code"]]),
                         data=json.dumps({"name": "Dr Renamed"}),
                         content_type="application/json")
        self.assertEqual(ir.status_code, 200)
        rec.refresh_from_db()
        self.assertEqual(rec.name, "Dr Renamed")

    def test_personal_record_is_not_compliance(self):
        """A plain save (no is_compliance flag) is a personal plan."""
        r = self.c.post(reverse("selector:record_plan"),
                        data=json.dumps({"plan": {"target_gene": "X"}, "guidance": []}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200)
        rec = SelectionRecord.objects.get(public_code=r.json()["code"])
        self.assertFalse(rec.is_compliance)

    def test_compliance_rejects_personal_email(self):
        payload = {"plan": {"target_gene": "TERT"}, "guidance": [], "is_compliance": True,
                   "name": "Dr Test", "institution": "University of Leicester",
                   "email": "someone@gmail.com"}
        r = self.c.post(reverse("selector:record_plan"),
                        data=json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("institutional", r.json()["error"].lower())

    def test_compliance_requires_all_fields(self):
        # institutional email but no institution → rejected
        payload = {"plan": {"target_gene": "X"}, "guidance": [], "is_compliance": True,
                   "name": "Dr Test", "email": "test@le.ac.uk"}
        r = self.c.post(reverse("selector:record_plan"),
                        data=json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_compliance_record_saved_with_institutional_email(self):
        payload = {"plan": {"target_gene": "TERT", "application": "WB"}, "guidance": [],
                   "is_compliance": True, "name": "Dr Test",
                   "institution": "University of Leicester", "email": "test@le.ac.uk"}
        r = self.c.post(reverse("selector:record_plan"),
                        data=json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        rec = SelectionRecord.objects.get(public_code=r.json()["code"])
        self.assertTrue(rec.is_compliance)
        self.assertEqual(rec.institution, "University of Leicester")
        pr = self.c.get(r.json()["pdf_url"])                 # compliance PDF renders
        self.assertEqual(pr.status_code, 200)
        self.assertTrue(pr.content.startswith(b"%PDF"))

    def test_record_rejects_garbage(self):
        r = self.c.post(reverse("selector:record_plan"),
                        data="not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_pdf_minimal_plan(self):
        """A near-empty plan (someone bailed early) must still render."""
        r = self.c.post(reverse("selector:record_plan"),
                        data=json.dumps({"plan": {"target_gene": "X"}, "guidance": []}),
                        content_type="application/json")
        code = r.json()["code"]
        pr = self.c.get(reverse("selector:plan_pdf", args=[code]))
        self.assertEqual(pr.status_code, 200)
        self.assertTrue(pr.content.startswith(b"%PDF"))
