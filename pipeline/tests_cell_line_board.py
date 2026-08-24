"""The cell lines board: find a line, edit it, mark it validated/received.

The rule with teeth: name, gene, genotype and parent are not editable in a cell.
A KO line means "this gene knocked out in that parent", and every session that
used it points at this row — retyping any of those in a grid would silently make
the row describe a different line while the history still says otherwise.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from pipeline.models import CellLine, Member, Site, Target

DB = "pipeline_db"


class CellLineBoardTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.create(name="Leicester", short_code="LEI")
        self.site2 = Site.objects.create(name="McGill", short_code="MCG")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        Member.objects.create(user_id=pu.pk, site_id=self.site.pk,
                              role="experimenter", is_active=True, display_name="Vera")
        self.target = Target.objects.create(protein_name="SOD", gene_name="SOD1")
        self.wt = CellLine.objects.create(name="HAP1-WT", genotype="WT")
        self.ko = CellLine.objects.create(
            name="SOD1-KO", genotype="KO", target=self.target,
            parent_line=self.wt, site=self.site, medium="IMDM")
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def _patch(self, query="", **body):
        return self.client.post(f"/pipeline/cell-lines/board/patch/?{query}", body)

    def test_the_page_renders_on_the_shared_module(self):
        html = self.client.get("/pipeline/cell-lines/board/").content.decode()
        self.assertIn("pipeline/board.js", html)
        self.assertIn("OGABoard.create(", html)

    def test_rows_carry_the_structure(self):
        rows = self.client.get("/pipeline/cell-lines/board/rows/").json()["rows"]
        ko = next(r for r in rows if r["name"] == "SOD1-KO")
        self.assertEqual(ko["gene"], "SOD1")
        self.assertEqual(ko["genotype"], "KO")
        self.assertEqual(ko["parent"], "HAP1-WT")
        self.assertFalse(ko["ko_validated"])

    def test_filters_narrow_the_board(self):
        self.assertEqual(
            self.client.get("/pipeline/cell-lines/board/rows/?genotype=KO").json()["count"], 1)
        self.assertEqual(
            self.client.get("/pipeline/cell-lines/board/rows/?gene=SOD1").json()["count"], 1)
        self.assertEqual(
            self.client.get("/pipeline/cell-lines/board/rows/?ko_validated=no").json()["count"], 2)

    def test_editing_a_descriptive_field_saves(self):
        data = self._patch(target_id=self.ko.pk, field="medium",
                           value="DMEM + 10% FBS").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["row"]["medium"], "DMEM + 10% FBS")
        self.ko.refresh_from_db()
        self.assertEqual(self.ko.medium, "DMEM + 10% FBS")

    def test_toggling_ko_validated(self):
        self._patch(target_id=self.ko.pk, field="ko_validated", value="yes")
        self.ko.refresh_from_db()
        self.assertTrue(self.ko.ko_validated)

        self._patch(target_id=self.ko.pk, field="ko_validated", value="")
        self.ko.refresh_from_db()
        self.assertFalse(self.ko.ko_validated)

    def test_site_resolves_by_name_and_a_bad_one_is_refused(self):
        self._patch(target_id=self.ko.pk, field="site", value="McGill")
        self.ko.refresh_from_db()
        self.assertEqual(self.ko.site_id, self.site2.pk)

        response = self._patch(target_id=self.ko.pk, field="site", value="Atlantis")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Atlantis", response.json()["error"])

    def test_structure_is_not_editable(self):
        for field in ("name", "genotype", "target", "parent_line", "company"):
            response = self._patch(target_id=self.ko.pk, field=field, value="tampered")
            self.assertEqual(response.status_code, 400, field)
            self.assertIn("not editable", response.json()["error"])
        self.ko.refresh_from_db()
        self.assertEqual(self.ko.name, "SOD1-KO")
        self.assertEqual(self.ko.genotype, "KO")
        self.assertEqual(self.ko.parent_line_id, self.wt.pk)

    def test_a_row_pushed_out_of_the_active_filter_reports_matches_false(self):
        data = self._patch("ko_validated=no", target_id=self.ko.pk,
                           field="ko_validated", value="yes").json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["matches"])

    def test_unknown_line_is_a_404(self):
        self.assertEqual(
            self._patch(target_id=999999, field="medium", value="x").status_code, 404)

    def test_rows_query_count_does_not_grow_with_the_board(self):
        def queries():
            with CaptureQueriesContext(connections[DB]) as ctx:
                r = self.client.get("/pipeline/cell-lines/board/rows/")
                self.assertEqual(r.status_code, 200)
            return len(ctx), r.json()["count"]

        small, small_n = queries()
        for i in range(30):
            CellLine.objects.create(name=f"BULK-{i}", genotype="WT")
        large, large_n = queries()
        self.assertEqual((small_n, large_n), (2, 32))
        self.assertEqual(small, large,
                         f"rows/ issued {small} queries for 2 lines but {large} "
                         f"for 32 — that is an N+1")
