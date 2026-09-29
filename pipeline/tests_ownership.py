"""Only a superuser writes another site's records (owner, 29 Sep 2026).

`services/ownership.py` asks it at every save; these pin the doors that were
open on live — a McGill member pasted Leicester antibodies, edited Leicester's
cells, and then could not delete what they had made — and that the refusal
reaches the reader in words whatever the view did with it.
"""
from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import TestCase

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             Site, Target, TargetNomination)
from pipeline.tests_timeouts import DB, _member_client


class OnlyASuperuserWritesAnotherSitesRecordsTests(TestCase):
    databases = {DB, "academy_db"}

    def setUp(self):
        self.mcgill = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.leicester = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.mcgill)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")
        company = Company.objects.using(DB).create(name="Proteintech")
        self.mine = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="MINE-1",
            site=self.mcgill)
        self.theirs = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="THEIRS-1",
            site=self.leicester)

    def _patch(self, ab, field, value):
        return self.client.post("/pipeline/antibodies/board/patch/",
                                {"target_id": ab.pk, "field": field, "value": value})

    def _superuser(self):
        User.objects.using("academy_db").update(is_superuser=True)

    def test_your_own_benchs_record_saves(self):
        self.assertEqual(self._patch(self.mine, "lot_number", "L9").status_code, 200)

    def test_another_sites_record_is_refused_by_name_and_left_alone(self):
        resp = self._patch(self.theirs, "lot_number", "L9")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("belongs to Leicester", resp.json()["error"])
        self.theirs.refresh_from_db(using=DB)
        self.assertEqual(self.theirs.lot_number, "")

    def test_moving_your_own_record_to_another_site_is_refused(self):
        resp = self._patch(self.mine, "site", "Leicester")
        self.assertEqual(resp.status_code, 403)
        self.mine.refresh_from_db(using=DB)
        self.assertEqual(self.mine.site_id, self.mcgill.pk)

    def test_taking_another_sites_record_is_refused(self):
        """Retyping its site to your own is two sites, not one."""
        self.assertEqual(self._patch(self.theirs, "site", "McGill").status_code, 403)

    def test_a_superuser_writes_any(self):
        self._superuser()
        self.assertEqual(self._patch(self.theirs, "lot_number", "L9").status_code, 200)

    def test_a_record_with_no_site_is_nobodys_bench(self):
        loose = Antibody.objects.using(DB).create(
            target=self.target, catalogue_number="LOOSE-1")
        self.assertEqual(self._patch(loose, "lot_number", "L9").status_code, 200)

    def test_the_paste_preview_blocks_the_row_and_the_save_writes_nothing(self):
        text = ("gene\tcatalogue\tcompany\tsite\n"
                "STMN2\tPASTE-1\tProteintech\tLeicester\n")
        check = self.client.post("/pipeline/antibodies/bulk/parse/",
                                 data=json.dumps({"text": text}),
                                 content_type="application/json").json()
        item = check["items"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertIn("belongs to Leicester", item["note"])
        self.client.post("/pipeline/antibodies/bulk/commit/",
                         data=json.dumps({"text": text, "dry_run": False}),
                         content_type="application/json")
        self.assertFalse(Antibody.objects.using(DB)
                         .filter(catalogue_number="PASTE-1").exists())

    def test_a_cell_line_pasted_for_another_site_is_blocked(self):
        text = "name\tgenotype\tsite\nHeLa\tWT\tLeicester\n"
        check = self.client.post("/pipeline/cell-lines/bulk/parse/",
                                 data=json.dumps({"text": text}),
                                 content_type="application/json").json()
        self.assertEqual(check["items"][0]["status"], "blocked")
        self.assertFalse(CellLine.objects.using(DB).filter(name="HeLa").exists())

    def test_a_session_planned_for_another_site_is_refused_on_the_check(self):
        body = {"gene": "STMN2", "procedure_type": "WB", "date": "2026-09-29",
                "site_id": self.leicester.pk, "text": "antibody\nTHEIRS-1\n"}
        check = self.client.post("/pipeline/session/plan/parse/",
                                 data=json.dumps(body),
                                 content_type="application/json").json()
        self.assertIn("This session would be Leicester's, not your site's. You can "
                      "plan sessions for your own bench; a superuser can plan them "
                      "for any site.", check["blocking"])
        resp = self.client.post(
            "/pipeline/session/plan/commit/",
            data=json.dumps({**body, "rows": [{"antibody": "THEIRS-1"}]}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ExperimentSession.objects.using(DB).exists())

    def test_another_sites_nomination_is_refused_on_the_target_board(self):
        nom = TargetNomination.objects.using(DB).create(
            target=self.target, site=self.leicester)
        resp = self.client.post("/pipeline/targets/board/patch/",
                                {"target_id": self.target.pk, "nomination_id": nom.pk,
                                 "field": "comments", "value": "mine now"})
        self.assertEqual(resp.status_code, 403)

    def test_a_view_that_catches_everything_still_says_whose_it_is(self):
        """The identity save answers any exception with a generic sentence; the
        refusal is carried on the request so it is the one the reader sees."""
        resp = self.client.post("/pipeline/antibodies/board/identity/save/",
                                {"antibody_id": self.theirs.pk,
                                 "catalogue_number": "THEIRS-2",
                                 "company": "Proteintech", "gene": "STMN2"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("belongs to Leicester", resp.json()["error"])
        self.theirs.refresh_from_db(using=DB)
        self.assertEqual(self.theirs.catalogue_number, "THEIRS-1")

    def test_a_second_site_adds_its_own_nomination_beside_the_imports(self):
        """Two sites may add their own data about one gene (owner, 29 Sep 2026).
        On a gene only the import records as Leicester's there is no nomination
        to edit, so a McGill Funded tick files McGill's own — it used to file a
        siteless one, which read as the gene's — and Leicester stays on the row."""
        theirs = Target.objects.using(DB).create(gene_name="ADRB2", site=self.leicester)
        resp = self.client.post("/pipeline/targets/board/patch/",
                                {"target_id": theirs.pk, "field": "funded",
                                 "value": "yes"})
        self.assertEqual(resp.status_code, 200)
        nom = TargetNomination.objects.using(DB).get(target=theirs)
        self.assertEqual((nom.site_id, nom.funded), (self.mcgill.pk, True))
        row = resp.json()["row"]
        self.assertEqual(row["sites"], ["McGill"])
        self.assertEqual(row["import_site_also"], "Leicester")

    def test_a_second_site_cannot_delete_a_gene_the_import_records_as_anothers(self):
        theirs = Target.objects.using(DB).create(gene_name="ADRB2", site=self.leicester)
        TargetNomination.objects.using(DB).create(target=theirs, site=self.mcgill)
        plan = self.client.post("/pipeline/records/delete/preview/",
                                {"kind": "target", "id": theirs.pk}).json()["plan"]
        self.assertFalse(plan["allowed"])
        self.assertIn("Leicester", plan["why"])
        self.assertIn("a superuser can delete any", plan["why"])

    def _shared(self):
        shared = Target.objects.using(DB).create(gene_name="SNCA", essential_gene="")
        TargetNomination.objects.using(DB).create(target=shared, site=self.leicester)
        TargetNomination.objects.using(DB).create(target=shared, site=self.mcgill)
        return shared

    def _set(self, target, field, value):
        return self.client.post("/pipeline/targets/board/patch/",
                                {"target_id": target.pk, "field": field, "value": value})

    def test_either_site_fills_a_blank_on_a_shared_gene(self):
        shared = self._shared()
        self.assertEqual(self._set(shared, "essential_gene", "NO").status_code, 200)
        self.assertEqual(self._set(shared, "f1000_date", "2026-09-01").status_code, 200)
        self.assertEqual(self._set(shared, "add_class", "Kinase").status_code, 200)

    def test_nobody_but_a_superuser_changes_or_clears_what_a_shared_gene_holds(self):
        shared = self._shared()
        Target.objects.using(DB).filter(pk=shared.pk).update(essential_gene="YES")
        for value in ("NO", ""):
            resp = self._set(shared, "essential_gene", value)
            self.assertEqual(resp.status_code, 403, value)
            self.assertIn("Leicester", resp.json()["error"])
        shared.refresh_from_db(using=DB)
        self.assertEqual(shared.essential_gene, "YES")
        self.assertEqual(self._set(shared, "remove_class", "Kinase").status_code, 403)
        self._superuser()
        self.assertEqual(self._set(shared, "essential_gene", "NO").status_code, 200)

    def test_a_gene_only_your_site_has_is_yours_to_correct(self):
        mine = Target.objects.using(DB).create(gene_name="GCG", essential_gene="YES")
        TargetNomination.objects.using(DB).create(target=mine, site=self.mcgill)
        self.assertEqual(self._set(mine, "essential_gene", "NO").status_code, 200)

    def test_a_gene_the_import_filed_under_your_own_site_still_saves(self):
        mine = Target.objects.using(DB).create(gene_name="GCG", site=self.mcgill)
        resp = self.client.post("/pipeline/targets/board/patch/",
                                {"target_id": mine.pk, "field": "funded",
                                 "value": "yes"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TargetNomination.objects.using(DB)
                        .filter(target=mine, funded=True).exists())
