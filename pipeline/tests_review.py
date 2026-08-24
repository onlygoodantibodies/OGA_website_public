"""The review queue: cropping no longer publishes, and releasing does.

What is pinned here is the class of defect this whole change exists to prevent —
**a figure reaching the public website without anybody deciding it should** —
and its mirror, a figure that was released and did not arrive. Both are silent:
nothing raises, nothing goes red, and you find out weeks later by looking at the
website or by being told the page is empty.

Deliberately not pinned: the wording of any panel, the shape of the queue page,
or how many clicks a release takes. Those are the parts most likely to be
redesigned once a scientist has used them, and a test over them is the reason a
redesign feels expensive.
"""
from __future__ import annotations

import io
import json
from unittest import mock

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from pipeline.models import (Antibody, Company, CropperImage, CropperSession,
                             Member, PendingPublicationImage, PublicationImage,
                             Site, Target)
from pipeline.public import public_targets
from pipeline.services import review as svc
from pipeline.services.cropper import commit as commit_mod
from pipeline.services.cropper import engine
from pipeline.tests_timeouts import DB, _member_client


def _png(colour=(200, 30, 30), size=(120, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


class CroppingDoesNotPublishTests(TestCase):
    """The whole point, in one class.

    The cropper's commit used to write ``PublicationImage``, and a
    ``PublicationImage`` **is** publication — ``pipeline/public.py`` derives the
    public site from that relation. So this asserts the negative: after a
    commit, the gene is not public, the public feed does not carry the antibody,
    and the recommendation the human set is not on the antibody yet.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        self.session = self._session()

    def _session(self, catalogue="ab138501", recommend=True):
        session = CropperSession.objects.using(DB).create(
            owner_username="member", gene="TRPA1", cell_line="HAP1",
            antibody_list=catalogue,
            metadata={catalogue: {"company": "Abcam"}},
            recommended={catalogue: {"WB": recommend}})
        image = CropperImage(
            session=session, application_type="WB", name="fig1.png",
            nat_w=120, nat_h=120,
            # One band, one column → exactly one cell, "0_0".
            grid={"bounds": {"top": 0, "bottom": 120}, "hLines": [],
                  "bandLeft": [0], "bandRight": [120], "vLines": [[]]},
            mapping={"0_0": {"assigned": True, "ab": 0}})
        image.image.save("fig1.png", ContentFile(_png()), save=False)
        image.save(using=DB)
        return session

    def test_a_commit_stages_and_publishes_nothing(self):
        target, summary = commit_mod.apply(self.session, actor_member=self.member)

        self.assertEqual(summary["crops"], 1)
        self.assertEqual(summary["destination"], "review queue")
        self.assertEqual(
            PublicationImage.objects.using(DB).count(), 0,
            "the cropper wrote a published figure — that is publication with "
            "nobody deciding it should be")
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 1)

    def test_the_gene_does_not_become_public(self):
        commit_mod.apply(self.session, actor_member=self.member)
        self.assertFalse(
            public_targets().using(DB).filter(pk=self.target.pk).exists())
        # …and the public gene page says so, which is the surface a reader hits.
        self.assertEqual(self.client.get("/antibodies/TRPA1/").status_code, 404)

    def test_the_recommendation_is_held_until_release(self):
        """A recommendation on a gene changes the public verdict on every OTHER
        antibody on it (``core/recommendations.py::curated_gene_ids`` separates
        "tested and not recommended" from "never assessed" on exactly this), so
        writing it while withholding the figure is its own leak."""
        commit_mod.apply(self.session, actor_member=self.member)
        antibody = Antibody.objects.using(DB).get(catalogue_number="ab138501")
        self.assertFalse(antibody.wb_recommended)
        self.assertTrue(
            PendingPublicationImage.objects.using(DB).get().recommended)

    def test_recropping_revises_the_queued_crop_rather_than_queueing_a_second(self):
        commit_mod.apply(self.session, actor_member=self.member)
        commit_mod.apply(self.session, actor_member=self.member)
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 1)


class OneFigureIsDecodedAtATimeTests(TestCase):
    """A session's figures are decoded one at a time, never all at once.

    This is the only test here about a resource rather than a record, and it
    earns that because the failure took the live site down. ``apply`` cached
    every source figure it opened and released none, so peak memory was the sum
    of the session rather than its largest figure. On 14 Aug 2026 a six-figure
    session — two 7003×4961 flow panels at 99 MB decoded, plus four smaller
    ones, 282 MB in total — went over the 512 MB the web service has, on top of
    a 244 MB resting baseline. The container was killed mid-commit and
    restarted, and the browser got Render's HTML error page where it expected
    JSON.

    Counting *concurrent* opens rather than measuring memory is what makes this
    cheap and deterministic: peak decoded bytes is the thing that matters, and
    "how many figures are open at once" is that number divided by a constant
    nobody has to guess. Four small figures here reproduce the shape without
    allocating anything.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        Company.objects.using(DB).create(name="Abcam")
        Target.objects.using(DB).create(gene_name="TRPA1")
        self.session = CropperSession.objects.using(DB).create(
            owner_username="member", gene="TRPA1", cell_line="HAP1",
            antibody_list="ab138501",
            metadata={"ab138501": {"company": "Abcam"}})
        # One figure per application, which is the ordinary shape of a full
        # gene's worth of work and exactly what the live session was.
        for i, app in enumerate(("WB", "IP", "ICC-IF", "FC")):
            image = CropperImage(
                session=self.session, application_type=app, name=f"fig{i}.png",
                nat_w=120, nat_h=120, order=i,
                grid={"bounds": {"top": 0, "bottom": 120}, "hLines": [],
                      "bandLeft": [0], "bandRight": [120], "vLines": [[]]},
                mapping={"0_0": {"assigned": True, "ab": 0}})
            image.image.save(f"fig{i}.png", ContentFile(_png()), save=False)
            image.save(using=DB)

    def test_the_commit_never_holds_two_source_figures_open_at_once(self):
        state = {"open": 0, "peak": 0, "opened": 0}
        real_open = commit_mod.Image.open

        def counting_open(fp, *args, **kwargs):
            im = real_open(fp, *args, **kwargs)
            state["open"] += 1
            state["opened"] += 1
            state["peak"] = max(state["peak"], state["open"])
            real_close = im.close

            def close():
                state["open"] -= 1
                real_close()
            im.close = close
            return im

        with mock.patch.object(commit_mod.Image, "open", counting_open):
            _, summary = commit_mod.apply(self.session, actor_member=self.member)

        self.assertEqual(summary["crops"], 4)
        self.assertEqual(
            state["opened"], 4,
            "every figure should still be opened — the fix is when they are "
            "released, not how many are read")
        self.assertEqual(
            state["peak"], 1,
            f"{state['peak']} source figures were decoded at the same time. "
            f"Peak memory is then the sum of the session rather than its "
            f"largest figure, which is what killed the live container "
            f"mid-commit — see services/cropper/commit.py::_by_figure")
        self.assertEqual(state["open"], 0, "a source figure was left open")


class DeletingASessionKeepsWhatIsQueuedTests(TestCase):
    """Deleting a cropper session leaves the crops it already sent to review.

    The confirm dialog promises exactly this — *"Crops you have already saved to
    the review queue are NOT affected"* — and nothing checked it. It is true
    because ``PendingPublicationImage.source_session`` is ``SET_NULL`` and the
    queued crop's bytes are its own file, not a reference to the session's
    source figure. Both of those are one edit away from being false, and if
    either changed the loss would be silent in the worst way: you delete a
    finished session to tidy up, the queue quietly shrinks, and nothing
    connects the two acts. A promise a page makes to a scientist about their
    own work is worth a test.

    Driven through the endpoint rather than ``session.delete()``, because the
    view is where the promise is printed and where the source figures are
    removed from storage — the half that could delete the wrong file.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        Company.objects.using(DB).create(name="Abcam")
        Target.objects.using(DB).create(gene_name="TRPA1")
        self.session = CropperSession.objects.using(DB).create(
            owner_username="carl", gene="TRPA1", cell_line="HAP1",
            antibody_list="ab138501",
            metadata={"ab138501": {"company": "Abcam"}},
            recommended={"ab138501": {"WB": True}})
        image = CropperImage(
            session=self.session, application_type="WB", name="fig1.png",
            nat_w=120, nat_h=120,
            grid={"bounds": {"top": 0, "bottom": 120}, "hLines": [],
                  "bandLeft": [0], "bandRight": [120], "vLines": [[]]},
            mapping={"0_0": {"assigned": True, "ab": 0}})
        image.image.save("fig1.png", ContentFile(_png()), save=False)
        image.save(using=DB)
        commit_mod.apply(self.session, actor_member=self.member)

    def test_the_queued_crop_survives_the_session_being_deleted(self):
        queued = PendingPublicationImage.objects.using(DB).get()
        self.assertEqual(queued.source_session_id, self.session.pk)

        response = self.client.post(
            "/pipeline/cropper/session/delete/",
            data=json.dumps({"session_id": self.session.pk}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CropperSession.objects.using(DB).filter(pk=self.session.pk).exists())

        queued.refresh_from_db()
        self.assertEqual(queued.status, PendingPublicationImage.Status.PENDING)
        self.assertIsNone(
            queued.source_session_id,
            "the crop should lose only its back-reference to the session")
        self.assertTrue(
            queued.recommended,
            "the human's verdict rides on this row and is applied at release")
        # The bytes, which are the whole point — a row pointing at a file the
        # session delete removed is the same loss with a record left behind.
        with queued.image.open("rb") as fh:
            self.assertTrue(fh.read(8).startswith(b"\x89PNG"))


class AnOldGeneNameDoesNotBecomeANewGeneTests(TestCase):
    """The cropper resolves an older symbol to the gene already in the pipeline.

    It asked ``gene_name__iexact`` and, finding nothing, offered to create the
    gene — so typing a renamed symbol into the tool that publishes figures would
    have made a *second* target for a gene already on file, with the antibodies
    and the published figures split across two rows that nothing joins. The
    public site is derived from the figure relation, so half a gene's evidence
    would simply stop being reachable, and nothing anywhere would have said so.

    Pinned at the commit rather than at the banner, because the banner is the
    warning and this is the write. The legend is here too: ``render_cell`` burns
    the gene into the pixels of every IF and FC crop, so a figure filed under
    LRRK2 carrying a caption reading PARK8 is published wrong and un-editably.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        Company.objects.using(DB).create(name="Abcam")
        self.lrrk2 = Target.objects.using(DB).create(
            gene_name="LRRK2", aliases="PARK8, DARDARIN")

    def _session(self, gene):
        session = CropperSession.objects.using(DB).create(
            owner_username="carl", gene=gene, cell_line="HAP1",
            antibody_list="ab138501",
            metadata={"ab138501": {"company": "Abcam"}})
        image = CropperImage(
            session=session, application_type="ICC-IF", name="fig1.png",
            nat_w=120, nat_h=120,
            grid={"bounds": {"top": 0, "bottom": 120}, "hLines": [],
                  "bandLeft": [0], "bandRight": [120], "vLines": [[]]},
            mapping={"0_0": {"assigned": True, "ab": 0}})
        image.image.save("fig1.png", ContentFile(_png()), save=False)
        image.save(using=DB)
        return session

    def test_committing_under_an_old_symbol_files_against_the_existing_gene(self):
        target, summary = commit_mod.apply(self._session("PARK8"),
                                           actor_member=self.member)

        self.assertEqual(target.pk, self.lrrk2.pk)
        self.assertEqual(
            Target.objects.using(DB).count(), 1,
            "a second target was created for a gene already in the pipeline")
        self.assertEqual(summary["target"], "update")
        self.assertEqual(summary["gene"], "LRRK2")
        self.assertEqual(summary["typed_gene"], "PARK8")
        self.assertEqual(summary["gene_matched_via"], "alias")
        # The antibody hangs off the gene on file, not off a new row.
        antibody = Antibody.objects.using(DB).get(catalogue_number="ab138501")
        self.assertEqual(antibody.target_id, self.lrrk2.pk)

    def test_the_crop_is_named_for_the_gene_it_is_filed_under(self):
        commit_mod.apply(self._session("PARK8"), actor_member=self.member)
        queued = PendingPublicationImage.objects.using(DB).get()
        self.assertIn(
            "LRRK2", queued.image.name,
            "the file is named for the typed symbol, not the gene it is under")
        self.assertNotIn("PARK8", queued.image.name)

    def test_an_ambiguous_old_name_is_refused_rather_than_guessed(self):
        """`CALM` is CALM1's everyday name and also on PICALM's synonym list.
        Guessing publishes a blot on the wrong gene's public page."""
        Target.objects.using(DB).create(gene_name="CALM1", aliases="CALM")
        Target.objects.using(DB).create(gene_name="PICALM", aliases="CALM")
        session = self._session("CALM")

        with self.assertRaises(commit_mod.Refused) as caught:
            commit_mod.apply(session, actor_member=self.member)
        self.assertIn("CALM1", str(caught.exception))
        self.assertIn("PICALM", str(caught.exception))
        self.assertEqual(
            PendingPublicationImage.objects.using(DB).count(), 0,
            "nothing should have been queued")
        self.assertFalse(
            Target.objects.using(DB).filter(gene_name="CALM").exists(),
            "the ambiguous name must not become a target of its own")

    def test_the_endpoint_refuses_it_too_and_says_why(self):
        """The guard is in `apply`, but the reader meets it here."""
        Target.objects.using(DB).create(gene_name="CALM1", aliases="CALM")
        Target.objects.using(DB).create(gene_name="PICALM", aliases="CALM")
        session = self._session("CALM")

        response = self.client.post(
            "/pipeline/cropper/commit/",
            data=json.dumps({"session_id": session.pk, "dry_run": False}),
            content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("CALM1", response.json()["error"])
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 0)

    def test_a_genuinely_new_gene_is_still_created(self):
        target, summary = commit_mod.apply(self._session("TRPA1"),
                                           actor_member=self.member)
        self.assertEqual(target.gene_name, "TRPA1")
        self.assertEqual(summary["target"], "create")
        self.assertEqual(summary["gene_matched_via"], "")

    def test_a_new_gene_is_created_in_the_conventional_case(self):
        """Uppercase is the rule and `orf` is the exception, so this goes
        through `gene_symbol.canonical` rather than being stored as typed —
        the write-path half of the eight mis-cased symbols already on file."""
        target, _ = commit_mod.apply(self._session("c9orf72"),
                                     actor_member=self.member)
        self.assertEqual(target.gene_name, "C9orf72")


class TheVerdictCanBeChangedAtTheMeetingTests(TestCase):
    """OGA's recommendation is editable in the queue, and only there does it wait.

    It could previously only be set in the cropper, by whoever cut the figure,
    alone, before anybody had looked at it — and the queue drew it as a
    read-only pill. So the one screen where a group looks at a blot together and
    decides whether the field should buy that antibody was the one screen that
    could not record the answer, and changing your mind meant re-cropping.

    The invariant underneath it is the one this whole table exists for: the flag
    moves on the **pending row** and never on the antibody, because
    ``core/recommendations.py::curated_gene_ids`` asks whether any antibody on a
    gene carries a recommendation at all — that is what separates *tested and
    not recommended* from *never assessed* on the public gene page — so an early
    write would move the public verdict on every other antibody on the gene
    while this figure is still withheld. Silent, and visible only on the public
    site.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        self.antibody = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="ab138501",
            site=self.site)
        self.item = svc.stage(
            antibody=self.antibody, application_type="WB", content=_png(),
            filename="TRPA1_ab138501_WB.png", recommended=False,
            staged_by="carl")

    def _post(self, ids, recommended):
        return self.client.post(
            "/pipeline/review/recommend/",
            data=json.dumps({"ids": ids, "recommended": recommended}),
            content_type="application/json")

    def test_a_member_can_add_a_recommendation(self):
        response = self._post([self.item.pk], True)
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertTrue(self.item.recommended)

    def test_a_member_can_remove_one(self):
        svc.set_recommended([self.item], True)
        self._post([self.item.pk], False)
        self.item.refresh_from_db()
        self.assertFalse(self.item.recommended)

    def test_it_does_not_touch_the_antibody(self):
        """The whole reason the pending table exists. Nothing public may move
        until somebody releases."""
        self._post([self.item.pk], True)
        self.antibody.refresh_from_db()
        self.assertFalse(
            self.antibody.wb_recommended,
            "the recommendation reached the antibody before release — that "
            "changes the public verdict on every other antibody on this gene")
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)

    def test_the_reply_redraws_the_row_and_the_manifest(self):
        """The card's verdict and the confirm panel's count are one press's
        worth of change, so both come back from the save."""
        data = self._post([self.item.pk], True).json()
        self.assertTrue(data["rows"][0]["recommended"])
        self.assertEqual(data["manifest"]["recommended"], 1)
        self.assertEqual(data["changed"], 1)

    def test_what_was_set_here_is_what_release_applies(self):
        """End to end: the meeting's answer, not the cropper's."""
        self._post([self.item.pk], True)
        svc.release(list(svc.pending_qs()), actor="carl")
        self.antibody.refresh_from_db()
        self.assertTrue(self.antibody.wb_recommended)

    def test_removing_it_here_un_recommends_at_release(self):
        """The other direction, which is the half a one-way flag would lose:
        `release` sets the field to `bool(item.recommended)`, so clearing it
        takes a recommendation off an antibody that had one."""
        self.antibody.wb_recommended = True
        self.antibody.save(using=DB)
        svc.set_recommended([self.item], True)
        self._post([self.item.pk], False)
        svc.release(list(svc.pending_qs()), actor="carl")
        self.antibody.refresh_from_db()
        self.assertFalse(self.antibody.wb_recommended)

    def test_a_released_figure_is_refused_by_name(self):
        """Its verdict is on the antibody now, so changing this row would
        silently do nothing — and the refusal says where to go instead."""
        svc.release([self.item], actor="carl")
        response = self._post([self.item.pk], True)
        # A released row is not in `pending_qs`, so the endpoint answers about
        # the set having moved on rather than pretending it changed something.
        self.assertIn(response.status_code, (403, 409))
        self.item.refresh_from_db()
        self.assertFalse(self.item.recommended)
        # The service-level refusal is the one that names the remedy.
        why = svc.recommend_refusal(self.item, self.member, False)
        self.assertIn("Set recommendations", why)

    def test_the_service_never_writes_a_released_row(self):
        svc.release([self.item], actor="carl")
        self.assertEqual(svc.set_recommended([self.item], True), 0)
        self.item.refresh_from_db()
        self.assertFalse(self.item.recommended)


class AnOversizedFigureIsRefusedNotDecodedTests(TestCase):
    """A figure too big to decode is refused, at both doors.

    `_by_figure` bounded the *peak* — one figure in memory instead of the whole
    session — but it does not bound the figure. One enormous scan would still
    take the container down, and that failure is the worst kind: the request
    dies, the browser gets the platform's HTML error page, and nothing on any
    screen says why. It is also the only failure here a person cannot act on
    without being told the reason, because "make the file smaller" is not a
    thing anybody guesses.

    Both doors, because they answer for different figures: the upload catches
    everything added from now on, and the commit catches what is already staged
    — including, had it been bigger, the session that started all this.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        Company.objects.using(DB).create(name="Abcam")
        Target.objects.using(DB).create(gene_name="TRPA1")

    def test_the_ceiling_leaves_room_for_the_figure_plus_the_app(self):
        """The number is derived, so it is checked rather than trusted.

        512 MB container, ~200 MB resting, 4 bytes a pixel. If a later edit
        raises the ceiling past what the box can hold, the guard stops guarding
        and the only symptom is the container dying again.
        """
        container_mb = 512
        resting_mb = 200
        decoded_mb = engine.MAX_MEGAPIXELS * 1_000_000 * 4 / (1024 * 1024)
        self.assertLess(
            resting_mb + decoded_mb, container_mb,
            f"a {engine.MAX_MEGAPIXELS:.0f} MP figure decodes to "
            f"{decoded_mb:.0f} MB, which does not fit beside the app in "
            f"{container_mb} MB")
        # …and it is not so tight that real work is refused: the largest figure
        # on file is 7003×4961.
        self.assertEqual(engine.size_refusal(7003, 4961, name="OGA_FC.png"), "")

    def test_the_refusal_says_the_size_the_limit_and_what_to_do(self):
        why = engine.size_refusal(12000, 9000, name="huge.png")
        self.assertIn("huge.png", why)
        self.assertIn("12000 × 9000", why)
        self.assertIn("108 megapixels", why)
        self.assertIn("60 megapixels", why)
        self.assertIn("Split it", why)
        # It reaches a bench scientist, so it names nothing they cannot act on.
        for leak in ("512", "MB", "container", "RAM", "MAX_MEGAPIXELS"):
            self.assertNotIn(leak, why)

    def test_the_upload_refuses_it_and_stores_nothing(self):
        from pipeline.models import CropperImage
        png = _png(size=(2, 2))
        upload = SimpleUploadedFile("huge.png", png, content_type="image/png")
        with mock.patch.object(engine, "MAX_MEGAPIXELS", 0.000001):
            response = self.client.post("/pipeline/cropper/stage-image/",
                                        {"image": upload, "app": "WB", "name": "huge.png"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("too big to crop", response.json()["error"])
        self.assertEqual(
            CropperImage.objects.using(DB).count(), 0,
            "the figure was stored despite being refused")

    def test_a_figure_already_staged_is_refused_at_the_commit(self):
        """The upload guard cannot reach what is already on file, and the
        session that started this was staged before it existed."""
        session = CropperSession.objects.using(DB).create(
            owner_username="carl", gene="TRPA1", cell_line="HAP1",
            antibody_list="ab138501",
            metadata={"ab138501": {"company": "Abcam"}})
        image = CropperImage(
            session=session, application_type="WB", name="OGA_FC_part 1.png",
            # Bigger than the ceiling — the shape of a real flow panel, doubled.
            nat_w=14006, nat_h=9922,
            grid={"bounds": {"top": 0, "bottom": 120}, "hLines": [],
                  "bandLeft": [0], "bandRight": [120], "vLines": [[]]},
            mapping={"0_0": {"assigned": True, "ab": 0}})
        image.image.save("fig.png", ContentFile(_png()), save=False)
        image.save(using=DB)

        _, items, match = commit_mod.build_plan(session)
        summary = commit_mod.summarize(session, None, items, match)
        self.assertIn("OGA_FC_part 1.png", summary["refusal"])

        with self.assertRaises(commit_mod.Refused):
            commit_mod.apply(session, actor_member=self.member)
        self.assertEqual(
            PendingPublicationImage.objects.using(DB).count(), 0,
            "an oversized figure was cropped anyway")

    def test_an_ordinary_session_is_untouched(self):
        """The guard must not become a tax on normal work."""
        session = CropperSession.objects.using(DB).create(
            owner_username="carl", gene="TRPA1", cell_line="HAP1",
            antibody_list="ab138501",
            metadata={"ab138501": {"company": "Abcam"}})
        image = CropperImage(
            session=session, application_type="WB", name="fig1.png",
            nat_w=3300, nat_h=2550,
            grid={"bounds": {"top": 0, "bottom": 120}, "hLines": [],
                  "bandLeft": [0], "bandRight": [120], "vLines": [[]]},
            mapping={"0_0": {"assigned": True, "ab": 0}})
        image.image.save("fig1.png", ContentFile(_png()), save=False)
        image.save(using=DB)
        _, summary = commit_mod.apply(session, actor_member=self.member)
        self.assertEqual(summary["refusal"], "")
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 1)


class ReleasingPublishesTests(TestCase):
    """The other direction: a release that does not arrive is just as silent."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        self.antibody = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab138501", site_id=self.site.pk)
        self.item = svc.stage(
            antibody=self.antibody, application_type="WB", content=_png(),
            filename="TRPA1_ab138501_WB.png", recommended=True,
            staged_by="member")

    def test_release_publishes_the_figure_and_applies_the_recommendation(self):
        result = svc.release([self.item], actor="member")

        self.assertEqual(len(result.released), 1)
        published = PublicationImage.objects.using(DB).get()
        self.assertEqual(published.antibody_id, self.antibody.pk)
        self.assertEqual(published.application_type, "WB")
        self.antibody.refresh_from_db()
        self.assertTrue(self.antibody.wb_recommended)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, svc.RELEASED)
        self.assertEqual(self.item.released_by, "member")

    def test_the_gene_becomes_public(self):
        self.assertEqual(svc.manifest([self.item])["new_public_genes"], ["TRPA1"])
        svc.release([self.item], actor="member")
        self.assertTrue(
            public_targets().using(DB).filter(pk=self.target.pk).exists())
        self.assertEqual(self.client.get("/antibodies/TRPA1/").status_code, 200)

    def test_a_set_that_grew_since_the_manifest_is_refused(self):
        """The number on the button is what was consented to — re-asking "may
        you?" at the commit answers yes while the list goes from one to ten."""
        second = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab2", site_id=self.site.pk)
        other = svc.stage(antibody=second, application_type="IP",
                          content=_png(), filename="TRPA1_ab2_IP.png")
        with self.assertRaises(svc.Refused):
            svc.release([self.item, other], actor="member", consented_count=1)
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)

    def test_a_release_is_re_runnable(self):
        svc.release([self.item], actor="member")
        self.item.refresh_from_db()
        again = svc.stage(antibody=self.antibody, application_type="WB",
                          content=_png((10, 10, 200)), filename="TRPA1_ab138501_WB.png")
        self.assertEqual(again.status, svc.PENDING,
                         "re-cropping a released figure queues the revision")
        # …and the public figure stands until the revision is released.
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)
        svc.release([again], actor="member")
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)

    def test_discarding_never_touches_the_public_site(self):
        svc.release([self.item], actor="member")
        self.item.refresh_from_db()
        with self.assertRaises(svc.Refused):
            svc.discard([self.item])
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)


