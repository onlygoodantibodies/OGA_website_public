"""IHC as a fifth figure application (26 Sep 2026) — cropped, released, drawn.

Pinned here are the ways it would go wrong **silently**:

* the cropper filing an IHC row under the wrong antibody, or stacking panels
  under labels that do not match them — the labels are burned into the pixels,
  so a knockout panel captioned "Mosaic" is published as evidence;
* an IHC figure failing on a verdict surface — the API, the manifest, the
  headline count, the review queue's recommendation — now that it carries one
  (the verdict itself is `tests_ihc_verdict`);
* the gene page failing to draw it, or drawing a column of "no data" for an
  application the gene never had.

Not pinned: the wording of the cropper's IHC controls or the caption. Those are
the parts a Cowork run is most likely to change.
"""
from __future__ import annotations

import io

from django.core.files.base import ContentFile
from django.test import TestCase
from PIL import Image

from pipeline.models import (Antibody, Company, CropperImage, CropperSession,
                             Member, PendingPublicationImage, PublicationImage,
                             Site, Target)
from pipeline.public import (gene_page_figures, headline_counts,
                             published_figures)
from pipeline.services import review as svc
from pipeline.services.cropper import commit as commit_mod
from pipeline.services.cropper import engine
from pipeline.tests_timeouts import DB, _member_client


def _png(size=(300, 200)) -> bytes:
    """Three coloured columns, so a crop from each is tellable apart."""
    im = Image.new("RGB", size, "white")
    w = size[0] // 3
    for i, colour in enumerate([(200, 30, 30), (30, 30, 200), (30, 160, 30)]):
        im.paste(colour, (i * w + 5, 5, (i + 1) * w - 5, size[1] - 5))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _grid(rows, cols, w=300, h=200):
    return {"bounds": {"top": 0, "bottom": h},
            "hLines": [h * i / rows for i in range(1, rows)],
            "bandLeft": [0] * rows, "bandRight": [w] * rows,
            "vLines": [[j / cols for j in range(1, cols)] for _ in range(rows)]}


class TheCropperStacksOneAntibodyPerRowTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        Company.objects.using(DB).create(name="Abcam")
        Target.objects.using(DB).create(gene_name="TP53")

    def _session(self, mapping, *, grid=None, antibodies="ab1101\nab16665"):
        session = CropperSession.objects.using(DB).create(
            owner_username="member", gene="TP53", cell_line="HAP1",
            antibody_list=antibodies)
        image = CropperImage(session=session, application_type="IHC",
                             name="fig7.png", nat_w=300, nat_h=200,
                             grid=grid or _grid(2, 3), mapping=mapping)
        image.image.save("fig7.png", ContentFile(_png()), save=False)
        image.save(using=DB)
        return session

    def _row(self, band, ab, cols=(0, 1, 2)):
        return {f"{band}_{c}": {"assigned": True, "ab": ab} for c in cols}

    def test_a_row_of_three_panels_is_one_crop_for_one_antibody(self):
        session = self._session({**self._row(0, 0), **self._row(1, 1)})
        _target, summary = commit_mod.apply(session, actor_member=self.member)
        self.assertEqual(summary["crops"], 2)
        staged = {p.antibody.catalogue_number: p.application_type
                  for p in PendingPublicationImage.objects.using(DB)}
        self.assertEqual(staged, {"ab1101": "IHC", "ab16665": "IHC"})

    def test_a_row_whose_panels_do_not_match_the_labels_is_refused_by_name(self):
        """Two panels under three labels would print the knockout as "TP53 KO"
        and the mosaic as nothing — or worse, the wrong way round."""
        session = self._session(self._row(0, 0, cols=(0, 1)))
        with self.assertRaises(commit_mod.Refused) as caught:
            commit_mod.apply(session, actor_member=self.member)
        self.assertIn("Row 1", str(caught.exception))
        self.assertIn("WT, TP53 KO, Mosaic", str(caught.exception))
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 0)

    def test_a_row_split_between_two_antibodies_is_refused(self):
        mapping = self._row(0, 0)
        mapping["0_2"]["ab"] = 1
        session = self._session(mapping)
        with self.assertRaises(commit_mod.Refused) as caught:
            commit_mod.apply(session, actor_member=self.member)
        self.assertIn("different antibodies", str(caught.exception))

    def test_labels_typed_for_the_figure_are_the_ones_counted(self):
        """PPP2R5D has no mosaic: two labels, two panels, accepted."""
        grid = {**_grid(2, 3), "ihcLabels": ["WT", "KO"]}
        session = self._session(self._row(0, 0, cols=(0, 1)), grid=grid)
        _target, summary = commit_mod.apply(session, actor_member=self.member)
        self.assertEqual(summary["crops"], 1)

    def test_a_figure_already_stacked_is_one_antibody_per_cell(self):
        """PPP2R5D's Figure 4: each antibody a column that labels itself."""
        grid = {**_grid(1, 3), "ihcLayout": "cell"}
        session = self._session({"0_0": {"assigned": True, "ab": 0},
                                 "0_1": {"assigned": True, "ab": 1}}, grid=grid)
        _target, summary = commit_mod.apply(session, actor_member=self.member)
        self.assertEqual(summary["crops"], 2)


