"""Typing a gene's methods on /pipeline/methods/ (2 Oct 2026).

What would be silently wrong: a preview that is not the paragraph the button
copies, a save that overwrites an edit made after its preview, a value that
vanished without being named, and a stored value lost because the form left
that antibody out.
"""
from __future__ import annotations

import json

from pipeline.models import AntibodyMethod, MethodsRecord, PublicationImage, Site
from pipeline.services import methods_entry as E
from pipeline.services import methods_text as M
from pipeline.tests_methods_record import DOI, _Figures
from pipeline.tests_timeouts import _member_client


class MethodsEntryTests(_Figures):

    def setUp(self):
        site = Site.objects.create(name="McGill", short_code="MCG")
        self.client = _member_client(self, site)

    def _post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def _body(self, **over):
        body = {"gene": "TGM2", "application": "WB",
                "report_file": "TGM2 report.pdf",
                "conditions": {**self.record.conditions, "ecl_type": "Pierce ECL (Thermo Fisher 32106)"},
                "antibodies": [{"id": self.ab1.pk, "amount": "1/500", "basis": "report_named", "where": ""},
                               {"id": self.ab2.pk, "amount": "1/2000", "basis": "", "where": "Table 4"}]}
        body.update(over)
        return body

    def test_the_preview_is_the_paragraph_the_button_copies_after_the_save(self):
        preview = self._post("/pipeline/methods/preview/", self._body()).json()
        saved = self._post("/pipeline/methods/save/",
                           {**self._body(), "based_on": preview["stamp"]}).json()
        self.assertTrue(saved["ok"], saved)
        shown = {p["id"]: p["text"] for p in preview["previews"]}
        button = M.for_antibodies([self.ab1, self.ab2])
        self.assertEqual(shown[self.ab1.pk], button[self.ab1.pk]["WB"]["text"])
        self.assertEqual(shown[self.ab2.pk], button[self.ab2.pk]["WB"]["text"])
        self.assertIn("diluted 1/2000", shown[self.ab2.pk])
        self.assertIn(DOI, shown[self.ab2.pk])
        # A value typed with no basis is the report naming it.
        self.assertEqual(AntibodyMethod.objects.get(antibody=self.ab2).basis, "report_named")
        self.assertTrue(MethodsRecord.objects.get(pk=self.record.pk).source_ref.startswith("web:"))

    def test_a_save_after_somebody_else_saved_is_refused(self):
        stamp = self._post("/pipeline/methods/preview/", self._body()).json()["stamp"]
        MethodsRecord.objects.filter(pk=self.record.pk).update(conditions={"blocking": "3% BSA"})
        r = self._post("/pipeline/methods/save/", {**self._body(), "based_on": stamp})
        self.assertEqual(r.status_code, 409)
        self.assertIn("changed by somebody else", r.json()["error"])
        self.assertEqual(MethodsRecord.objects.get(pk=self.record.pk).conditions,
                         {"blocking": "3% BSA"})

    def test_an_emptied_box_is_named_and_removed_and_a_left_out_antibody_is_kept(self):
        AntibodyMethod.objects.create(record=self.record, antibody=self.ab2, amount="1/750",
                                      basis="report_named")
        body = self._body(conditions={**self.record.conditions, "blocking": ""},
                          antibodies=[{"id": self.ab1.pk, "amount": "", "basis": "", "where": ""}])
        preview = self._post("/pipeline/methods/preview/", body).json()
        self.assertEqual([x["key"] for x in preview["removed"]], ["blocking"])
        self.assertEqual([x["catalogue"] for x in preview["antibodies_removed"]], ["ab111"])
        self._post("/pipeline/methods/save/", {**body, "based_on": preview["stamp"]})
        self.assertNotIn("blocking", MethodsRecord.objects.get(pk=self.record.pk).conditions)
        self.assertFalse(AntibodyMethod.objects.filter(antibody=self.ab1).exists())
        self.assertEqual(AntibodyMethod.objects.get(antibody=self.ab2).amount, "1/750")

    def test_a_note_rather_than_a_method_is_named_not_stored(self):
        body = self._body(conditions={"blocking": "not stated in methods text"})
        preview = self._post("/pipeline/methods/preview/", body).json()
        self.assertEqual([x["key"] for x in preview["dropped"]], ["blocking"])
        self.assertNotIn("blocking", preview["conditions"])

    def test_a_gene_still_in_review_can_be_entered_and_no_button_shows(self):
        PublicationImage.objects.filter(antibody__target=self.target).delete()
        page = self.client.get("/pipeline/methods/?gene=TGM2&app=IP")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Methods · TGM2", page.content.decode())
        body = self._body(application="IP", conditions={"bead_type": "Dynabeads protein A"},
                          antibodies=[{"id": self.ab3.pk, "amount": "2 µg", "basis": "report_protocol",
                                       "where": ""}])
        preview = self._post("/pipeline/methods/preview/", body).json()
        saved = self._post("/pipeline/methods/save/", {**body, "based_on": preview["stamp"]}).json()
        self.assertTrue(saved["ok"], saved)
        rows = {a["id"]: a for a in E.state(self.target, "IP")["antibodies"]}
        self.assertEqual(rows[self.ab3.pk]["amount"], "2 µg")
        self.assertEqual(M.for_antibodies([self.ab3]), {})
