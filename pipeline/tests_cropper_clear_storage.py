"""Clear storage on the cropper (owner, 28 Sep 2026) — every saved session and
every old unattached upload, in one press.

Pinned are the ways it would be wrong silently: deleting a set other than the
one listed, taking a figure out from under a run being built right now,
leaving the file behind when the row goes, and a member who is not an
administrator deleting other people's sessions.
"""
from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pipeline.models import CropperImage, CropperSession, Site
from pipeline.tests_timeouts import DB, _member_client

MANIFEST = reverse("pipeline:cropper_storage_manifest")
CLEAR = reverse("pipeline:cropper_storage_clear")


def _image(session, name, *, age=timedelta(0)):
    im = CropperImage(session=session, name=name, application_type="WB")
    im.image.save(name, ContentFile(b"\x89PNG not really"), save=False)
    im.save(using=DB)
    if age:
        CropperImage.objects.using(DB).filter(pk=im.pk).update(created_at=timezone.now() - age)
    return im


class ClearStorageTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.client = _member_client(self, Site.objects.using(DB).create(name="McGill", short_code="MCG"))
        mine = CropperSession.objects.using(DB).create(owner_username="carl", gene="PARP1")
        theirs = CropperSession.objects.using(DB).create(owner_username="riham", gene="TP53")
        self.held = [_image(mine, "wb.png"), _image(theirs, "if.png")]
        self.orphan = _image(None, "never-saved.png", age=timedelta(hours=3))
        self.building = _image(None, "being-added-now.png")
        self.addCleanup(lambda: self.building.image.storage.delete(self.building.image.name))

    def _admin(self):
        User.objects.using("academy_db").filter(username="carl").update(is_superuser=True)

    def _post(self, stamp):
        return self.client.post(CLEAR, json.dumps({"stamp": stamp}), content_type="application/json")

    def test_a_member_who_is_not_an_administrator_is_refused_and_nothing_goes(self):
        self.assertEqual(self.client.get(MANIFEST).status_code, 403)
        self.assertEqual(self._post("anything").status_code, 403)
        self.assertEqual(CropperSession.objects.using(DB).count(), 2)
        self.assertEqual(CropperImage.objects.using(DB).count(), 4)

    def test_the_press_deletes_what_was_listed_rows_and_files_and_spares_a_run_in_progress(self):
        self._admin()
        m = self.client.get(MANIFEST).json()
        self.assertEqual({s["gene"] for s in m["sessions"]}, {"PARP1", "TP53"})
        self.assertEqual([o["name"] for o in m["orphans"]], ["never-saved.png"])
        self.assertEqual((m["files"], m["too_new"]), (3, 1))

        storage = self.orphan.image.storage
        gone = [im.image.name for im in self.held + [self.orphan]]
        r = self._post(m["stamp"])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["files_not_deleted"], [])

        self.assertFalse(CropperSession.objects.using(DB).exists())
        self.assertEqual(list(CropperImage.objects.using(DB).values_list("pk", flat=True)),
                         [self.building.pk])
        self.assertEqual([n for n in gone if storage.exists(n)], [])
        self.assertTrue(storage.exists(self.building.image.name))

    def test_a_set_that_moved_after_the_list_was_drawn_deletes_nothing(self):
        self._admin()
        stamp = self.client.get(MANIFEST).json()["stamp"]
        CropperSession.objects.using(DB).create(owner_username="riham", gene="SOD1")
        r = self._post(stamp)
        self.assertEqual(r.status_code, 409)
        self.assertIn("Nothing was deleted", r.json()["error"])
        self.assertEqual(CropperSession.objects.using(DB).count(), 3)
        self.assertEqual(CropperImage.objects.using(DB).count(), 4)
        self.assertTrue(self.orphan.image.storage.exists(self.orphan.image.name))

    def test_the_button_is_drawn_greyed_with_its_reason_for_a_member(self):
        page = self.client.get(reverse("pipeline:cropper")).content.decode()
        self.assertIn('id="clearstorage" class="sec" style="padding:5px 10px" disabled', page)
        self.assertIn("Clear storage needs an administrator account.", page)
        self._admin()
        page = self.client.get(reverse("pipeline:cropper")).content.decode()
        self.assertNotIn("clearstorage-why", page)


class DroppedFiguresLeaveNoFileTests(TestCase):
    """A figure dropped from the page, or removed from a saved session, is
    deleted file and all (28 Sep 2026) — a row deleted without its file leaves
    something nothing records, which not even Clear storage can find."""
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.client = _member_client(self, Site.objects.using(DB).create(name="McGill", short_code="MCG"))

    def _stage(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), "white").save(buf, "PNG")
        r = self.client.post(reverse("pipeline:cropper_stage_image"),
                             {"image": SimpleUploadedFile("fig.png", buf.getvalue(), "image/png"),
                              "app": "WB", "name": "fig.png"})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _discard(self, token):
        return self.client.post(reverse("pipeline:cropper_discard_image"),
                                json.dumps({"token": token}), content_type="application/json")

    def test_an_unsaved_figure_dropped_from_the_page_is_deleted_with_its_file(self):
        d = self._stage()
        im = CropperImage.objects.using(DB).get(pk=d["id"])
        storage, name = im.image.storage, im.image.name
        self.assertTrue(storage.exists(name))
        self.assertEqual(self._discard(d["discard_token"]).json()["deleted"], True)
        self.assertFalse(CropperImage.objects.using(DB).filter(pk=d["id"]).exists())
        self.assertFalse(storage.exists(name))

    def test_a_figure_a_saved_session_holds_is_left_alone(self):
        d = self._stage()
        sess = CropperSession.objects.using(DB).create(owner_username="carl", gene="PARP1")
        CropperImage.objects.using(DB).filter(pk=d["id"]).update(session=sess)
        self.addCleanup(lambda: CropperImage.objects.using(DB).get(pk=d["id"]).image.delete(save=False))
        self.assertEqual(self._discard(d["discard_token"]).json()["deleted"], False)
        self.assertTrue(CropperImage.objects.using(DB).filter(pk=d["id"]).exists())

    def test_nobody_can_discard_a_figure_by_guessing_its_id(self):
        d = self._stage()
        self.addCleanup(lambda: CropperImage.objects.using(DB).get(pk=d["id"]).image.delete(save=False))
        self.assertEqual(self._discard(str(d["id"])).status_code, 400)
        self.assertTrue(CropperImage.objects.using(DB).filter(pk=d["id"]).exists())

    def test_removing_a_figure_from_a_saved_session_deletes_its_file(self):
        keep, drop = self._stage(), self._stage()
        save = reverse("pipeline:cropper_session_save")
        payload = {"gene": "PARP1", "images": [{"id": keep["id"]}, {"id": drop["id"]}]}
        sid = self.client.post(save, json.dumps(payload), content_type="application/json").json()["session_id"]
        dropped = CropperImage.objects.using(DB).get(pk=drop["id"])
        storage, name = dropped.image.storage, dropped.image.name
        self.addCleanup(lambda: CropperImage.objects.using(DB).get(pk=keep["id"]).image.delete(save=False))
        payload.update(session_id=sid, images=[{"id": keep["id"]}])
        self.client.post(save, json.dumps(payload), content_type="application/json")
        self.assertFalse(CropperImage.objects.using(DB).filter(pk=drop["id"]).exists())
        self.assertFalse(storage.exists(name))
        self.assertTrue(CropperImage.objects.using(DB).filter(pk=keep["id"]).exists())
