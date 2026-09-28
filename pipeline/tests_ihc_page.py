"""A gene's IHC page: whole report figures, private until released.

What is pinned is what would be silently wrong: a whole figure reachable
before release, a release that published a different set from the one
consented to, a withdrawal that left the bytes up, a gene leaving the site
while its IHC page stayed, a preview that counted a crop the save does not
cut, a figure whose bytes are not the source's, a meta description whose count
disagrees with the tables under it, and a link to a page that 404s.

Not pinned: the wording and layout of the page, which are the parts most likely
to change once a scientist has read one.
"""
from __future__ import annotations

import io
import json
import re
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import Client, TestCase
from PIL import Image

from pipeline.models import (Antibody, Company, CropperImage, CropperSession,
                             IhcFigure, IhcFigureAntibody, Member,
                             PendingIhcFigure, PendingIhcFigureAntibody,
                             PendingPublicationImage, PublicationImage, Site,
                             Target)
from pipeline.services import ihc_figures
from pipeline.services import review as svc
from pipeline.services.cropper import commit as commit_mod

DB = "pipeline_db"


def _jpeg(colour=(150, 90, 60), size=(64, 48), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, fmt)
    return buf.getvalue()


def _spec(label="Figure 4", legend="HAP1 WT and KO pellets. As published.",
          **extra):
    spec = ihc_figures.normalise({
        "on": True, "label": label, "legend": legend,
        "samples": {"hap1_pellets": True},
        "controls": ["he", "rabbit_secondary"]})
    spec.update(extra)
    return spec


class _Fixture(TestCase):
    databases = {DB, "academy_db"}

    def setUp(self):
        self.settings_ctx = self.settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="ihc-page-"))
        self.settings_ctx.enable()
        self.addCleanup(self.settings_ctx.disable)
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.company = Company.objects.using(DB).create(name="Cell Signaling")
        self.target = Target.objects.using(DB).create(gene_name="PPP2R5D")
        self.ab1 = Antibody.objects.using(DB).create(
            catalogue_number="5687", target=self.target, company=self.company,
            site=self.site, host_species="Rabbit")
        self.ab2 = Antibody.objects.using(DB).create(
            catalogue_number="MA5-18066", target=self.target, company=self.company,
            site=self.site, out_of_market=True)

    def _publish_gene(self, ab=None):
        """A published crop, which is what makes the gene public."""
        img = PublicationImage(antibody=ab or self.ab1, application_type="IHC")
        img.image.save("PPP2R5D_5687_IHC.png", ContentFile(_jpeg(fmt="PNG")), save=False)
        img.save(using=DB)
        return img

    def _stage(self, content=None, spec=None, antibodies=None):
        fig, _ = ihc_figures.stage(
            target=self.target, spec=spec or _spec(), content=content or _jpeg(),
            width=64, height=48, antibodies=antibodies or [self.ab1, self.ab2],
            staged_by="vera", control_genotype="KO")
        return fig

    def _release(self, figs, crops=()):
        with self.captureOnCommitCallbacks(using=DB, execute=True):
            return svc.release_all(list(crops), list(figs), actor="carl")

    def _session(self, antibodies=("5687",), crop=False, fmt="JPEG",
                 label="Figure 5"):
        session = CropperSession.objects.using(DB).create(
            owner_username="carl", gene="PPP2R5D", cell_line="HAP1",
            antibody_list="5687")
        self.source = _jpeg(fmt=fmt)
        im = CropperImage(
            session=session, application_type="IHC", name="fig5.jpg",
            nat_w=64, nat_h=48,
            grid={"bounds": {"top": 0, "bottom": 48}, "hLines": [],
                  "bandLeft": [0], "bandRight": [64], "vLines": [[]],
                  "ihcLayout": "cell", "ihcScale": "50 µm",
                  "ihcPage": {"on": True, "crop": crop, "label": label,
                              "legend": "Mouse tissue, as published.",
                              "samples": {"tissue": True, "tissue_species": "mouse",
                                          "tissue_organs": "lung"},
                              "controls": ["he"],
                              "antibodies": list(antibodies)}},
            # A mapped cell, so the preview would count a crop if it did not
            # ask `is_cropped`.
            mapping={"0_0": {"assigned": True, "ab": 0}})
        im.image.save("fig5.jpg", ContentFile(self.source), save=False)
        im.save(using=DB)
        return session

    def _client(self, superuser=False):
        for alias in ("academy_db", DB):
            u = User(username="carl", is_superuser=superuser)
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.using(DB).create(user_id=pu.pk, site_id=self.site.pk,
                                        role="admin", is_active=True)
        client = Client()
        self.assertTrue(client.login(username="carl", password="pw"))
        return client


