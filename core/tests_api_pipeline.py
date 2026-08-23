"""Pre-release data: your own reagents, and nothing else.

Everything these endpoints return is **unpublished**, so the failure they exist
to prevent is the worst kind this codebase has: one manufacturer seeing
another's figures before publication. That is silent — a 200 with rows in it
looks identical whether the scoping worked or not — so it is pinned from both
ends: the right rows are there, and the wrong rows are not.

The other half is the reverse leak. These figures must not appear on any
*published* surface until they are released, and the published feed lives one
module over with no idea this table exists. That is the point of the split
(``pipeline/models.py::PendingPublicationImage``), and this asserts it holds.
"""
from __future__ import annotations

import io

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from core.models import APIConsumer
from pipeline.models import Antibody, Company, PublicationImage, Report, Target
from pipeline.services import review as svc


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), (30, 90, 200)).save(buf, "PNG")
    return buf.getvalue()


class PreReleaseIsScopedToYourOwnReagentsTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        cls.abcam = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.proteintech = Company.objects.create(name="Proteintech")
        cls.target = Target.objects.create(gene_name="TRPA1")

        cls.theirs = Antibody.objects.create(
            target=cls.target, company=cls.abcam, catalogue_number="ab138501")
        cls.somebody_elses = Antibody.objects.create(
            target=cls.target, company=cls.proteintech,
            catalogue_number="10586-1-AP")

        cls.our_figure = svc.stage(
            antibody=cls.theirs, application_type="WB", content=_png(),
            filename="TRPA1_ab138501_WB.png", recommended=True)
        cls.their_figure = svc.stage(
            antibody=cls.somebody_elses, application_type="WB", content=_png(),
            filename="TRPA1_10586-1-AP_WB.png")

        cls.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")
        cls.unscoped = APIConsumer.objects.create(
            name="Antibody Registry", consumer_type="rrid")

    def _get(self, name, consumer, **params):
        return self.client.get(reverse(name), params,
                               HTTP_X_API_KEY=str(consumer.api_key))

    def test_you_see_your_own_pre_release_figures(self):
        body = self._get("api:pipeline_data", self.consumer).json()
        self.assertEqual(body["count"], 1)
        row = body["figures"][0]
        self.assertEqual(row["catalogue_number"], "ab138501")
        self.assertTrue(row["provisional_recommendation"])
        self.assertEqual(row["status"], "awaiting_release")

    def test_you_do_not_see_another_suppliers(self):
        body = self._get("api:pipeline_data", self.consumer).json()
        catalogues = {r["catalogue_number"] for r in body["figures"]}
        self.assertNotIn(
            "10586-1-AP", catalogues,
            "one manufacturer was shown another's unpublished figure")

    def test_a_key_with_no_supplier_scope_is_refused_by_name(self):
        """Unscoped is not a narrow scope — it is none. And the refusal says
        what still works, because a 403 with no way forward reads as the key
        having been switched off."""
        response = self._get("api:pipeline_data", self.unscoped)
        self.assertEqual(response.status_code, 403)
        detail = response.json()["detail"]
        self.assertIn("supplier scope", detail)
        self.assertIn("/api/v1/antibodies/", detail)

    def test_the_image_is_re_checked_against_the_scope(self):
        """A URL is not a permission, and this one is handed to an outside
        party."""
        mine = self.client.get(reverse("api:pipeline_image"),
                               {"id": self.our_figure.pk},
                               HTTP_X_API_KEY=str(self.consumer.api_key))
        self.assertEqual(mine.status_code, 200)

        theirs = self.client.get(reverse("api:pipeline_image"),
                                 {"id": self.their_figure.pk},
                                 HTTP_X_API_KEY=str(self.consumer.api_key))
        self.assertEqual(theirs.status_code, 404)

    def test_a_key_is_required(self):
        self.assertEqual(
            self.client.get(reverse("api:pipeline_data")).status_code, 401)
        self.assertEqual(
            self.client.get(reverse("api:pipeline_image"),
                            {"id": self.our_figure.pk}).status_code, 401)


class PreReleaseNeverReachesAPublishedSurfaceTests(TestCase):
    """The published feeds have no idea this table exists, and must not."""

    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.target = Target.objects.create(gene_name="TRPA1")
        cls.antibody = Antibody.objects.create(
            target=cls.target, company=cls.company, catalogue_number="ab138501")
        svc.stage(antibody=cls.antibody, application_type="WB", content=_png(),
                  filename="TRPA1_ab138501_WB.png", recommended=True)
        cls.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")

    def _get(self, name, **params):
        return self.client.get(reverse(name), params,
                               HTTP_X_API_KEY=str(self.consumer.api_key))

    def test_the_antibody_feed_does_not_carry_it(self):
        body = self._get("api:antibodies_feed", preview="true").json()
        self.assertEqual(body["count"], 0)

    def test_the_manifest_does_not_carry_it(self):
        body = self._get("api:manifest").json()
        self.assertEqual(body["counts"]["files_in_scope"], 0)
        self.assertEqual(body["files"], [])

    def test_it_appears_the_moment_it_is_released(self):
        svc.release(list(svc.pending_qs()), actor="root")
        cache.clear()
        self.assertEqual(self._get("api:antibodies_feed", preview="true")
                         .json()["count"], 1)
        self.assertEqual(PublicationImage.objects.count(), 1)


