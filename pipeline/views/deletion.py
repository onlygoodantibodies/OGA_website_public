"""Deleting one record — preview, then confirm against what the preview said.

Two endpoints, both POST, because a GET that deletes is a link a crawler or a
prefetch can follow. The preview is a POST for the same reason it is a separate
call: it names what would go, and the commit re-asks every question rather than
trusting it (a preview is not a permission slip — the record may have gained
another site's antibody while the dialog was open). ``agreed`` carries the total
the panel put on the button, so a delete that overrides the refusals is checked
against what the person consented to and not merely against whether they are
still allowed.

All logic is ``services/deletion.py``; these are thin.
"""
from __future__ import annotations

import logging

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member
from pipeline.services import deletion

logger = logging.getLogger(__name__)

DB = "pipeline_db"


def _asker(request):
    """Who is asking — their Member row, and whether they are a superuser.

    Deleting is scoped to your own bench's records, so this is not decoration:
    it is the check. A superuser may remove any deletable record, which is what
    keeps a mistake at a site with nobody senior on it from needing a shell.
    """
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        member = Member.objects.using(DB).select_related("site").get(
            user_id=pu.pk, is_active=True)
    except Exception:
        member = None
    return member, bool(request.user.is_superuser)


@pipeline_member_required
@require_POST
def delete_preview(request):
    """What deleting this record would do, before anything happens."""
    try:
        member, is_superuser = _asker(request)
        p = deletion.plan(request.POST.get("kind", ""), request.POST.get("id"),
                          member=member, is_superuser=is_superuser)
    except deletion.Refused as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "plan": p.as_dict()})


@pipeline_member_required
@require_POST
def delete_commit(request):
    try:
        member, is_superuser = _asker(request)
        result = deletion.delete(request.POST.get("kind", ""),
                                 request.POST.get("id"),
                                 request.POST.get("confirm", ""),
                                 member=member, is_superuser=is_superuser,
                                 agreed=request.POST.get("agreed"))
    except deletion.Refused as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        logger.exception("delete failed (kind=%s id=%s)",
                         request.POST.get("kind"), request.POST.get("id"))
        return JsonResponse(
            {"ok": False,
             "error": "Could not delete that — nothing has been changed."},
            status=400)
    logger.warning("deleted %s %s (%s) by %s", result["kind"], result["id"],
                   result["label"], request.user.username)
    return JsonResponse({"ok": True, **result})
