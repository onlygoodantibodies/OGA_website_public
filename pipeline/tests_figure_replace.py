"""Replace a figure: a second door into the review queue, not a way to publish.

Pinned: the silent wrong writes. A replacement that published without release,
one that quietly un-recommended the antibody at release (``release`` writes the
pending row's verdict onto the antibody, and ``stage`` defaults it to False),
and a file that is not an image being queued as one. Not pinned: the page.
"""
from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from pipeline.models import (Antibody, Company, PendingPublicationImage,
                             PublicationImage, Site, Target)
from pipeline.services import figure_replace
from pipeline.services import review
from pipeline.services.cropper import engine
from pipeline.tests_timeouts import DB, _member_client


def _png(mode="RGB", colour=(200, 30, 30), size=(60, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, colour).save(buf, "PNG")
    return buf.getvalue()


class ReplaceAFigureTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.client = _member_client(self, self.site)
        company = Company.objects.using(DB).create(name="Cell Signaling Technology")
        self.target = Target.objects.using(DB).create(gene_name="RAB6A")
        self.ab = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="9625",
            site=self.site, ip_recommended=True)
        # The live figure sits at an older key, as the imported ones do.
        self.live = PublicationImage.objects.using(DB).create(
            antibody=self.ab, application_type="IP",
            image="experiments/RAB6A_9625_IP.png", control_genotype="KD")

    def _upload(self, content, app="IP"):
        return self.client.post("/pipeline/figures/replace/upload/", {
            "antibody_id": self.ab.pk, "application_type": app, "q": "9625",
            "image": SimpleUploadedFile("new.png", content, "image/png")})

    def test_staging_publishes_nothing_and_keeps_the_verdict_and_control(self):
        response = self._upload(_png())
        self.assertEqual(response.status_code, 302)
        item = PendingPublicationImage.objects.using(DB).get()
        self.live.refresh_from_db()
        self.assertEqual(self.live.image.name, "experiments/RAB6A_9625_IP.png",
                         "staging moved the public row — that is publication")
        self.assertTrue(item.recommended,
                        "releasing this would un-recommend the antibody")
        self.assertEqual(item.control_genotype, "KD")
        self.assertTrue(item.image.name.endswith("RAB6A_9625_IP.png"))

        review.release([item], actor="owner")
        self.ab.refresh_from_db()
        self.live.refresh_from_db()
        self.assertTrue(self.ab.ip_recommended)
        self.assertEqual(self.live.image.name, item.image.name)
        self.assertEqual(self.live.control_genotype, "KD")

    def test_a_not_recommended_verdict_also_rides_through(self):
        self._upload(_png(), app="WB")
        self.assertFalse(PendingPublicationImage.objects.using(DB).get().recommended)

    def test_transparency_is_flattened_onto_white(self):
        self._upload(_png("RGBA", (0, 0, 0, 0)))
        item = PendingPublicationImage.objects.using(DB).get()
        with item.image.open("rb") as fh:
            img = Image.open(fh)
            img.load()
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.getpixel((0, 0)), (255, 255, 255))

    def test_a_panel_lands_on_the_croppers_canvas(self):
        """A 220x692 strip stored at its own size drew narrower and taller than
        every cropped figure on the gene page."""
        self._upload(_png(size=(220, 692)), app="WB")
        item = PendingPublicationImage.objects.using(DB).get()
        with item.image.open("rb") as fh:
            img = Image.open(fh)
            img.load()
        self.assertEqual(img.size, (engine.CANVAS, engine.CANVAS))
        self.assertEqual(img.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(img.getpixel((engine.CANVAS // 2, engine.CANVAS // 2)),
                         (200, 30, 30))

    def test_a_file_that_is_not_an_image_is_refused_and_nothing_queued(self):
        response = self._upload(b"not an image at all")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "could not be read as an image",
                            status_code=400)
        self.assertFalse(PendingPublicationImage.objects.using(DB).exists())

    def test_search_finds_the_antibody_by_catalogue_number(self):
        response = self.client.get("/pipeline/figures/replace/?q=9625")
        self.assertContains(response, "RAB6A")
        self.assertContains(response, "Replace IP figure")
        self.assertContains(response, "Add WB figure")
        self.assertEqual(figure_replace.search("", ""), [])

    def test_the_count_says_what_it_matched(self):
        """`?gene=` alone printed "17 antibodies match ." (live, 29 Sep 2026)."""
        text = " ".join(self.client.get(
            "/pipeline/figures/replace/?gene=RAB6A").content.decode().split())
        self.assertIn("matches on RAB6A.", text)
        self.assertNotIn("match .", text)