class WholeFigureLifecycleTests(_Fixture):

    def test_private_until_release_then_copied_and_withdrawn_for_real(self):
        content = _jpeg()
        fig = self._stage(content)
        self.assertTrue(fig.image.name.startswith("ihc_figures_pending/"),
                        "a queued whole figure must be on the private prefix")
        self.assertEqual(IhcFigure.objects.using(DB).count(), 0)

        self._publish_gene()
        self._release([fig])
        live = IhcFigure.objects.using(DB).get()
        self.assertTrue(live.image.name.startswith("ihc_figures/"))
        with live.image.open("rb") as fh:
            self.assertEqual(fh.read(), content, "release must copy the bytes as they are")
        self.assertEqual([ab.pk for ab in ihc_figures.ordered_antibodies(live)],
                         [self.ab1.pk, self.ab2.pk])
        public_name = live.image.name
        storage = live.image.storage

        # A legend edit waiting in the queue does not reach the live page.
        self._stage(content, spec=_spec(legend="Edited legend."))
        live.refresh_from_db()
        self.assertNotEqual(live.legend, "Edited legend.")

        with self.captureOnCommitCallbacks(using=DB, execute=True):
            svc.withdraw_whole_figures([live], actor="carl", consented_count=1)
        self.assertFalse(IhcFigure.objects.using(DB).exists())
        self.assertFalse(storage.exists(public_name),
                         "withdrawing a whole figure must unpublish its bytes")
        pending = PendingIhcFigure.objects.using(DB).get()
        self.assertEqual(pending.status, "pending")
        self.assertTrue(pending.image.storage.exists(pending.image.name),
                        "the private copy is what makes re-release one press")

    def test_an_unchanged_figure_is_not_rewritten(self):
        content = _jpeg()
        fig = self._stage(content)
        _, written = ihc_figures.stage(
            target=self.target, spec=_spec(), content=content, width=64, height=48,
            antibodies=[self.ab1])
        self.assertFalse(written)
        self.assertEqual(PendingIhcFigure.objects.using(DB).get().pk, fig.pk)

    def test_two_labels_that_slug_alike_are_one_figure(self):
        self._stage(spec=_spec(label="Figure 4"))
        self._stage(spec=_spec(label="figure 4"))
        self.assertEqual(PendingIhcFigure.objects.using(DB).count(), 1)

    def test_a_format_a_browser_cannot_draw_is_refused_by_name(self):
        with self.assertRaises(ihc_figures.Refused) as ctx:
            self._stage(content=_jpeg(fmt="TIFF"))
        self.assertIn("TIFF", str(ctx.exception))
        self.assertFalse(PendingIhcFigure.objects.using(DB).exists())

    def test_discarding_a_released_figure_is_refused(self):
        self._publish_gene()
        fig = self._stage()
        self._release([fig])
        fig.refresh_from_db()
        with self.assertRaises(ihc_figures.Refused):
            ihc_figures.discard([fig])

    def test_a_discard_does_not_free_an_object_another_table_holds(self):
        fig = self._stage()
        name = fig.image.name
        IhcFigure.objects.using(DB).create(
            target=self.target, slug="other", label="Other", legend="x", image=name)
        with self.captureOnCommitCallbacks(using=DB, execute=True):
            ihc_figures.discard([fig])
        self.assertTrue(fig.image.storage.exists(name))

    def test_merge_drops_a_losers_link_the_survivor_already_has(self):
        fig = self._stage(antibodies=[self.ab1, self.ab2])
        ihc_figures.merge_links(self.ab1, [self.ab2])
        self.assertEqual(
            list(PendingIhcFigureAntibody.objects.using(DB).filter(figure=fig)
                 .values_list("antibody_id", flat=True)), [self.ab1.pk])


