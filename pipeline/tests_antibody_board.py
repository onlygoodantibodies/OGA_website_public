"""The antibodies board: find an antibody, edit it, toggle what it's good for.

The rule with teeth here is that identity is not editable. ``(catalogue,
company, target, lot, site)`` is the antibody's key — retyping the catalogue
number in a grid cell would silently turn the row into a different antibody,
so the board refuses it. RRID is refused for the same class of reason: it is
written through ``rrid_utils`` so the bare AB_number and the registry link
cannot drift apart.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from pipeline.models import Antibody, Company, Member, Site, Target

DB = "pipeline_db"


class AntibodyBoardTests(TestCase):
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
                              role="experimenter", is_active=True,
                              display_name="Vera")
        self.target = Target.objects.create(protein_name="SOD", gene_name="SOD1")
        self.company = Company.objects.create(name="Proteintech")
        self.antibody = Antibody.objects.create(
            target=self.target, company=self.company, catalogue_number="12A8",
            clonality="polyclonal", host_species="rabbit", site=self.site)
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def _patch(self, query="", **body):
        return self.client.post(f"/pipeline/antibodies/board/patch/?{query}", body)

    def test_the_page_renders_on_the_shared_module(self):
        html = self.client.get("/pipeline/antibodies/board/").content.decode()
        self.assertIn("pipeline/board.js", html)
        self.assertIn("OGABoard.create(", html)

    def test_rows_carry_what_the_grid_draws(self):
        row = self.client.get("/pipeline/antibodies/board/rows/").json()["rows"][0]
        self.assertEqual(row["catalogue"], "12A8")
        self.assertEqual(row["company"], "Proteintech")
        self.assertEqual(row["gene"], "SOD1")
        self.assertEqual(row["recommended"], {"wb": False, "ip": False,
                                              "if": False, "fc": False})

    def test_filters_narrow_the_board(self):
        other_target = Target.objects.create(protein_name="Tau", gene_name="MAPT")
        Antibody.objects.create(target=other_target, company=self.company,
                                catalogue_number="99Z", wb_recommended=True)
        by_gene = self.client.get("/pipeline/antibodies/board/rows/?gene=MAPT").json()
        self.assertEqual(by_gene["count"], 1)
        by_app = self.client.get(
            "/pipeline/antibodies/board/rows/?application=wb").json()
        self.assertEqual(by_app["count"], 1)
        recommended = self.client.get(
            "/pipeline/antibodies/board/rows/?recommended=no").json()
        self.assertEqual(recommended["count"], 1)
        self.assertEqual(recommended["rows"][0]["catalogue"], "12A8")

    def test_editing_a_free_text_field_saves(self):
        data = self._patch(target_id=self.antibody.pk, field="lot_number",
                           value="B-4471").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["row"]["lot_number"], "B-4471")
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.lot_number, "B-4471")

    def test_toggling_a_recommendation(self):
        self._patch(target_id=self.antibody.pk, field="wb_recommended", value="yes")
        self.antibody.refresh_from_db()
        self.assertTrue(self.antibody.wb_recommended)

        self._patch(target_id=self.antibody.pk, field="wb_recommended", value="")
        self.antibody.refresh_from_db()
        self.assertFalse(self.antibody.wb_recommended)

    def test_site_resolves_by_name_and_a_bad_one_is_refused(self):
        self._patch(target_id=self.antibody.pk, field="site", value="McGill")
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.site_id, self.site2.pk)

        response = self._patch(target_id=self.antibody.pk, field="site",
                               value="Atlantis")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Atlantis", response.json()["error"])
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.site_id, self.site2.pk)

    def test_a_non_numeric_concentration_is_refused(self):
        response = self._patch(target_id=self.antibody.pk, field="concentration",
                               value="lots")
        self.assertEqual(response.status_code, 400)
        # "not a number" was the old wording, and it was wrong about the case
        # that matters: "1.0 mg/mL" *is* a number, with a unit. The refusal names
        # the units it takes, so it says what is right and not only what is
        # wrong — services/concentration.py.
        self.assertIn("not a concentration", response.json()["error"])
        self.assertIn("µg/mL", response.json()["error"])

    def test_identity_and_rrid_are_not_editable(self):
        for field in ("catalogue_number", "company", "target", "rrid", "rrid_link"):
            response = self._patch(target_id=self.antibody.pk, field=field,
                                   value="tampered")
            self.assertEqual(response.status_code, 400, field)
            self.assertIn("not editable", response.json()["error"])
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.catalogue_number, "12A8")
        self.assertEqual(self.antibody.rrid, "")

    def test_a_row_pushed_out_of_the_active_filter_reports_matches_false(self):
        data = self._patch(f"site={self.site.pk}", target_id=self.antibody.pk,
                           field="site", value="McGill").json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["matches"])
        self.assertIsNone(data["row"])

    def test_unknown_antibody_is_a_404(self):
        self.assertEqual(
            self._patch(target_id=999999, field="lot_number", value="x").status_code,
            404)

    def test_rows_query_count_does_not_grow_with_the_board(self):
        def queries():
            with CaptureQueriesContext(connections[DB]) as ctx:
                r = self.client.get("/pipeline/antibodies/board/rows/")
                self.assertEqual(r.status_code, 200)
            return len(ctx), r.json()["count"]

        small, small_n = queries()
        for i in range(30):
            Antibody.objects.create(target=self.target, company=self.company,
                                    catalogue_number=f"BULK-{i}")
        large, large_n = queries()
        self.assertEqual((small_n, large_n), (1, 31))
        self.assertEqual(small, large,
                         f"rows/ issued {small} queries for 1 antibody but {large} "
                         f"for 31 — that is an N+1")
