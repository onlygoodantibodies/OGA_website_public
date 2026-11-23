"""Checking knockout lines for a list writes nothing and reads each line honestly.

uOttawa pasted a collaborator's wish list into Bulk Add Targets to find KO lines
(23 Nov 2026). `services/ko_screen.py` is the screen that question wanted. The
three things here are the ones that would be wrong without any screen saying so.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from django.db import connections
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from pipeline.models import DepMapExpression, HorizonKoLine, Site, Target, TargetNomination
from pipeline.services import ko_screen, supplier_ko
from pipeline.tests_timeouts import DB, _member_client


class KoScreenTests(TestCase):
    databases = {"academy_db", "pipeline_db"}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="Ontario")
        # ABCA1: Abcam has a finished (Pass) HAP1 line and an unvalidated U-87 MG
        # one (pipeline/data/abcam_ko_lines_2026_09_28.csv).
        for line, tpm in (("HAP1", 1.0), ("U-87 MG", 6.0), ("SH-SY5Y", 4.0)):
            DepMapExpression.objects.using(DB).create(gene_name="ABCA1", cell_line=line, tpm_log2=tpm)

    # Abcam's file is left out of the public repo (tools/export_public.py).
    @unittest.skipUnless(os.path.exists(supplier_ko.LISTS["abcam"].csv_path),
                         "Abcam's KO list is not in this copy")
    def test_a_finished_line_in_a_parental_that_does_not_express_is_not_usable(self):
        # The supplier made it and passed it, but HAP1 is below 2.5: a knockout
        # of a gene the line does not express removes nothing, so it must not
        # read as a usable control.
        row = ko_screen.screen(["ABCA1"])["rows"][0]
        hap1 = next(ln for ln in row["abcam"] if ln["parental_line"] == "HAP1")
        self.assertTrue(hap1["finished"])
        self.assertFalse(hap1["expresses"])
        self.assertEqual(row["route"], ko_screen.BUY_OTHER)

    def test_a_cell_naming_several_genes_is_listed_back_never_guessed(self):
        entries, unreadable = ko_screen.parse("PRKN (PARK2)\nPRKCA/B/G\nCUX2, cux2")
        self.assertEqual([e["gene"] for e in entries], ["PRKN", "CUX2"])
        self.assertIn("read as PRKN", entries[0]["note"])
        self.assertEqual(unreadable, ["PRKCA/B/G"])

    def test_a_list_that_cannot_be_read_is_named_not_read_as_empty(self):
        missing = supplier_ko.KoList(key="abcam", supplier="Abcam",
                                     source_file="not_there.csv", source_date="",
                                     done_status="Pass", done_word="verified")
        supplier_ko._by_gene.cache_clear()
        try:
            with mock.patch.dict(supplier_ko.LISTS, {"abcam": missing}):
                result = ko_screen.screen(["ABCA1"])
        finally:
            supplier_ko._by_gene.cache_clear()
        # `assertIn`, not equality: the public copy has neither file, so
        # Thermo Fisher is (rightly) named there too.
        self.assertIn("Abcam", result["unreadable_lists"])
        self.assertEqual(result["rows"][0]["abcam"], [])

    def test_cost_does_not_grow_with_the_list(self):
        def count(genes):
            with CaptureQueriesContext(connections[DB]) as ctx:
                ko_screen.screen(genes)
            return len(ctx.captured_queries)
        HorizonKoLine.objects.using(DB).create(gene_name="CUX2", item_number="HZ1", product_name="x")
        self.assertEqual(count(["ABCA1"]), count(["ABCA1", "CUX2", "FOXP2", "TBR1", "SOD1"]))

    def test_the_endpoint_writes_nothing(self):
        client = _member_client(self, self.site)
        before = (Target.objects.using(DB).count(), TargetNomination.objects.using(DB).count())
        resp = client.post("/pipeline/feasibility/ko-screen/",
                           data=json.dumps({"genes": "ABCA1\nNEWGENE1\nPRKCA/B/G"}),
                           content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertEqual([r["gene"] for r in d["rows"]], ["ABCA1", "NEWGENE1"])
        self.assertEqual(d["rows"][1]["route"], ko_screen.UNKNOWN)
        self.assertEqual(d["unreadable"], ["PRKCA/B/G"])
        self.assertEqual(before, (Target.objects.using(DB).count(),
                                  TargetNomination.objects.using(DB).count()))
