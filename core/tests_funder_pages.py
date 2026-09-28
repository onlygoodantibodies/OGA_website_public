"""The MJFF page lists the public genes MJFF paid for, and nothing else.

The funder is not a column — `core/funders.py` maps label names onto it — so
what these pin is the half that can go silently wrong: a gene tagged only on
its nomination dropped (that was 9 of 25 on live data), an unpublished target
leaking onto a public page, the count on the page disagreeing with the rows
under it, and the logo drawn with no alt text.
"""
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from core import funders
from core.funders import MJFF, rows_for
from pipeline.models import (Antibody, Company, GrantingAgency, Project,
                             PublicationImage, Target, TargetNomination)


class MjffPageTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="Proteintech")
        mjff = GrantingAgency.objects.create(name="MJFF")
        both = GrantingAgency.objects.create(name="MJFF/GBA1 Canada")
        nih = GrantingAgency.objects.create(name="NIH")
        pd_proteins = Project.objects.create(name="PD Proteins")
        ad = Project.objects.create(name="AD Proteins")

        def published(target, catalogue, *apps):
            ab = Antibody.objects.create(
                target=target, company=company, catalogue_number=catalogue)
            for app in apps:
                PublicationImage.objects.create(
                    antibody=ab, application_type=app,
                    image=f"pubs/{catalogue}_{app}.png")
            return ab

        # Tagged on the Target row only — the 2019 import's shape.
        cls.prkn = Target.objects.create(
            protein_name="Parkin", gene_name="PRKN",
            project=pd_proteins, granting_agency=mjff)
        published(cls.prkn, "14060-1-AP", "WB", "IP")
        published(cls.prkn, "ab77924", "WB")
        Antibody.objects.create(  # logged, nothing published: not counted
            target=cls.prkn, company=company, catalogue_number="A00001")

        # Tagged on a nomination only — the live record's shape.
        cls.becn1 = Target.objects.create(
            protein_name="Beclin-1", gene_name="BECN1")
        TargetNomination.objects.create(
            target=cls.becn1, project=pd_proteins, granting_agency=mjff)
        published(cls.becn1, "11306-1-AP", "WB")

        # The Access value naming two funders at once.
        cls.ctsb = Target.objects.create(
            protein_name="Cathepsin B", gene_name="CTSB")
        TargetNomination.objects.create(target=cls.ctsb, granting_agency=both)
        published(cls.ctsb, "12216-1-AP", "ICC-IF")

        # MJFF-funded, nothing released yet: not public.
        cls.pink1 = Target.objects.create(
            protein_name="PINK1", gene_name="PINK1",
            project=pd_proteins, granting_agency=mjff)
        Antibody.objects.create(
            target=cls.pink1, company=company, catalogue_number="BC100-494")

        # Public, but another funder's gene.
        cls.mapt = Target.objects.create(
            protein_name="Tau", gene_name="MAPT", project=ad, granting_agency=nih)
        published(cls.mapt, "10274-1-AP", "WB")

    def setUp(self):
        self.url = reverse("funder_page", kwargs={"slug": "mjff"})

    # --- the reader ---

    def test_membership_reads_the_target_row_and_the_nominations(self):
        self.assertEqual([r.gene for r in rows_for(MJFF)], ["BECN1", "CTSB", "PRKN"])

    def test_antibody_count_is_the_published_ones(self):
        by_gene = {r.gene: r for r in rows_for(MJFF)}
        self.assertEqual(by_gene["PRKN"].antibodies, 2)
        self.assertEqual([c for c, _ in by_gene["PRKN"].applications], ["WB", "IP"])
        self.assertEqual([c for c, _ in by_gene["CTSB"].applications], ["ICC-IF"])

    def test_one_row_per_target_however_many_figures(self):
        self.assertEqual(funders.funder_targets(MJFF).count(), 3)

    # --- the page ---

    def test_the_page_counts_what_it_lists_and_links_each_gene(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(resp.context["count"], len(resp.context["rows"]))
        self.assertIn("3 genes", html)
        self.assertIn("4 antibodies", html)
        for gene in ("PRKN", "BECN1", "CTSB"):
            self.assertIn(reverse("antibody_table", kwargs={"gene_name": gene}), html)
        for gene in ("PINK1", "MAPT"):
            self.assertNotIn(gene, html)

    def test_the_page_carries_the_mjff_logo_with_alt_text(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("supporters/mjff", html)
        self.assertIn(f'alt="{escape(MJFF.logo_alt)}"', html)

    def test_the_page_names_itself_and_does_not_say_validated(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("<title>MJFF-funded genes — Only Good Antibodies</title>", html)
        self.assertNotIn("knockout-validated", html)

    def test_an_unknown_funder_is_a_404(self):
        resp = self.client.get(reverse("funder_page", kwargs={"slug": "wellcome"}))
        self.assertEqual(resp.status_code, 404)

    def test_a_page_with_nothing_public_still_renders(self):
        PublicationImage.objects.all().delete()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["count"], 0)
        self.assertIn("No gene on this list has public results yet",
                      resp.content.decode())

    # --- the ways in ---

    def test_the_home_page_and_partners_page_link_to_it(self):
        for name in ("home", "partners"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn(self.url, html, f"{name} does not link to the page")

    def test_the_sitemap_offers_it(self):
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertIn(self.url, xml)
