"""Raw files against a session — list, attach, download, remove.

Thin over ``services/attachments.py``, which owns every refusal.

The download goes through here rather than linking at storage directly, for two
reasons that are not style. A media URL is cross-origin once ``USE_R2`` is on,
so ``OGABoard.downloadWithReceipt`` — which fetches the bytes itself so the page
can name the file that arrived — would be blocked by CORS and report "that file
could not be built" about a file that is perfectly fine. And a raw gel scan is
lab data: served from here it is behind ``pipeline_member_required``, where a
bucket URL is a guessable public link.
"""
from __future__ import annotations

import logging

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import ExperimentSession, FileAttachment, Member
from pipeline.services import attachments as svc

logger = logging.getLogger(__name__)

DB = "pipeline_db"


def _asker(request):
    """Who is asking — their Member row, and whether they are a superuser.

    Same shape as ``views/deletion.py::_asker`` and for the same reason: what a
    person may attach to, and remove from, is their own bench's sessions.
    """
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        member = Member.objects.using(DB).select_related("site").get(
            user_id=pu.pk, is_active=True)
    except Exception:
        member = None
    return member, bool(request.user.is_superuser)


def _session(request, key="session_id"):
    src = request.POST if request.method == "POST" else request.GET
    return (ExperimentSession.objects.using(DB)
            .filter(pk=(src.get(key) or "").strip() or 0).first())


@pipeline_member_required
@require_GET
def attachment_list(request):
    """One session's files, plus the categories its procedure is likely to
    produce. Drawn on opening the session, and redrawn on its own after an
    upload or a removal — a file added does not change any result row, so
    nothing else on the panel needs to move.
    """
    session = _session(request)
    if session is None:
        return JsonResponse({"ok": False, "error": "unknown session"}, status=404)
    member, is_superuser = _asker(request)
    # Whether this person may add or remove here, and why not if not. The panel
    # greys its button with the reason **on the page** rather than hiding the
    # control: a control that is simply absent is a feature a reader concludes
    # does not exist, and a disabled one whose reason lives on `title` explains
    # nothing to a touch screen.
    why_not = svc.attach_refusal(session, member, is_superuser)
    return JsonResponse({
        "ok": True,
        "session_id": session.pk,
        "rows": svc.rows_for(session),
        "categories": svc.categories_for(session.procedure_type),
        # Which antibody rows a file can be filed against. Answered here rather
        # than read off whatever the calling page happens to have drawn, so the
        # sessions board and the gene page offer one list — see
        # ``attachments.result_options``.
        "results": svc.result_options(session),
        "max_mb": svc.max_mb(),
        "may_write": not why_not,
        "why_not": why_not,
    })


@pipeline_member_required
@require_POST
def attachment_upload(request):
    session = _session(request)
    if session is None:
        return JsonResponse({"ok": False, "errors": ["unknown session"]}, status=404)
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"ok": False, "errors": ["Choose a file first."]})
    member, is_superuser = _asker(request)
    try:
        result = svc.save(
            session, upload,
            category=(request.POST.get("category") or "").strip(),
            result_id=(request.POST.get("result_id") or "").strip() or None,
            description=request.POST.get("description") or "",
            member=member, is_superuser=is_superuser)
    except Exception:
        # Never the exception text on the page — it goes to the log, and the
        # reader gets a sentence they can act on.
        logger.exception("attachment upload failed for session %s", session.pk)
        return JsonResponse({"ok": False, "errors": [
            "That file could not be stored. Nothing has been changed — try "
            "again, and tell whoever looks after the site if it keeps "
            "happening."]}, status=500)
    return JsonResponse(result)


@pipeline_member_required
@require_POST
def attachment_delete(request):
    member, is_superuser = _asker(request)
    return JsonResponse(svc.remove((request.POST.get("id") or "").strip(),
                                   member=member, is_superuser=is_superuser))


@pipeline_member_required
@require_GET
def attachment_download(request, pk):
    att = FileAttachment.objects.using(DB).filter(pk=pk).first()
    if att is None or not att.file:
        return JsonResponse({"ok": False, "error": "That file is not on file."},
                            status=404)
    return FileResponse(att.file.open("rb"), as_attachment=True,
                        filename=att.original_filename or att.file.name.rsplit("/", 1)[-1])
