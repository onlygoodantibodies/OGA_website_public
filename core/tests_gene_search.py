"""The way back, and the way sideways.

A gene page's only route back to the gene list was the logo, and the only way to
reach a different gene was to go home first and search there. `core/header.html`
now carries a bar with a back link and a gene search box on every page but the
homepage, fed by `/api/internal/genes/`.

These lock the two things that would break silently: the bar disappearing from
the gene page, and the index serving genes whose pages 404 (the same trap
`tests_public_genes.py` covers for the browser extension).
"""
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from pipeline.models import Antibody, Company, PublicationImage, Target


class GeneSearchBarTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()  # the index is cached for an hour
        company = Company.objects.create(name="Proteintech")
        self.target = Target.objects.create(
            protein_name="Synuclein", gene_name="SNCA", aliases="PARK1, NACP")
        ab = Antibody.objects.create(
            target=self.target, company=company, catalogue_number="10842-1-AP")
        PublicationImage.objects.create(
            antibody=ab, application_type="WB", image="pubs/snca_wb.png")

        # Named, but nothing published — must not reach the search index.
        Target.objects.create(protein_name="GAPDH", gene_name="GAPDH")

    # --- the bar ---

    def test_gene_page_offers_a_way_back_and_a_search_box(self):
        response = self.client.get(
            reverse("antibody_table", kwargs={"gene_name": "SNCA"}))
        html = response.content.decode()
        self.assertIn("oga-subnav-back", html)
        self.assertIn(f'href="{reverse("home")}"', html)
        self.assertIn("oga-genesearch-input", html)

    def test_homepage_keeps_only_its_own_search(self):
        """The homepage hero search already covers this — two boxes would clash."""
        html = self.client.get(reverse("home")).content.decode()
        self.assertNotIn("oga-genesearch-input", html)
        self.assertIn('class="search-bar"', html)

    def test_other_pages_carry_the_bar_too(self):
        html = self.client.get(reverse("about")).content.decode()
        self.assertIn("oga-genesearch-input", html)

    # --- the index behind it ---

    def test_index_lists_public_genes_with_aliases(self):
        response = self.client.get(reverse("gene_search_index"))
        self.assertEqual(response.status_code, 200)
        genes = response.json()["genes"]
        self.assertEqual(genes, [{"name": "SNCA", "aliases": "PARK1, NACP"}])

    def test_index_omits_genes_with_nothing_published(self):
        names = [g["name"] for g in
                 self.client.get(reverse("gene_search_index")).json()["genes"]]
        self.assertNotIn("GAPDH", names)

    def test_every_suggested_gene_has_a_live_page(self):
        """No suggestion may lead to a 404 — same guarantee as the extension."""
        for gene in self.client.get(reverse("gene_search_index")).json()["genes"]:
            with self.subTest(gene=gene["name"]):
                response = self.client.get(
                    reverse("antibody_table", kwargs={"gene_name": gene["name"]}))
                self.assertEqual(response.status_code, 200)