class GeneProgressTests(TestCase):
    """Where the genes a supplier has reagents in have got to."""

    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        cls.abcam = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.other = Company.objects.create(name="Proteintech")

        cls.mine = Target.objects.create(gene_name="TRPA1")
        cls.not_mine = Target.objects.create(gene_name="SNCA")

        cls.antibody = Antibody.objects.create(
            target=cls.mine, company=cls.abcam, catalogue_number="ab138501")
        Antibody.objects.create(
            target=cls.not_mine, company=cls.other, catalogue_number="10586-1-AP")

        svc.stage(antibody=cls.antibody, application_type="IP", content=_png(),
                  filename="TRPA1_ab138501_IP.png")
        PublicationImage.objects.create(
            antibody=cls.antibody, application_type="WB",
            image="publication_images/x.png")
        Report.objects.create(target=cls.mine, zenodo_doi="10.5281/zenodo.1",
                              zenodo_date="2026-08-01")

        cls.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")

    def _rows(self):
        return self.client.get(
            reverse("api:gene_progress"),
            HTTP_X_API_KEY=str(self.consumer.api_key)).json()["genes"]

    def test_it_lists_only_genes_you_have_reagents_in(self):
        genes = {r["gene"] for r in self._rows()}
        self.assertEqual(genes, {"TRPA1"})

    def test_it_reports_published_and_awaiting_per_application(self):
        row = self._rows()[0]
        self.assertTrue(row["applications"]["WB"]["published"])
        self.assertTrue(row["applications"]["IP"]["awaiting_release"])
        self.assertFalse(row["applications"]["IP"]["published"])
        self.assertEqual(row["awaiting_release"], 1)
        self.assertEqual(row["your_antibodies"], 1)

    def test_it_reports_the_published_report(self):
        row = self._rows()[0]
        self.assertEqual(row["report"]["status"], "published")
        self.assertEqual(row["report"]["doi"], "10.5281/zenodo.1")
        self.assertEqual(row["stage"], "reported")

    def test_it_does_not_count_another_suppliers_waiting_figures(self):
        """A supplier's own awaiting-release count is theirs alone: another
        company's unpublished figure is not a fact this may hand over."""
        theirs = Antibody.objects.create(
            target=self.mine, company=self.other, catalogue_number="10586-1-AP-2")
        svc.stage(antibody=theirs, application_type="FC", content=_png(),
                  filename="TRPA1_10586_FC.png")
        row = self._rows()[0]
        self.assertEqual(row["awaiting_release"], 1)
        self.assertFalse(row["applications"]["FC"]["awaiting_release"])


class ItDoesNotGrowAQueryPerGeneTests(TestCase):
    """`gene_progress` answers for a set, and the obvious implementation —
    `gene_progress.steps_for` in a loop — is six or seven queries per gene.

    Pinned as "the count does not grow with the row count" rather than as a
    fixed number, which is the form every other cost guard in this repo takes:
    N+1 is how these surfaces die, and it dies on the real dataset while looking
    fine on a handful of dev rows.
    """

    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")

    def _add(self, gene):
        target = Target.objects.create(gene_name=gene)
        antibody = Antibody.objects.create(
            target=target, company=self.company, catalogue_number=f"ab-{gene}")
        svc.stage(antibody=antibody, application_type="WB", content=_png(),
                  filename=f"{gene}_WB.png")
        PublicationImage.objects.create(
            antibody=antibody, application_type="IP", image=f"pubs/{gene}.png")
        Report.objects.create(target=target, zenodo_doi="10.5281/zenodo.1",
                              zenodo_date="2026-08-01")

    def _queries(self):
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        cache.clear()
        with CaptureQueriesContext(connections["pipeline_db"]) as ctx:
            self.client.get(reverse("api:gene_progress"),
                            HTTP_X_API_KEY=str(self.consumer.api_key))
        return len(ctx)

    def test_the_query_count_does_not_grow_with_the_gene_count(self):
        self._add("SNCA")
        one = self._queries()
        for gene in ("STMN2", "ELP3", "TRPA1", "PRKN"):
            self._add(gene)
        five = self._queries()
        self.assertEqual(
            one, five,
            f"one gene cost {one} queries and five cost {five} — this endpoint "
            f"grows with the dataset")
