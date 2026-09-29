"""Clear the cropper's working storage in one press — every saved session and
every upload no session holds (owner, 28 Sep 2026).

Cropper storage is working state only: a session is a gene's grid and mapping
with the figures uploaded to it, and an upload with no session is a figure
added and never saved (the tool stages it at once, so it survives a reload).
Nothing here reaches the review queue or a public page: crops and whole IHC
figures are written to their own keys when saved (`review.stage`,
`ihc_figures`), never pointed at these files.

Three things hold it:

* **Superusers only.** An unattached upload records no owner, so only somebody
  who may act for everyone can clear it — and clearing sessions is clearing
  other people's saved work.
* **A manifest, and the number on the button is what was consented to.** The
  page lists what will go and hands back a stamp of it; ``clear`` recomputes the
  set and refuses a different one, since "delete everything" pressed after a
  session was saved elsewhere would take work nobody saw listed.
* **An upload less than an hour old is left alone.** Every figure is
  unattached between being added and its session's first save, so a sweep
  with no age limit would pull a figure out from under a run in progress (two
  sessions cropping at once is expected). It is counted and said.

Files first, then rows — the order `views/cropper.py::cropper_session_delete`
already uses; a file that could not be deleted is named, never swallowed.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from pipeline.models import CropperImage, CropperSession

logger = logging.getLogger(__name__)
DB = "pipeline_db"
ORPHAN_MIN_AGE = timedelta(hours=1)


class Refused(Exception):
    """A sentence the page shows as it is."""


def delete_files(images) -> list[str]:
    """Delete each upload's file from storage; the names that would not go.

    A row's ``.delete()`` never touches its file, so every path that removes
    a ``CropperImage`` calls this first — a row deleted without it leaves a
    file nothing records, which not even Clear storage can find again.
    """
    kept = []
    for im in images:
        if not im.image:
            continue
        try:
            im.image.storage.delete(im.image.name)
        except Exception:
            logger.exception("cropper: could not delete %s", im.image.name)
            kept.append(im.name or im.image.name)
    return kept


def _stamp(session_ids, image_ids) -> str:
    raw = "s:" + ",".join(map(str, sorted(session_ids))) + "|i:" + ",".join(map(str, sorted(image_ids)))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _orphans(now):
    unattached = CropperImage.objects.using(DB).filter(session__isnull=True)
    old = unattached.filter(created_at__lt=now - ORPHAN_MIN_AGE).order_by("created_at")
    return list(old), unattached.filter(created_at__gte=now - ORPHAN_MIN_AGE).count()


def manifest(now=None) -> dict:
    """What one press would delete, in words the page prints, plus its stamp."""
    now = now or timezone.now()
    sessions = list(CropperSession.objects.using(DB).order_by("-updated_at"))
    counts = {s.pk: s.images.using(DB).count() for s in sessions}
    orphans, too_new = _orphans(now)
    session_images = sum(counts.values())
    return {
        "sessions": [{"id": s.pk, "gene": s.gene or "(no gene)", "owner": s.owner_username,
                      "images": counts[s.pk],
                      "updated_at": s.updated_at.isoformat()} for s in sessions],
        "orphans": [{"id": im.pk, "name": im.name or im.image.name,
                     "created_at": im.created_at.isoformat()} for im in orphans],
        "too_new": too_new,
        "files": session_images + len(orphans),
        "stamp": _stamp(counts, [im.pk for im in orphans]),
    }


def clear(stamp: str, now=None) -> dict:
    """Delete everything ``manifest`` listed — or nothing, if the set moved."""
    now = now or timezone.now()
    sessions = list(CropperSession.objects.using(DB).all())
    orphans, too_new = _orphans(now)
    if _stamp([s.pk for s in sessions], [im.pk for im in orphans]) != stamp:
        raise Refused("Nothing was deleted: the storage changed after the list was drawn "
                      "(a session was saved or a figure added). Press Clear storage again "
                      "to see what is there now.")
    images = list(CropperImage.objects.using(DB).filter(session__in=sessions)) + orphans
    kept = delete_files(images)
    with transaction.atomic(using=DB):
        CropperImage.objects.using(DB).filter(pk__in=[im.pk for im in orphans]).delete()
        CropperSession.objects.using(DB).filter(pk__in=[s.pk for s in sessions]).delete()
    return {"sessions": len(sessions), "files": len(images), "too_new": too_new,
            "files_not_deleted": kept}
