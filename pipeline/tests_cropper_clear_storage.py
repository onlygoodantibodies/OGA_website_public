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
