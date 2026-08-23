"""The people board — who can sign in, and what they may do.

The fifth board, on the same contract as the other four
(``board_queryset`` → ``apply_filters`` → ``row_for`` → ``board_rows``, a patch
endpoint, and a ``parse → plan → apply`` pop-out), so ``board.js`` drives it
unchanged.

All the logic is ``services/members.py``, which is also what
``manage.py pipeline_users`` calls — one write path, two doors. These are thin.

Superuser-only (``pipeline_superuser_required``): this page creates logins and
sets ``is_staff``, which is site-wide raw access, not a pipeline permission.
"""
from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_superuser_required
from pipeline.services import board_page
from pipeline.services import members as member_svc

logger = logging.getLogger(__name__)

_FILTER_KEYS = ("q", "site", "role", "active", "imported")


def _filters(request) -> dict:
    """Same shape as every other board, ``?site=`` included — a name, a short
    code or a pk, spelled the way the filter form spells it."""
    from pipeline.services import sites as site_svc
    f = {k: (request.GET.get(k) or "").strip() for k in _FILTER_KEYS}
    f["site"] = site_svc.form_value(f["site"])
    return f


@pipeline_superuser_required
@require_GET
def user_board(request):
    opts = member_svc.filter_options()
    return render(request, "pipeline/user_board.html", {
        "filters": _filters(request),
        "sites": opts["sites"],
        "site_pks": [str(s.pk) for s in opts["sites"]],
        "roles": opts["roles"],
        "new_columns": member_svc.COLUMNS,
        "new_example": member_svc.EXAMPLE,
        # So the page can say "you cannot do that to yourself" before you try.
        "me": request.user.username,
    })


@pipeline_superuser_required
@require_GET
def user_board_rows(request):
    filters = _filters(request)
    page, per_page = board_page.read_params(request)
    data = member_svc.board_page(page=page, per_page=per_page,
                                 locate=board_page.locate_param(request), **filters)
    # Never silently. The Access import's own records are left out by default and
    # the chip says how many, so a short list cannot read as the whole list.
    return JsonResponse({"ok": True, **data,
                         "hidden_imported": member_svc.hidden_import_count(**filters)})


@pipeline_superuser_required
@require_POST
def user_board_patch(request):
    """Save one cell. A refusal we wrote is shown; a crash is logged, not shown."""
    member_id = request.POST.get("target_id") or request.POST.get("member_id")
    field = (request.POST.get("field") or "").strip()
    value = request.POST.get("value", "")
    try:
        member = member_svc.set_field(member_id, field, value,
                                      actor_username=request.user.username)
    except member_svc.Refused as refusal:
        return JsonResponse({"ok": False, "error": str(refusal)}, status=400)
    except Exception:
        logger.exception("user board patch failed (member=%s field=%s)",
                         member_id, field)
        return JsonResponse(
            {"ok": False,
             "error": "Could not save that — the value has been left unchanged."},
            status=400)

    fresh = (member_svc.apply_filters(member_svc.board_queryset(), **_filters(request))
             .filter(pk=member.pk).first())
    if fresh is None:
        return JsonResponse({"ok": True, "matches": False, "row": None})
    rows = member_svc.board_rows(**dict(_filters(request)))
    row = next((r for r in rows if r["id"] == member.pk), None)
    return JsonResponse({"ok": True, "matches": row is not None, "row": row})


@pipeline_superuser_required
@require_POST
def user_reset_password(request):
    """Set a login password and hand it back **once**.

    The reason this is a button rather than advice: Django admin's user change
    form cannot do it. Its password field is a read-only hash with a separate
    page behind a button, so editing a user there and pressing Save reports
    success and changes no password — which is exactly how a superuser account
    spent a week with a password nobody knew.
    """
    member_id = request.POST.get("member_id")
    try:
        password = member_svc.reset_password(
            member_id, password=request.POST.get("password", ""),
            actor_username=request.user.username)
    except member_svc.Refused as refusal:
        return JsonResponse({"ok": False, "error": str(refusal)}, status=400)
    except Exception:
        logger.exception("password reset failed (member=%s)", member_id)
        return JsonResponse(
            {"ok": False, "error": "Could not set that password — nothing changed."},
            status=400)
    # Deliberately not logged, here or anywhere.
    return JsonResponse({"ok": True, "password": password})


def _text(request):
    """The pasted TSV. ``OGABoard.newEntry`` posts JSON, not a form — reading
    ``request.POST`` here gave an empty string and a preview of nothing."""
    try:
        return (json.loads(request.body or "{}") or {}).get("text", "")
    except (ValueError, TypeError):
        return ""


@pipeline_superuser_required
@require_POST
def user_board_check(request):
    """The preview. Writes nothing."""
    items = member_svc.plan(member_svc.parse(_text(request)))
    return JsonResponse({"ok": True, "items": items,
                         "summary": member_svc.summarize(items)})


@pipeline_superuser_required
@require_POST
def user_board_commit(request):
    try:
        result = member_svc.grant(member_svc.parse(_text(request)),
                                  actor_username=request.user.username)
    except member_svc.Refused as refusal:
        return JsonResponse({"ok": False, "error": str(refusal)}, status=400)
    except Exception:
        logger.exception("granting pipeline access failed")
        return JsonResponse(
            {"ok": False, "error": "Could not create those people — nothing was saved."},
            status=400)
    return JsonResponse({"ok": True, "result": result})