class OnePressOneConsentTests(_Fixture):

    def test_the_count_is_the_sum_of_crops_and_whole_figures(self):
        crop = svc.stage(antibody=self.ab1, application_type="IHC",
                         content=_jpeg(fmt="PNG"), filename="PPP2R5D_5687_IHC.png")
        fig = self._stage()
        with self.assertRaises(svc.Refused):
            svc.release_all([crop], [fig], actor="carl", consented_count=1)
        self.assertFalse(PublicationImage.objects.using(DB).exists())
        self.assertFalse(IhcFigure.objects.using(DB).exists())
        with self.captureOnCommitCallbacks(using=DB, execute=True):
            svc.release_all([crop], [fig], actor="carl", consented_count=2)
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)
        self.assertEqual(IhcFigure.objects.using(DB).count(), 1)

    def test_whole_figures_for_a_gene_with_no_page_are_refused_by_name(self):
        fig = self._stage()
        with self.assertRaises(svc.Refused) as ctx:
            svc.release_all([], [fig], actor="carl")
        self.assertIn("PPP2R5D", str(ctx.exception))
        self.assertFalse(IhcFigure.objects.using(DB).exists())
        self.assertEqual(svc.manifest([], [fig])["ihc_page_waits_for_gene"], ["PPP2R5D"])

    def test_a_gene_leaving_the_site_takes_its_whole_figures(self):
        img = self._publish_gene()
        fig = self._stage()
        self._release([fig])
        m = svc.withdraw_manifest(self.target.pk)
        self.assertEqual((m["figures"], m["whole_figures"], m["consent_count"]), (1, 1, 2))
        with self.assertRaises(svc.Refused):
            svc.withdraw([img], actor="carl", consented_count=1)
        with self.captureOnCommitCallbacks(using=DB, execute=True):
            result = svc.withdraw([img], actor="carl", consented_count=2)
        self.assertEqual(len(result.whole_figures_withdrawn), 1)
        self.assertFalse(IhcFigure.objects.using(DB).exists())

    def test_the_queue_index_lists_a_gene_whose_only_item_is_a_whole_figure(self):
        self._stage()
        waiting = svc.genes_waiting()
        self.assertEqual([(w["gene"], w["whole_figures"]) for w in waiting],
                         [("PPP2R5D", 1)])