class TheEngineStacksWithoutRescalingPanelsTests(TestCase):
    databases = set()

    def test_panels_keep_their_relative_size(self):
        """Each panel is pasted at its native size and only the stack is
        scaled, so two scale bars of one printed length stay one length."""
        src = Image.new("RGB", (400, 200), "white")
        src.paste((200, 30, 30), (0, 0, 200, 100))     # wide panel
        src.paste((30, 30, 200), (0, 100, 100, 200))   # half as wide
        stack, pieces = engine._stack_panels(
            src, [(0, 0, 200, 100), (0, 100, 100, 200)])
        self.assertEqual(pieces[0].width, 2 * pieces[1].width)
        self.assertEqual(stack.width, pieces[0].width)

    def test_no_labels_means_no_gutter(self):
        src = Image.new("RGB", (200, 100), (200, 30, 30))
        out = engine.render_cell(src, engine.Cell(
            0, 0, 200, 100, "IHC", "ab1", panels=[(0, 0, 200, 100)]))
        self.assertEqual(out.size, (engine.CANVAS, engine.CANVAS))


class AnIhcFigureCarriesItsVerdictTests(TestCase):
    """Released, it is on the gene page and every verdict surface, with a
    verdict of its own (26 Sep 2026 — it was a figure with none for one day;
    `tests_ihc_verdict` pins the verdict itself)."""

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.using(DB).create(name="Abcam")
        cls.target = Target.objects.using(DB).create(gene_name="TP53")
        cls.ab = Antibody.objects.using(DB).create(
            target=cls.target, company=company, catalogue_number="ab1101",
            wb_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=cls.ab, application_type="WB", image="pubs/tp53_WB.png")
        PublicationImage.objects.using(DB).create(
            antibody=cls.ab, application_type="IHC", image="pubs/tp53_IHC.png")

    def test_the_gene_page_draws_an_ihc_column_and_no_empty_ones(self):
        body = self.client.get("/antibodies/TP53/").content.decode()
        self.assertIn("pubs/tp53_IHC.png", body)
        self.assertIn('data-app="ihc"', body)
        self.assertNotIn('data-app="fc"', body)
        self.assertEqual(gene_page_figures(self.target), ("WB", "IHC"))

    def test_an_unflagged_ihc_figure_on_a_curated_gene_is_not_supportive(self):
        """The gene is curated (its WB is recommended) and IHC was not picked —
        the same rule as every other application, not "no verdict"."""
        body = self.client.get("/antibodies/TP53/").content.decode()
        self.assertNotIn("no OGA verdict for IHC", body)
        from core.api_views import _serialise_antibody
        record = _serialise_antibody(self.ab, True, include_recs=True)
        self.assertEqual(record["oga_support"]["IHC"], "not_supportive")

    def test_it_is_a_test_in_the_headline_and_the_api(self):
        self.assertEqual(published_figures().count(), 2)
        self.assertEqual(headline_counts()["experiment_count"], 2)
        from core.api_views import _serialise_antibody
        record = _serialise_antibody(self.ab, True, include_recs=True)
        self.assertEqual([e["experiment_type"] for e in record["experiments"]],
                         ["WB", "IHC"])

    def test_the_verdict_surfaces_do_not_fail_on_it(self):
        from core import not_supportive
        from core.api_manifest import _all_images
        self.assertEqual({i.application_type for i in _all_images()},
                         {"WB", "IHC"})
        not_supportive.reference_figures([self.target.gene_name])

    def test_the_review_queue_offers_a_recommendation_on_it(self):
        pending = PendingPublicationImage.objects.using(DB).create(
            antibody=self.ab, application_type="IHC", image="pubs/tp53_IHC.png")
        judged = svc.judgements([pending])[pending.pk]
        self.assertNotIn("no_verdict", judged)
        self.assertEqual(judged["public_support"], "not_supportive")
        self.assertEqual(svc.set_recommended([pending], True), 1)
        pending.refresh_from_db()
        self.assertEqual(svc.manifest([pending])["recommended"], 1)