class TheQueueIsReachableTests(TestCase):
    """A routed endpoint is not a reachable one, and a staged figure that no
    screen draws is a whole stage of the work nobody can find."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        antibody = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab138501", site_id=self.site.pk)
        self.item = svc.stage(antibody=antibody, application_type="WB",
                              content=_png(), filename="TRPA1_ab138501_WB.png",
                              staged_by="member")

    def test_the_queue_lists_the_gene(self):
        body = self.client.get("/pipeline/review/").content.decode()
        self.assertIn("TRPA1", body)

    def test_the_gene_page_shows_what_is_waiting(self):
        body = self.client.get(f"/pipeline/target/{self.target.pk}/").content.decode()
        self.assertIn("Figures awaiting release", body)
        self.assertIn("ab138501", body)

    def test_the_staged_image_is_served_by_this_app(self):
        """Never a bucket URL: an unreleased figure on a public URL is released
        to anybody who has the URL, whatever the database says."""
        response = self.client.get(f"/pipeline/review/image/{self.item.pk}/")
        self.assertEqual(response.status_code, 200)

    def test_releasing_through_the_page_publishes(self):
        """End to end through the endpoint, as the person who may press it.

        A superuser, because releasing is the review meeting's act
        (`OnlyAnAdministratorReleasesTests` below is where that rule is pinned);
        what this one is about is that the wiring behind the press works at all.
        """
        from django.contrib.auth.models import User
        from django.test import Client

        for alias in ("academy_db", DB):
            user = User(username="root", is_superuser=True, is_staff=True)
            user.set_password("pw")
            user.save(using=alias)
        pipeline_user = User.objects.using(DB).get(username="root")
        Member.objects.using(DB).create(user_id=pipeline_user.pk,
                                        site_id=self.site.pk, role="admin",
                                        is_active=True)
        client = Client()
        self.assertTrue(client.login(username="root", password="pw"))

        response = client.post(
            "/pipeline/review/release/",
            data={"ids": [self.item.pk], "count": 1},
            content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)


class OnlyAnAdministratorReleasesTests(TestCase):
    """Publishing is the review meeting's press, not a bench decision.

    Owner's call, 13 Aug 2026. Pinned from both ends because a permission check
    fails silently in either direction: a member who can publish is a figure on
    the website nobody decided to put there, and a superuser who cannot is a
    review meeting that ends with nothing released and no reason on the screen.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        antibody = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab138501", site_id=self.site.pk)
        self.item = svc.stage(antibody=antibody, application_type="WB",
                              content=_png(), filename="TRPA1_ab138501_WB.png",
                              staged_by="member")

    def _superuser_client(self):
        from django.contrib.auth.models import User
        from django.test import Client
        for alias in ("academy_db", DB):
            user = User(username="root", is_superuser=True, is_staff=True)
            user.set_password("pw")
            user.save(using=alias)
        pipeline_user = User.objects.using(DB).get(username="root")
        Member.objects.using(DB).create(user_id=pipeline_user.pk,
                                        site_id=self.site.pk, role="admin",
                                        is_active=True)
        client = Client()
        self.assertTrue(client.login(username="root", password="pw"))
        return client

    def _release(self, client):
        return client.post("/pipeline/review/release/",
                           data={"ids": [self.item.pk], "count": 1},
                           content_type="application/json")

    def test_a_member_is_refused_and_nothing_is_published(self):
        response = self._release(self.client)
        self.assertEqual(response.status_code, 403)
        self.assertIn("administrator", response.json()["error"])
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)

    def test_a_superuser_releases(self):
        response = self._release(self._superuser_client())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)

    def test_the_page_greys_the_button_with_the_reason_on_it(self):
        """Disabled with the reason on the page, never hidden and never on a
        `title` — a tooltip explains nothing on a touch screen, and a control
        that is simply absent is a feature a reader concludes does not exist."""
        body = self.client.get("/pipeline/review/?gene=TRPA1").content.decode()
        self.assertIn("Release to the public site", body)
        self.assertIn("administrator", body)
        # …and the queue itself is still readable by everybody: seeing what is
        # waiting is not the thing that was narrowed.
        self.assertIn("ab138501", body)


