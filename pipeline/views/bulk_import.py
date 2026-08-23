"""
Bulk antibody paste tool — paste a list of antibodies (e.g. catalogue + product
URLs looked up in a chat) and add/update them all at once, instead of editing
records one by one.

Page + two JSON endpoints (parse preview, commit). Login-gated. Writes via
pipeline.services.bulk_antibodies, which reuses the cropper's dedup-safe engine.
"""
import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member
from pipeline.services import bulk_antibodies as bulk
from pipeline.services import bulk_cell_lines as bulkcl

DB = "pipeline_db"

logger = logging.getLogger(__name__)


def _member(request):
    return (Member.objects.using(DB)
            .filter(user__username=request.user.username).first())


def _refusal(what, exc):
    """A failed paste is a refusal in JSON, never an unhandled 500.

    An HTML error page is unparseable to the caller, so the board could only say
    "the server returned 500" — which it then paired with "nothing was saved",
    an assertion it had no way to check. Both halves apply in one place now: the
    detail goes to the log, and the page gets a sentence a scientist can act on.
    """
    logger.exception("bulk %s failed", what)
    return JsonResponse(
        {"ok": False,
         "error": f"Could not save these {what}. Check the board to see what "
                  "was written before trying again — the error has been logged."},
        status=400)

@pipeline_member_required
@require_POST
def bulk_antibodies_parse(request):
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    # Re-check path: if the page sends edited rows, re-plan them instead of
    # re-parsing raw text — keeps the status badges honest after in-grid edits.
    rows = d.get("rows")
    if rows is None:
        rows = bulk.parse(d.get("text", ""), d.get("default_gene", ""))
    # Same member as apply gets: a row resolves to a vial, and site is half of
    # what makes a vial that vial, so a preview without it would disagree.
    items = bulk.plan(rows, create_targets=bool(d.get("create_targets")),
                      member=_member(request))
    return JsonResponse({"items": items, "summary": bulk.summarize(items)})


@pipeline_member_required
@require_POST
def bulk_antibodies_commit(request):
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    rows = d.get("rows")
    if rows is None:
        rows = bulk.parse(d.get("text", ""), d.get("default_gene", ""))
    create_targets = bool(d.get("create_targets"))

    try:
        if d.get("dry_run", True):
            items = bulk.plan(rows, create_targets, member=_member(request))
            return JsonResponse({"dry_run": True, "summary": bulk.summarize(items)})

        result = bulk.apply(rows, create_targets, member=_member(request),
                            overwrite=bool(d.get("overwrite")))
    except Exception as exc:
        return _refusal("antibodies", exc)
    # The board, filtered to the row just written — the antibody edit page it
    # used to point at is retired, and its one unique job (changing catalogue,
    # supplier or gene) is the board's identity dialog now.
    from django.urls import reverse
    from urllib.parse import quote
    board_url = reverse("pipeline:antibody_board")
    for rec in result["created"] + result["updated"]:
        rec["edit_url"] = f"{board_url}?q={quote(str(rec.get('catalogue') or ''))}"
    return JsonResponse({"dry_run": False, "result": result})


# =============================================================================
# Bulk cell lines — the cell-line counterpart of the antibody paste tool
# =============================================================================

@pipeline_member_required
@require_POST
def bulk_cell_lines_parse(request):
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    # Re-check path: edited rows re-plan without re-parsing (see bulk_antibodies_parse).
    rows = d.get("rows")
    if rows is None:
        rows = bulkcl.parse(d.get("text", ""), d.get("default_gene", ""),
                            d.get("default_genotype", ""))
    items = bulkcl.plan(rows, create_targets=bool(d.get("create_targets")),
                        member=_member(request))
    return JsonResponse({"items": items, "summary": bulkcl.summarize(items)})


@pipeline_member_required
@require_POST
def bulk_cell_lines_commit(request):
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    rows = d.get("rows")
    if rows is None:
        rows = bulkcl.parse(d.get("text", ""), d.get("default_gene", ""),
                            d.get("default_genotype", ""))
    create_targets = bool(d.get("create_targets"))

    try:
        if d.get("dry_run", True):
            items = bulkcl.plan(rows, create_targets, member=_member(request))
            return JsonResponse({"dry_run": True, "summary": bulkcl.summarize(items)})

        result = bulkcl.apply(rows, create_targets, member=_member(request),
                              overwrite=bool(d.get("overwrite")))
    except Exception as exc:
        return _refusal("cell lines", exc)
    from django.urls import reverse
    from urllib.parse import quote
    board_url = reverse("pipeline:cell_line_board")
    for rec in result["created"] + result["updated"]:
        rec["detail_url"] = f"{board_url}?q={quote(str(rec.get('name') or ''))}"
    return JsonResponse({"dry_run": False, "result": result})
