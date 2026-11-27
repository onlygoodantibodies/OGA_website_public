"""A gene's page says how many papers cite each antibody, and never invents a zero.

Asked for on 23 Nov 2026: a site drafting a manuscript needed citations per
antibody and could not get them out of the pipeline, though the CiteAb snapshot
was already here (`core/citations.py::counts_by_rrid`).
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase

from core import citations
from pipeline.models import Antibody, Company, Site, Target
from pipeline.tests_timeouts import DB, _member_client


class GenePageCitationsTests(TestCase):
    databases = {"academy_db", "pipeline_db"}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="McGill")
        cls.target = Target.objects.using(DB).create(gene_name="TP53")
        company = Company.objects.using(DB).create(name="Santa Cruz")
        for cat, rrid in (("sc-126", "RRID:AB_628082"),   # in the snapshot
                          ("noid-1", ""),                  # no RRID at all
                          ("absent-1", "AB_999999999")):   # an RRID it does not hold
            Antibody.objects.using(DB).create(catalogue_number=cat, company=company,
                                              target=cls.target, site=cls.site, rrid=rrid)

    def test_each_row_says_a_count_or_why_there_is_none(self):
        expected = (citations.counts_by_rrid() or {}).get("AB_628082")
        self.assertTrue(expected, "the shipped snapshot holds sc-126")
        page = _member_client(self, self.site).get(f"/pipeline/target/{self.target.pk}/")
        html = page.content.decode()
        self.assertIn(f'>{expected["papers"]}</span>', html)   # RRID: prefix normalised
        self.assertIn("no RRID", html)
        self.assertIn("none found", html)
        # The two antibodies without a count must not be drawn as zero papers.
        self.assertNotIn('font-medium text-gray-900">0</span>', html)

    def test_a_missing_snapshot_is_named_not_read_as_no_papers(self):
        with mock.patch.object(citations, "counts_by_rrid", return_value=None):
            page = _member_client(self, self.site).get(f"/pipeline/target/{self.target.pk}/")
        html = page.content.decode()
        self.assertIn("citation data is not available", html)
        self.assertNotIn("none found", html)