class WithdrawingPutsAFigureBackInTheQueueTests(TestCase):
    """A released figure can come back off the site, and the crop survives it.

    ``release`` could put a figure in front of the world and nothing could take
    it back. ``discard`` refuses a released row *because* the public figure would
    stay up, and pointed at a control on the antibodies board that has never
    existed; the only thing that removed a ``PublicationImage`` was deleting the
    whole antibody, which takes every reading with it.

    Three things here would be silently wrong rather than loud, which is why
    they are the ones pinned:

    * **The bytes.** The cropper wrote ``PublicationImage`` directly until this
      queue existed, so the oldest public figures have no pending row behind
      them — on live, all 31 figures on the four genes withdrawn on 22 Aug 2026
      were of that vintage. A withdrawal that deleted the row would destroy the
      only copy, and nothing would say so.
    * **The flag.** ``core/recommendations.py`` reads ``wb_recommended`` *first*,
      so an antibody left flagged after its figure went would keep reading
      *recommended* on the public API with no evidence behind it.
    * **A newer crop.** If somebody re-cropped since release, the queued row
      holds bytes the public one does not. Staging over it would throw their
      work away.

    Not pinned: the command's wording, or the shape of the receipt.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        self.antibody = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="ab138501",
            site=self.site)

    def _publish(self, application="WB", recommended=True, content=None):
        """A figure on the public site the way the old cropper left them: a
        ``PublicationImage`` with no pending row behind it at all."""
        image = PublicationImage.objects.using(DB).create(
            antibody=self.antibody, application_type=application,
            image=ContentFile(content or _png(), name=f"TRPA1_ab138501_{application}.png"))
        field = svc.RECOMMENDATION_FIELD[application]
        setattr(self.antibody, field, recommended)
        self.antibody.save(using=DB, update_fields=[field])
        return image

    def test_the_figure_leaves_the_public_site(self):
        image = self._publish()
        self.assertIn(self.target, list(public_targets()))
        svc.withdraw([image], actor="carl")
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)
        self.assertNotIn(self.target, list(public_targets()))

    def test_the_crop_survives_as_a_pending_row(self):
        """The whole safety of a withdrawal: it is a move, not a delete."""
        payload = _png(colour=(7, 99, 42))
        image = self._publish(content=payload)
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 0)

        svc.withdraw([image], actor="carl")

        queued = PendingPublicationImage.objects.using(DB).get()
        self.assertEqual(queued.status, PendingPublicationImage.Status.PENDING)
        self.assertEqual(queued.application_type, "WB")
        queued.image.open("rb")
        try:
            self.assertEqual(queued.image.read(), payload)
        finally:
            queued.image.close()

    def test_the_recommendation_comes_off_the_antibody_and_rides_back_on_the_row(self):
        image = self._publish(recommended=True)
        svc.withdraw([image], actor="carl")

        self.antibody.refresh_from_db()
        self.assertFalse(self.antibody.wb_recommended,
                         "the antibody still reads recommended with no figure behind it")
        self.assertTrue(PendingPublicationImage.objects.using(DB).get().recommended,
                        "the verdict was lost, so re-releasing would publish it unrecommended")

    def test_releasing_it_again_puts_it_back_exactly(self):
        """The round trip is the point — withdrawing is reversible by design."""
        image = self._publish(recommended=True)
        svc.withdraw([image], actor="carl")

        svc.release(list(PendingPublicationImage.objects.using(DB).all()), actor="carl")

        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)
        self.assertIn(self.target, list(public_targets()))
        self.antibody.refresh_from_db()
        self.assertTrue(self.antibody.wb_recommended)

    def test_a_newer_queued_crop_is_not_clobbered(self):
        image = self._publish()
        newer = _png(colour=(1, 2, 3))
        svc.stage(antibody=self.antibody, application_type="WB", content=newer,
                  filename="recropped.png", staged_by="sara")

        result = svc.withdraw([image], actor="carl")

        self.assertEqual(result.already_queued, 1)
        self.assertEqual(result.restaged, 0)
        queued = PendingPublicationImage.objects.using(DB).get()
        queued.image.open("rb")
        try:
            self.assertEqual(queued.image.read(), newer, "the re-crop was overwritten")
        finally:
            queued.image.close()
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)

    def test_it_names_the_genes_that_stop_being_public(self):
        """A count does not show it, and it is what a reader of the site sees."""
        wb, ip = self._publish("WB"), self._publish("IP")

        partial = svc.withdraw([wb], actor="carl")
        self.assertEqual(partial.genes_leaving_public, [],
                         "the gene still has a figure, so its page stays up")

        final = svc.withdraw([ip], actor="carl")
        self.assertEqual(final.genes_leaving_public, ["TRPA1"])

    def test_the_command_writes_nothing_without_apply(self):
        self._publish()
        call_command("withdraw_figures", "--gene", "TRPA1", stdout=io.StringIO())
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 0)

    def test_the_command_withdraws_the_gene_with_apply(self):
        self._publish()
        call_command("withdraw_figures", "--gene", "TRPA1", "--apply",
                     stdout=io.StringIO())
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 1)
        self.assertNotIn(self.target, list(public_targets()))

    def test_an_unknown_gene_is_refused_by_name(self):
        self._publish()
        with self.assertRaises(CommandError) as caught:
            call_command("withdraw_figures", "--gene", "TRPAI",
                         stdout=io.StringIO())
        self.assertIn("TRPAI", str(caught.exception))
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)


class TheGenePageOffersTheWithdrawalTests(TestCase):
    """The press exists on the screen where the judgement is made.

    Set recommendations is where somebody looks at a gene's figures and decides
    what the field should buy; deciding *none of this should be public yet* is
    the same judgement, so the control belongs beside it rather than on a board
    three clicks away.

    What is pinned is the pair that can disagree silently: the button is drawn
    from the same read that draws the cards, and the endpoint refuses exactly
    what the greyed button says it will. A control drawn armed that the server
    then refuses, or greyed where the server would have allowed it, is invisible
    until somebody presses it.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        self.antibody = Antibody.objects.using(DB).create(
            target=self.target, company=company, catalogue_number="ab138501",
            site=self.site, wb_recommended=True)
        PublicationImage.objects.using(DB).create(
            antibody=self.antibody, application_type="WB",
            image=ContentFile(_png(), name="TRPA1_ab138501_WB.png"))

    def _superuser_client(self):
        """Same shape as the release tests'. Deliberately local rather than a
        ``superuser=`` on the shared ``_member_client``: a change to a fixture
        every test in the app builds is a change to how every test runs."""
        from django.contrib.auth.models import User
        from django.test import Client
        for alias in ("academy_db", DB):
            user = User(username="root", is_superuser=True, is_staff=True)
            user.set_password("pw")
            user.save(using=alias)
        pipeline_user = User.objects.using(DB).get(username="root")
        Member.objects.using(DB).create(user_id=pipeline_user.pk,
                                        site_id=self.site.pk, role="admin",
                                        is_active=True)
        client = Client()
        self.assertTrue(client.login(username="root", password="pw"))
        return client

    def _manifest(self):
        resp = self.client.get("/pipeline/recommendations/antibodies/?gene=TRPA1")
        self.assertEqual(resp.status_code, 200)
        return resp.json()["withdraw"]

    def test_the_manifest_rides_with_the_cards(self):
        """One read draws both, so the count on the button cannot lag the grid."""
        manifest = self._manifest()
        self.assertEqual(manifest["figures"], 1)
        self.assertEqual(manifest["antibodies"], 1)
        self.assertEqual(manifest["applications"], ["WB"])
        self.assertEqual(manifest["recommendations"], 1)
        self.assertTrue(manifest["leaves_public_site"],
                        "the page must say the gene stops being a page at all")

    def test_a_member_is_told_why_the_button_is_off(self):
        why = self._manifest()["why_not"]
        self.assertTrue(why, "a member saw an armed button they cannot press")
        self.assertIn("administrator", why)

    def test_the_endpoint_refuses_a_member_too(self):
        """The greyed button and the refusal are one reader, or they drift."""
        resp = self.client.post(
            "/pipeline/recommendations/withdraw/",
            data=json.dumps({"gene": "TRPA1", "count": 1}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(PublicationImage.objects.using(DB).count(), 1)

    def test_a_superuser_withdraws_the_gene(self):
        self.client = self._superuser_client()
        resp = self.client.post(
            "/pipeline/recommendations/withdraw/",
            data=json.dumps({"gene": "TRPA1", "count": 1}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["genes_leaving_public"], ["TRPA1"])
        self.assertEqual(PublicationImage.objects.using(DB).count(), 0)
        self.assertEqual(PendingPublicationImage.objects.using(DB).count(), 1)

    def test_a_grown_set_is_refused_rather_than_withdrawn(self):
        """The number on the button is what was consented to."""
        self.client = self._superuser_client()
        PublicationImage.objects.using(DB).create(
            antibody=self.antibody, application_type="IP",
            image=ContentFile(_png(), name="TRPA1_ab138501_IP.png"))

        resp = self.client.post(
            "/pipeline/recommendations/withdraw/",
            data=json.dumps({"gene": "TRPA1", "count": 1}),   # the panel saw one
            content_type="application/json")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(PublicationImage.objects.using(DB).count(), 2,
                         "a set that grew underneath the panel was withdrawn anyway")


class OneObjectAtOnePublicKeyTests(TestCase):
    """Staged and published figures share one object, so deletes got dangerous.

    Owner's decision, 23 Aug 2026: a crop is written at its final public URL so
    partners can build on it before release. That removes the copy at release
    and, with it, the guarantee that "delete my file" only touched my file.
    Every delete in `review.py` can now blank a live gene page.

    These are the silent ones. Nothing raises when bytes vanish from a bucket —
    the row is still there, the page still renders, and the image is a broken
    icon that somebody notices weeks later.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        # A temp MEDIA_ROOT, or leftovers from an earlier run make the very
        # first crop come back suffixed and the assertions read as bugs.
        import tempfile
        self.enterContext(
            self.settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="review-keys-")))
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="TRPA1")
        self.ab = Antibody.objects.using(DB).create(
            catalogue_number="ab-key-1", company=self.company,
            target=self.target, site=self.site)

    def _stage(self, colour=(10, 20, 30)):
        return svc.stage(antibody=self.ab, application_type="WB",
                         content=_png(colour), filename="TRPA1_ab-key-1_WB.png",
                         staged_by="tester")

    def test_a_crop_is_written_at_the_public_key_not_a_pending_one(self):
        item = self._stage()
        self.assertTrue(item.image.name.startswith("publication_images/"),
                        f"staged at {item.image.name!r}")

    def test_release_points_at_the_same_object_and_moves_no_bytes(self):
        item = self._stage()
        staged_name = item.image.name
        svc.release([item], actor="tester", consented_count=1)
        live = PublicationImage.objects.using(DB).get(
            antibody=self.ab, application_type="WB")
        self.assertEqual(live.image.name, staged_name,
                         "release copied the bytes instead of pointing at them")
        self.assertTrue(live.image.storage.exists(staged_name))

    def test_discarding_a_recrop_does_not_blank_the_published_figure(self):
        """The sharp edge: re-cropping a released figure, then changing your mind."""
        item = self._stage()
        svc.release([item], actor="tester", consented_count=1)
        live_name = PublicationImage.objects.using(DB).get(
            antibody=self.ab, application_type="WB").image.name

        requeued = self._stage(colour=(90, 90, 90))   # back to pending, same key
        self.assertEqual(requeued.image.name, live_name)

        svc.discard([requeued], actor="tester")

        live = PublicationImage.objects.using(DB).get(
            antibody=self.ab, application_type="WB")
        self.assertTrue(
            live.image.storage.exists(live.image.name),
            "discarding the queue entry deleted the bytes the gene page serves")

    def test_withdraw_keeps_the_object_for_the_requeued_row(self):
        item = self._stage()
        svc.release([item], actor="tester", consented_count=1)
        live = PublicationImage.objects.using(DB).get(
            antibody=self.ab, application_type="WB")
        name = live.image.name

        svc.withdraw([live], actor="tester", consented_count=1)

        queued = PendingPublicationImage.objects.using(DB).get(
            antibody=self.ab, application_type="WB")
        self.assertEqual(queued.image.name, name)
        self.assertTrue(queued.image.storage.exists(name),
                        "withdrawal deleted the only copy of the figure")

    def test_a_recrop_replaces_the_object_rather_than_suffixing_it(self):
        """`file_overwrite=False` would make the 'final' URL not final."""
        first = self._stage()
        name = first.image.name
        second = self._stage(colour=(1, 2, 3))
        self.assertEqual(second.image.name, name,
                         "a re-crop landed on a suffixed key, so the public URL "
                         "a partner was given stopped being the live one")