class CropperWholeFigureTests(_Fixture):
    """A tissue-only figure: on the IHC page, and never cropped."""

    def test_a_figure_only_commit_cuts_nothing_and_copies_the_bytes(self):
        session = self._session()
        target, items, match = commit_mod.build_plan(session)
        summary = commit_mod.summarize(session, target, items, match)
        self.assertEqual((summary["crops"], summary["whole_figures"]), (0, 1))
        self.assertEqual(summary["refusal"], "")
        abs_before = Antibody.objects.using(DB).count()
        targets_before = Target.objects.using(DB).count()
        commit_mod.apply(session)
        self.assertFalse(PendingPublicationImage.objects.using(DB).exists())
        fig = PendingIhcFigure.objects.using(DB).get()
        with fig.image.open("rb") as fh:
            self.assertEqual(fh.read(), self.source, "the pixel rule: bytes as uploaded")
        self.assertEqual(fig.scale, "50 µm")
        self.assertEqual((fig.width, fig.height), (64, 48))
        self.assertEqual(Antibody.objects.using(DB).count(), abs_before)
        self.assertEqual(Target.objects.using(DB).count(), targets_before)

    def test_a_catalogue_not_on_file_is_refused_in_the_check_and_the_save(self):
        session = self._session(antibodies=("5687", "ab999"))
        target, items, match = commit_mod.build_plan(session)
        summary = commit_mod.summarize(session, target, items, match)
        self.assertIn("ab999", summary["refusal"])
        with self.assertRaises(commit_mod.Refused):
            commit_mod.apply(session)
        self.assertFalse(PendingIhcFigure.objects.using(DB).exists())

    def test_a_pellet_only_figure_is_refused_for_the_ihc_page(self):
        """Owner, 26 Sep 2026: the IHC page is for tissue. The pellet figure
        is in the report behind its DOI, and its pellets are cropped."""
        session = self._session()
        im = session.images.using(DB).get()
        im.grid["ihcPage"]["samples"] = {"hap1_pellets": True}
        im.save(using=DB)
        target, items, match = commit_mod.build_plan(session)
        self.assertIn("not ticked as showing tissue",
                      commit_mod.summarize(session, target, items, match)["refusal"])
        with self.assertRaises(commit_mod.Refused):
            commit_mod.apply(session)
        self.assertFalse(PendingIhcFigure.objects.using(DB).exists())

    def test_a_tiff_is_refused_before_the_save(self):
        session = self._session(fmt="TIFF")
        target, items, match = commit_mod.build_plan(session)
        self.assertIn("TIFF", commit_mod.summarize(session, target, items, match)["refusal"])

    def test_the_whole_figure_settings_survive_a_save_and_a_load(self):
        for alias in ("academy_db", DB):
            u = User(username="carl"); u.set_password("pw"); u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.using(DB).create(user_id=pu.pk, site_id=self.site.pk,
                                        role="admin", is_active=True)
        client = Client()
        self.assertTrue(client.login(username="carl", password="pw"))
        im = CropperImage(application_type="IHC", name="fig4.jpg", nat_w=64, nat_h=48)
        im.image.save("fig4.jpg", ContentFile(_jpeg()), save=False)
        im.save(using=DB)
        page = {"on": True, "crop": False, "label": "Figure 4", "legend": "L",
                "samples": {"hap1_pellets": True, "other_lines": "MCF7"},
                "controls": ["he"], "supplier_key": True,
                "antibodies": ["5687"], "source": ""}
        resp = client.post("/pipeline/cropper/session/save/", data=json.dumps({
            "gene": "PPP2R5D", "images": [{"id": im.pk, "app": "IHC", "name": "fig4.jpg",
                                           "grid": {"ihcPage": page}, "mapping": {}}]}),
            content_type="application/json")
        sid = resp.json()["session_id"]
        loaded = client.get(f"/pipeline/cropper/session/load/?id={sid}").json()
        self.assertEqual(loaded["images"][0]["grid"]["ihcPage"], page)


