"""The internal gene record says what the public page for that gene holds.

Asked for 25 Sep 2026: a completed gene's record had no way to the public site
and no statement of what was on it. The numbers must be the public page's own —
same verdicts — or the two pages disagree about one gene.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from pipeline.models import Antibody, Company, PublicationImage, Site, Target
from pipeline.public import public_summary
from pipeline.tests_timeouts import DB, _member_client


class TheGeneRecordSummarisesItsPublicPageTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.client = _member_client(self, self.site)
        company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="NR4A2")
        good = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="ab1",
            wb_recommended=True)
        bad = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="ab2",
            out_of_market=True)
        for ab in (good, bad):
            PublicationImage.objects.using(DB).create(
                antibody=ab, application_type="WB", image=f"pubs/{ab.pk}.png")
        PublicationImage.objects.using(DB).create(
            antibody=bad, application_type="ICC-IF", image="pubs/if.png")

    def test_counts_per_application_follow_the_public_verdicts(self):
        s = public_summary(self.target)
        self.assertEqual(s["antibody_count"], 2)
        self.assertEqual(s["discontinued"], 1)
        self.assertEqual(s["application_list"], "WB and ICC-IF")
        rows = {r["application"]: r for r in s["applications"]}
        self.assertEqual(set(rows), {"WB", "ICC-IF"})
        self.assertEqual((rows["WB"]["figures"], rows["WB"]["supportive"],
                          rows["WB"]["not_supportive"]), (2, 1, 1))
        self.assertEqual((rows["ICC-IF"]["figures"], rows["ICC-IF"]["supportive"]),
                         (1, 0))

    def test_the_record_links_to_the_public_page(self):
        body = self.client.get(
            reverse("pipeline:target_detail", args=[self.target.pk])
        ).content.decode()
        self.assertIn("On the public site", body)
        self.assertIn(f'href="{reverse("antibody_table", kwargs={"gene_name": "NR4A2"})}"', body)

    def test_a_gene_with_nothing_released_says_so(self):
        bare = Target.objects.using(DB).create(gene_name="STMN2")
        self.assertIsNone(public_summary(bare))
        body = self.client.get(
            reverse("pipeline:target_detail", args=[bare.pk])).content.decode()
        self.assertIn("Not on the public site yet", body)
