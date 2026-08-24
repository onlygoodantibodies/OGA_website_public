"""Changing what a record *is* — the last job the legacy pages were kept for.

The boards refuse catalogue, supplier and gene in a cell, and they are right to:
retyping one in a grid turns the row into a different reagent while every result
already recorded against it stays attached. But that refusal only works if there
is a "there" to send people to, and until now the there was the antibody and
cell-line edit pages — the last legacy pages alive, kept for this one job.

So what is pinned is that the replacement is *better* than the pages it removes,
not merely equivalent: it says what is attached before the change, it refuses a
change that would collide with an existing vial, and it goes through the same
writers everything else does.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             Member, PublicationImage, Site, Target)
from pipeline.tests_timeouts import DB, _member_client


class AntibodyIdentityTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.stmn2 = Target.objects.using(DB).create(gene_name="STMN2")
        self.elp3 = Target.objects.using(DB).create(gene_name="ELP3")
        self.company = Company.objects.using(DB).create(name="Proteintech")
        self.ab = Antibody.objects.using(DB).create(
            target_id=self.stmn2.pk, company_id=self.company.pk,
            catalogue_number="10586-1-AP", lot_number="20051",
            site_id=self.site.pk)

    def _load(self):
        resp = self.client.get("/pipeline/antibodies/board/identity/",
                               {"antibody_id": self.ab.pk})
        self.assertEqual(resp.status_code, 200)
        return resp.json()["identity"]

    def _save(self, **fields):
        data = {"antibody_id": self.ab.pk, "catalogue_number": "10586-1-AP",
                "company": "Proteintech", "gene": "STMN2"}
        data.update(fields)
        return self.client.post("/pipeline/antibodies/board/identity/save/", data)

    def test_it_says_what_is_attached_before_you_change_anything(self):
        """A number is the difference between renaming a thing and rewriting the
        history of eleven experiments."""
        self.assertEqual(self._load()["attached"],
                         {"results": 0, "figures": 0, "recommended": False})
        # update_or_create, never create: (antibody, application_type) is unique.
        PublicationImage.objects.using(DB).update_or_create(
            antibody_id=self.ab.pk, application_type="WB", defaults={})
        self.ab.wb_recommended = True
        self.ab.save(using=DB, update_fields=["wb_recommended"])
        attached = self._load()["attached"]
        self.assertEqual(attached["figures"], 1)
        self.assertIs(attached["recommended"], True)

    def test_the_identity_can_be_changed(self):
        resp = self._save(catalogue_number="10586-2-AP", gene="ELP3")
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["ok"], True)
        self.ab.refresh_from_db(using=DB)
        self.assertEqual(self.ab.catalogue_number, "10586-2-AP")
        self.assertEqual(self.ab.target_id, self.elp3.pk)

    def test_a_change_onto_an_existing_vial_is_refused_by_name(self):
        """unique_antibody_per_site_lot covers all five parts. Renaming one row
        onto another is an IntegrityError at best and a silent merge of two
        reagents at worst."""
        twin = Antibody.objects.using(DB).create(
            target_id=self.elp3.pk, company_id=self.company.pk,
            catalogue_number="24523-1-AP", lot_number="20051",
            site_id=self.site.pk)
        resp = self._save(catalogue_number="24523-1-AP", gene="ELP3")
        self.assertEqual(resp.status_code, 400)
        error = resp.json()["error"]
        self.assertIn("already a row", error)
        self.assertIn(str(twin.pk), error)
        self.ab.refresh_from_db(using=DB)
        self.assertEqual(self.ab.catalogue_number, "10586-1-AP")

    def test_a_supplier_is_resolved_not_duplicated(self):
        """Company.resolve, the same writer every other path uses — or one
        vendor ends up on file under two spellings."""
        before = Company.objects.using(DB).count()
        self._save(company="proteintech")
        self.assertEqual(Company.objects.using(DB).count(), before)

    def test_a_blank_catalogue_or_supplier_is_refused_with_a_reason(self):
        for field, word in (("catalogue_number", "catalogue number"),
                            ("company", "supplier")):
            with self.subTest(field=field):
                resp = self._save(**{field: ""})
                self.assertEqual(resp.status_code, 400)
                self.assertIn(word, resp.json()["error"])

    def test_an_unknown_gene_is_refused_rather_than_invented(self):
        """Correcting a record must not quietly put a new gene on the master
        list — there are already too many routes that mint a Target, and one
        hiding behind a typo here would be the worst of them."""
        before = Target.objects.using(DB).count()
        resp = self._save(gene="NOTAGENE")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("NOTAGENE", resp.json()["error"])
        # It names both doors now. Sending somebody to Add a gene as though it
        # were the only one is the claim the hub was corrected for.
        self.assertIn("target board", resp.json()["error"])
        self.assertEqual(Target.objects.using(DB).count(), before)
        self.ab.refresh_from_db(using=DB)
        self.assertEqual(self.ab.target_id, self.stmn2.pk)

    def test_the_save_returns_the_redrawn_row(self):
        """Same contract as every other patch: one row back, so the board
        redraws that row instead of refetching the grid."""
        row = self._save(catalogue_number="10586-2-AP").json()
        self.assertIs(row["matches"], True)
        self.assertEqual(row["row"]["catalogue"], "10586-2-AP")


class CellLineIdentityTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")
        self.wt = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk)
        self.ko = CellLine.objects.using(DB).create(
            name="HAP1 STMN2 KO", genotype="KO", target_id=self.target.pk,
            parent_line_id=self.wt.pk, site_id=self.site.pk)

    def _save(self, line, **fields):
        data = {"cell_line_id": line.pk, "name": line.name,
                "genotype": line.genotype,
                "gene": line.target.gene_name if line.target_id else "",
                "parent": line.parent_line.name if line.parent_line_id else ""}
        data.update(fields)
        return self.client.post("/pipeline/cell-lines/board/identity/save/", data)

    def test_it_counts_the_sessions_that_used_the_line(self):
        ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-07-30",
            site_id=self.site.pk, experimenter_id=self.member.pk,
            cell_line_wt_id=self.wt.pk, cell_line_ko_id=self.ko.pk)
        resp = self.client.get("/pipeline/cell-lines/board/identity/",
                               {"cell_line_id": self.ko.pk})
        self.assertEqual(resp.json()["identity"]["attached"]["sessions"], 1)

    def test_a_wild_type_line_may_not_be_given_a_gene(self):
        """The rule a person who never opens a guide gets wrong, and the one that
        makes a second, wrong line."""
        resp = self._save(self.wt, gene="STMN2")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("recorded once, with no gene", resp.json()["error"])

    def test_a_knockout_needs_a_gene_and_a_parent(self):
        self.assertIn("gene", self._save(self.ko, gene="").json()["error"])
        self.assertIn("parental line", self._save(self.ko, parent="").json()["error"])

    def test_a_parent_that_is_not_on_file_is_refused(self):
        resp = self._save(self.ko, parent="HAP9")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("HAP9", resp.json()["error"])

    def test_a_line_can_be_renamed(self):
        resp = self._save(self.ko, name="HAP1 STMN2 KO clone 2")
        self.assertEqual(resp.status_code, 200)
        self.ko.refresh_from_db(using=DB)
        self.assertEqual(self.ko.name, "HAP1 STMN2 KO clone 2")

    def test_an_unknown_genotype_is_refused(self):
        resp = self._save(self.ko, genotype="knockdown")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not a genotype", resp.json()["error"])


class TheLastLegacyPagesAreGoneTests(TestCase):
    """The antibody and cell-line detail and edit pages existed to do one thing
    the boards would not. They no longer do."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def test_their_names_no_longer_resolve(self):
        for name in ("antibody_detail", "antibody_edit",
                     "cell_line_detail", "cell_line_edit"):
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(f"pipeline:{name}", args=[1])

    def test_the_boards_offer_the_identity_dialog_instead(self):
        for url, endpoint in (
                ("/pipeline/antibodies/board/", "pipeline:antibody_identity"),
                ("/pipeline/cell-lines/board/", "pipeline:cell_line_identity")):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn(reverse(endpoint), body)
                self.assertIn('class="identity', body)