class PublicIhcPageTests(_Fixture):

    def _live(self, **spec_extra):
        self._publish_gene()
        fig = self._stage(spec=_spec(**spec_extra))
        self._release([fig])

    def test_no_page_without_released_whole_figures(self):
        self._publish_gene()
        self.assertEqual(self.client.get("/antibodies/PPP2R5D/ihc/").status_code, 404)

    def test_no_page_for_a_gene_that_is_not_public(self):
        fig = self._stage()
        # Released behind the gate's back, as if the crop had been withdrawn.
        with self.captureOnCommitCallbacks(using=DB, execute=True):
            ihc_figures.release([fig], actor="carl", written=[])
        self.assertEqual(self.client.get("/antibodies/PPP2R5D/ihc/").status_code, 404)

    def test_the_page_names_itself_and_counts_what_it_draws(self):
        self._live()
        resp = self.client.get("/antibodies/PPP2R5D/ihc/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("<title>PPP2R5D immunohistochemistry figures | Only Good Antibodies</title>", html)
        meta = re.search(r'<meta name="description" content="Immunohistochemistry figures for (\d+) ', html)
        drawn = set(re.findall(r'<td>\s*(?:<a href="[^"]*">)?([^<\s]+)', html.split("<tbody>", 1)[1]))
        self.assertEqual(int(meta.group(1)), 2)
        self.assertEqual(int(meta.group(1)), len({"5687", "MA5-18066"} & drawn))
        self.assertIn("knockout-controlled HAP1 cell pellets", html)
        self.assertIn("Discontinued by the supplier", html)
        self.assertIn("length not stated in the source report", html)
        self.assertIn("Legend as published", html)

    def test_the_tissue_sentence_is_drawn_only_when_a_figure_declares_tissue(self):
        self._live()
        self.assertNotIn("Tissue panels carry no OGA rating",
                         self.client.get("/antibodies/PPP2R5D/ihc/").content.decode())
        fig = PendingIhcFigure.objects.using(DB).get()
        fig.samples = {"tissue": True, "tissue_species": "mouse"}
        fig.status = "pending"
        fig.save(using=DB)
        self._release([fig])
        self.assertIn("Tissue panels carry no OGA rating",
                      self.client.get("/antibodies/PPP2R5D/ihc/").content.decode())

    def test_the_gene_page_links_only_to_a_page_that_exists(self):
        self._publish_gene()
        self.assertNotIn("/antibodies/PPP2R5D/ihc/",
                         self.client.get("/antibodies/PPP2R5D/").content.decode())
        fig = self._stage()
        self._release([fig])
        self.assertIn("/antibodies/PPP2R5D/ihc/",
                      self.client.get("/antibodies/PPP2R5D/").content.decode())

    def test_an_antibody_stained_in_tissue_links_there_from_its_hap1_result(self):
        """The pellets sit beside the antibody on the gene page; the tissue
        figure is one click away from that antibody's IHC cell — and only
        from an antibody the tissue figure shows."""
        self._publish_gene()
        self._release([self._stage(spec=_spec(samples={"tissue": True}),
                                   antibodies=[self.ab1])])
        html = self.client.get("/antibodies/PPP2R5D/").content.decode()
        self.assertEqual(html.count("See tissue staining"), 1)
        self.assertIn('href="/antibodies/PPP2R5D/ihc/#figure-4"', html)
        self.assertIn('id="figure-4"',
                      self.client.get("/antibodies/PPP2R5D/ihc/").content.decode())

    def test_a_figure_not_ticked_as_tissue_is_never_linked_as_tissue(self):
        self._publish_gene()
        self._release([self._stage(antibodies=[self.ab1])])
        self.assertNotIn("See tissue staining",
                         self.client.get("/antibodies/PPP2R5D/").content.decode())

    def test_the_sitemap_lists_only_genes_with_a_page(self):
        self._publish_gene()
        other = Target.objects.using(DB).create(gene_name="TP53")
        ab = Antibody.objects.using(DB).create(catalogue_number="x1", target=other,
                                               company=self.company, site=self.site)
        self._publish_gene(ab)
        fig = self._stage()
        self._release([fig])
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertIn("/antibodies/PPP2R5D/ihc/", xml)
        self.assertNotIn("/antibodies/TP53/ihc/", xml)


class NamedWhereTheyWouldBeLostTests(_Fixture):

    def test_deleting_an_antibody_names_the_whole_figures_showing_it(self):
        from pipeline.services import deletion
        self._stage(antibodies=[self.ab1])
        blockers = dict(deletion.plan_for_antibody(self.ab1).blockers)
        self.assertEqual(blockers["whole IHC figures showing it"], 1)

    def test_the_snapshot_names_a_table_the_database_does_not_have_yet(self):
        from pipeline.services import snapshot
        with mock.patch.object(snapshot, "_tables_on_file",
                               return_value={"pipeline_site"}):
            payload = snapshot.build(["Site", "IhcFigure"])
        self.assertIn("IhcFigure", payload["manifest"]["not_found"])
        self.assertIn("Site", payload["tables"])


class ReviewQueueEndpointsTests(_Fixture):
    """The endpoints the queue page drives — the bytes are gated, the press
    releases both kinds under one count, and every redraw carries the figures."""

    def test_queued_bytes_are_for_members_only(self):
        fig = self._stage()
        url = f"/pipeline/review/ihc-image/{fig.pk}/"
        self.assertNotEqual(Client().get(url).status_code, 200)
        self.assertEqual(self._client().get(url).status_code, 200)

    def test_one_press_releases_both_and_the_redraw_carries_the_figures(self):
        client = self._client(superuser=True)
        crop = svc.stage(antibody=self.ab1, application_type="IHC",
                         content=_jpeg(fmt="PNG"), filename="PPP2R5D_5687_IHC.png")
        fig = self._stage()
        rows = client.get(f"/pipeline/review/rows/?target_id={self.target.pk}").json()
        self.assertEqual([f["id"] for f in rows["figures"]], [fig.pk])
        m = client.post("/pipeline/review/manifest/",
                        data=json.dumps({"ids": [], "figure_ids": [fig.pk]}),
                        content_type="application/json").json()["manifest"]
        self.assertEqual(m["ihc_page_waits_for_gene"], ["PPP2R5D"])
        with self.captureOnCommitCallbacks(using=DB, execute=True):
            resp = client.post("/pipeline/review/release/", data=json.dumps(
                {"ids": [crop.pk], "figure_ids": [fig.pk], "count": 2}),
                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["whole_figures"], 1)
        self.assertIn("/antibodies/PPP2R5D/ihc/",
                      [u["url"] for u in resp.json()["public_urls"]])

    def test_withdrawing_one_whole_figure_needs_an_administrator(self):
        self._publish_gene()
        self._release([self._stage()])
        live = IhcFigure.objects.using(DB).get()
        resp = self._client().post("/pipeline/review/ihc-withdraw/", data=json.dumps(
            {"figure_ids": [live.pk], "count": 1}), content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(IhcFigure.objects.using(DB).exists())

    def test_whoever_staged_a_figure_may_discard_it(self):
        fig = self._stage()
        other = Site.objects.using(DB).create(name="UBC", short_code="UBC")
        member = mock.Mock(site_id=other.pk)
        self.assertEqual(ihc_figures.discard_refusal(fig, member, False, "vera"), "")
        self.assertNotEqual(ihc_figures.discard_refusal(fig, member, False, "ian"), "")
        self.assertEqual(
            ihc_figures.discard_refusal(fig, mock.Mock(site_id=self.site.pk), False, "ian"), "")


class CropperUploadsArePrivateTests(_Fixture):
    """A whole figure is copied from the cropper's upload, so the upload must
    not sit at a public address while the figure is private until release."""

    def test_the_cropper_draws_a_staged_figure_through_the_members_view(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        client = self._client()
        source = _jpeg()
        resp = client.post("/pipeline/cropper/stage-image/", {
            "image": SimpleUploadedFile("fig5.jpg", source, content_type="image/jpeg"),
            "app": "IHC", "name": "fig5.jpg"})
        self.assertEqual(resp.status_code, 200, resp.content)
        im = CropperImage.objects.using(DB).get()
        url = resp.json()["url"]
        self.assertEqual(url, f"/pipeline/cropper/image/{im.pk}/",
                         "the cropper must be handed the gated view, not a storage URL")
        got = client.get(url)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(b"".join(got.streaming_content), source)
        self.assertNotEqual(Client().get(url).status_code, 200)

    def test_new_uploads_go_private_and_older_ones_still_read(self):
        from django.core.files.storage import FileSystemStorage

        from pipeline import storages
        self.assertIs(CropperImage._meta.get_field("image")._storage_callable,
                      storages.cropper_storage)
        primary = FileSystemStorage(location=tempfile.mkdtemp(prefix="private-"))
        legacy = FileSystemStorage(location=tempfile.mkdtemp(prefix="public-"))
        legacy.save("cropper_staging/old.jpg", ContentFile(b"old"))
        st = storages._PrivateFirst(primary, legacy)
        name = st.save("cropper_staging/new.jpg", ContentFile(b"new"))
        self.assertTrue(primary.exists(name))
        self.assertFalse(legacy.exists(name), "a new upload reached the public bucket")
        with st.open("cropper_staging/old.jpg") as fh:
            self.assertEqual(fh.read(), b"old", "a session staged before this must still load")
        st.delete("cropper_staging/old.jpg")
        self.assertFalse(legacy.exists("cropper_staging/old.jpg"))
        with mock.patch.object(storages, "attachment_storage", return_value=primary):
            self.assertIsInstance(storages.cropper_storage(), storages._PrivateFirst)


class WholeFigureListsTheCropsRecordTests(_Fixture):

    def test_a_product_at_two_sites_is_listed_on_the_vial_holding_its_ihc_crop(self):
        other = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        vial2 = Antibody.objects.using(DB).create(
            catalogue_number="5687", target=self.target, company=self.company,
            site=other)
        self.assertGreater(vial2.pk, self.ab1.pk)
        self._publish_gene(ab=vial2)
        session = self._session(crop=True)
        plan = commit_mod.whole_figure_plan(session, self.target)
        self.assertEqual([ab.pk for ab in plan[0]["antibodies"]], [vial2.pk],
                         "the table must name the row the gene page shows as tested")


class ARelabelledFigureIsNotQueuedTwiceTests(_Fixture):

    def _relabel(self, session, label):
        im = session.images.using(DB).get()
        im.grid = dict(im.grid, ihcPage=dict(im.grid["ihcPage"], label=label))
        im.save(using=DB)

    def _summary(self, session):
        target, items, match = commit_mod.build_plan(session)
        return commit_mod.summarize(session, target, items, match)

    def test_the_old_label_is_named_and_taken_out_of_the_queue(self):
        session = self._session(label="Fig 5")
        commit_mod.apply(session)
        self._relabel(session, "Figure 5")
        summary = self._summary(session)
        self.assertEqual(summary["whole_figures_dropped"], ["“Fig 5”"])
        self.assertEqual(summary["whole_figures_left_live"], [])
        with self.captureOnCommitCallbacks(using=DB, execute=True):
            _, done = commit_mod.apply(session)
        self.assertEqual(done["whole_figures_discarded"], ["“Fig 5”"])
        self.assertEqual(list(PendingIhcFigure.objects.using(DB)
                              .values_list("label", flat=True)), ["Figure 5"])

    def test_a_released_old_label_is_named_and_left_on_the_page(self):
        self._publish_gene()
        session = self._session(label="Fig 5")
        commit_mod.apply(session)
        self._release(list(PendingIhcFigure.objects.using(DB).all()))
        self._relabel(session, "Figure 5")
        summary = self._summary(session)
        self.assertEqual(summary["whole_figures_dropped"], [])
        self.assertEqual(summary["whole_figures_left_live"], ["“Fig 5”"])
        commit_mod.apply(session)
        self.assertTrue(IhcFigure.objects.using(DB).filter(label="Fig 5").exists())


class FiguresReturningWithTheirGeneTests(_Fixture):

    def test_whole_figures_left_behind_are_named_when_the_gene_returns(self):
        img = self._publish_gene()
        self._release([self._stage()])
        # The gene leaves the site some other way than Withdraw.
        PublicationImage.objects.using(DB).filter(pk=img.pk).delete()
        crop = svc.stage(antibody=self.ab1, application_type="IHC",
                         content=_jpeg(fmt="PNG"), filename="PPP2R5D_5687_IHC.png")
        m = svc.manifest([crop])
        self.assertEqual(m["new_public_genes"], ["PPP2R5D"])
        self.assertEqual(m["whole_figures_returning"], [{"gene": "PPP2R5D", "count": 1}])
