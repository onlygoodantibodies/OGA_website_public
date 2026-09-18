"""The sessions board — one page to find a session and edit it.

Replaces the walk across five pages (list → detail → edit, plus a separate
planner and a separate download/upload page) with the shape that already works
for targets: filter, click a cell, type, done.

Sessions are two-level, so the board is too. A row is one session and its header
fields edit in place. Opening a row fetches that session's results — the columns
differ per procedure, which is exactly why they are not all in one flat grid.

Logic lives in ``services/session_board.py``; these are thin.
"""
from __future__ import annotations

import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import (CellLine, ExperimentSession, Member)
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import members as member_svc
from pipeline.services import session_board as board
from pipeline.services import session_io as sio
from pipeline.services import board_page
from pipeline.services import next_step
from pipeline.services import sites as site_svc
from pipeline.views.imports import columns_and_example

logger = logging.getLogger(__name__)

DB = "pipeline_db"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_FILTER_KEYS = ("q", "gene", "procedure", "site", "experimenter", "status",
                "date_from", "date_to", "show_cancelled")


def _filters(request) -> dict:
    """The board's filters, with ``?site=`` spelled the way the form spells it.

    See ``services/sites.py::form_value``: the query is rebuilt from the filter
    form on every rows fetch, so a site named rather than numbered was dropped
    the moment the grid loaded.
    """
    f = {k: (request.GET.get(k) or "").strip() for k in _FILTER_KEYS}
    if "site" in f:
        f["site"] = site_svc.form_value(f["site"])
    return f


# Board column → the tip already written for the same column in the workbook, so
# the hover text on the page and the note in the downloaded sheet cannot drift.
# Same trick as the target board; a test asserts every key still resolves.
_HEADER_TIP_KEYS = {
    "gene": "gene", "application": "procedure", "date": "session_date",
    "experimenter": "session_experimenter", "site": "session_site",
    "status": "session_status", "cell_lines": "cell_lines",
    "results": "results", "comments": "session_comments",
}


@pipeline_member_required
@require_GET
def session_board(request):
    opts = board.filter_options()
    new_columns, new_example = columns_and_example("sessions")
    # The two cell-line boxes on Plan a session are free text on a panel whose
    # rule is that anything new is created with the session, so a typo mints a
    # near-duplicate line rather than failing. These are the site's own lines,
    # spelled the way the parser reads them back — see
    # `services/cell_lines.py::picker_options`.
    site_id = getattr(member_svc.for_request(request), "site_id", None)
    return render(request, "pipeline/session_board.html", {
        # One gene's progress and its next step, when the board is
        # filtered to one gene — services/next_step.py, derived from
        # records rather than from any stored status.
        **next_step.context(_filters(request).get("gene", "")),
        "filters": _filters(request),
        "sites": opts["sites"],
        # The pks the site <select> carries, so a ?site= that names no
        # site on file can be rendered as the selected option rather than
        # dropped — see templates/pipeline/_unknown_site_option.html.
        "site_pks": [str(s.pk) for s in opts["sites"]],
        "experimenters": opts["experimenters"],
        "procedures": opts["procedures"],
        "statuses": opts["statuses"],
        "tips": {col: sio.COLUMN_TIPS.get(key, "")
                 for col, key in _HEADER_TIP_KEYS.items()},
        # The new-entry table lists the antibodies to test. Columns come from
        # bulk_sessions.SESSION_COLUMNS, the same constant behind its Excel
        # template and its paste parser.
        "new_columns": new_columns,
        "new_example": new_example,
        # What each cell may hold — a `<select>` where the writer refuses
        # anything else, a `<datalist>` where an unlisted value is legitimate.
        # The cell-line boxes take the member's site, because which lines a
        # slot may offer is a question about whose bench is asking
        # (`services/cell_lines.py`), and answering it wrongly is how a
        # session gets controlled against another site's line.
        "cell_choices": board.cell_choices(site_id=site_id),
        "wt_options": cell_line_svc.picker_options(genotype="WT", site_id=site_id),
        "ko_options": cell_line_svc.picker_options(genotype="KO", site_id=site_id),
    })


@pipeline_member_required
@require_GET
def session_board_rows(request):
    page, per_page = board_page.read_params(request)
    data = board.board_page(page=page, per_page=per_page,
                            locate=board_page.locate_param(request),
                            **_filters(request))
    return JsonResponse({"ok": True, **data})


@pipeline_member_required
@require_GET
def session_board_results(request):
    """One session's results, its conditions, and the protocol it followed.

    All three used to be on ``session_detail``, which is why the first field test
    recorded every result there. The panel carries them now, so the page has
    nothing left that only it can do.
    """
    session = (ExperimentSession.objects.using(DB)
               .select_related("target", "protocol_template")
               .filter(pk=request.GET.get("session_id")).first())
    if session is None:
        return JsonResponse({"ok": False, "error": "unknown session"}, status=404)

    from django.urls import reverse
    stored = session.session_conditions or {}
    return JsonResponse({
        "ok": True,
        "session_id": session.pk,
        **board.results_for(session),
        # Conditions: the registry says which exist for this procedure, the
        # session says what was recorded. Keys the registry does not know about
        # — arbitrary columns from a per-gene Excel template — are listed too,
        # read-only, so they are visible rather than silently carried.
        "conditions": [dict(f, value=stored.get(f["key"], ""))
                       for f in board.condition_fields(session.procedure_type)],
        "extra_conditions": [
            {"key": k, "value": v} for k, v in sorted(stored.items())
            if k not in board.condition_field_names(session.procedure_type)],
        "protocol": board.protocol_guidance(session),
        "bench_sheet_url": reverse("pipeline:session_download",
                                   args=[session.pk, "bench-sheet"]),
        # This session's own workbook — every column, its results pre-filled,
        # its number baked into `session_ref` so the upload fills it in.
        "workbook_url": reverse("pipeline:session_download",
                                args=[session.pk, "workbook"]),
        "workbook_upload_url": reverse("pipeline:session_template_upload_preview"),
        "workbook_commit_url": reverse("pipeline:session_template_upload_commit"),
        "results_upload_url": reverse("pipeline:session_results_upload",
                                      args=[session.pk]),
        "results_commit_url": reverse("pipeline:session_results_commit",
                                      args=[session.pk]),
        # The raw-files panel is `OGABoard.filesPanel`, shared with the gene
        # page, and it fetches its own list — so an upload redraws the files and
        # nothing else, a file added changing no result row. Its three routes
        # take no arguments, so the page holds them once rather than repeating
        # them in every session's payload.
    })


def _set_session_field(session, field, value):
    """Apply one edit to a session header field. Raises ValueError on bad input."""
    value = (value or "").strip()

    if field == "date":
        if not value:
            raise ValueError("a session needs a date")
        session.date = value
    elif field == "status":
        valid = {v for v, _ in ExperimentSession.SessionStatus.choices}
        if value not in valid:
            raise ValueError(f"'{value}' is not a status")
        session.status = value
    elif field == "fc_sub_protocol":
        valid = {v for v, _ in ExperimentSession.FcSubProtocol.choices}
        if value not in valid:
            raise ValueError(f"'{value}' is not an FC sub-protocol")
        session.fc_sub_protocol = value
    elif field == "protein_loading_ug":
        # A blank clears the number; anything non-numeric is a typo, not a value.
        if not value:
            session.protein_loading_ug = None
        else:
            try:
                session.protein_loading_ug = float(value)
            except ValueError:
                raise ValueError(f"'{value}' is not a number")
    elif field == "comments":
        session.comments = value
    elif field == "site":
        # `services/sites.py` writes the refusal, so all four boards name the
        # sites on file rather than only the one you got wrong.
        session.site_id = site_svc.strict_id(value)
    elif field == "experimenter":
        member = (Member.objects.using(DB).filter(display_name__iexact=value).first()
                  if value else None)
        if value and member is None:
            raise ValueError(f"no member called '{value}'")
        # Cross-DB FK: assign the id, never the object (CLAUDE.md).
        session.experimenter_id = member.pk if member else None
    elif field in ("cell_line_wt", "cell_line_ko"):
        # `services/cell_lines.py` writes both the match and the refusal, for the
        # same reason `services/sites.py` does above. This cell rendered
        # `SH-SY5Y — Leicester` and then refused that exact string, while the
        # bare `SH-SY5Y` it would accept re-resolved to whichever of five rows
        # sorted first — so a wild type set to another institution's knockout
        # could not be corrected here at all: one spelling errored, the other
        # silently changed nothing.
        want = "WT" if field == "cell_line_wt" else "KO"
        line, err = cell_line_svc.resolve(value, genotype=want,
                                          site_id=session.site_id)
        if value and line is None:
            raise ValueError(err)
        setattr(session, f"{field}_id", line.pk if line else None)
    elif field.startswith(board.CONDITION_PREFIX):
        # One protocol condition — antibody dilution, exposure, gel percentage.
        # They live in the session_conditions JSON rather than as columns,
        # because which ones exist depends on the procedure.
        #
        # Only the named key is touched, and a cleared one is deleted rather than
        # stored as "". Sessions created from the per-gene Excel template carry
        # arbitrary spreadsheet columns in that dict which no form knows about,
        # and rebuilding it from scratch is how the full-page save used to wipe
        # them on every write.
        key = field[len(board.CONDITION_PREFIX):]
        if key not in board.condition_field_names(session.procedure_type):
            raise LookupError(field)
        conditions = dict(session.session_conditions or {})
        if value.strip():
            conditions[key] = value.strip()
        else:
            conditions.pop(key, None)
        session.session_conditions = conditions
    else:
        raise LookupError(field)


@pipeline_member_required
@require_POST
def session_board_patch(request):
    """Save one cell — a session header field, or one field of one result row.

    Returns the recomputed session row so the grid stays truthful (the result
    count moves when a result is edited), and ``matches`` so a row an edit has
    pushed out of the active filter leaves the screen.
    """
    session_id = request.POST.get("session_id")
    result_id = request.POST.get("result_id") or None
    field = (request.POST.get("field") or "").strip()
    value = request.POST.get("value", "")

    session = ExperimentSession.objects.using(DB).filter(pk=session_id).first()
    if session is None:
        return JsonResponse({"ok": False, "error": "unknown session"}, status=404)

    try:
        if result_id:
            model = board.RESULT_MODELS.get(session.procedure_type)
            if model is None or field not in board.result_field_names(session.procedure_type):
                return JsonResponse(
                    {"ok": False, "error": f"'{field}' is not editable"}, status=400)
            row = model.objects.using(DB).filter(pk=result_id, session_id=session.pk).first()
            if row is None:
                return JsonResponse({"ok": False, "error": "unknown result row"},
                                    status=404)
            model_field = model._meta.get_field(field)
            if model_field.get_internal_type() == "DecimalField":
                setattr(row, field, value.strip() or None)
            else:
                setattr(row, field, value.strip())
            row.save(using=DB)
        elif (field in board.EDITABLE_SESSION_FIELDS
                or field.startswith(board.CONDITION_PREFIX)):
            # A protocol condition arrives as `cond:<key>`. It is not in
            # EDITABLE_SESSION_FIELDS because which conditions exist depends on
            # the procedure — _set_session_field checks the key against that
            # procedure's registry and raises LookupError on anything else.
            _set_session_field(session, field, value)
            session.save(using=DB)
        else:
            return JsonResponse({"ok": False, "error": f"'{field}' is not editable"},
                                status=400)
    except LookupError:
        return JsonResponse({"ok": False, "error": f"'{field}' is not editable"},
                            status=400)
    except ValueError as e:
        # A message worth showing: the value was wrong, and it says how.
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        logger.exception("session board patch failed (session=%s field=%s)",
                         session_id, field)
        return JsonResponse(
            {"ok": False,
             "error": "Could not save that — the value has been left unchanged."},
            status=400)

    fresh = (board.apply_filters(board.board_queryset(), **_filters(request))
             .filter(pk=session.pk).first())
    if fresh is None:
        return JsonResponse({"ok": True, "matches": False, "row": None})
    counts = board.result_counts([fresh.pk])
    readings = board.reading_counts([fresh.pk])
    return JsonResponse({"ok": True, "matches": True,
                         "row": board.row_for(fresh, counts, readings)})


# ---------------------------------------------------------------------------
# File round-trip — the same filters as the grid, so a download matches
# what is on screen
# ---------------------------------------------------------------------------

@pipeline_member_required
@require_GET
def session_board_export(request):
    data = sio.build_export(**_filters(request))
    resp = HttpResponse(data, content_type=_XLSX)
    resp["Content-Disposition"] = 'attachment; filename="oga_sessions.xlsx"'
    return resp


@pipeline_member_required
@require_POST
def session_board_upload_preview(request):
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    parsed = sio.parse_upload(f)
    if not parsed.get("ok"):
        return JsonResponse(parsed, status=400)
    return JsonResponse(sio.plan(parsed))


@pipeline_member_required
@require_POST
def session_board_upload_commit(request):
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    parsed = sio.parse_upload(f)
    if not parsed.get("ok"):
        return JsonResponse(parsed, status=400)
    result = sio.apply(
        parsed,
        apply_overwrites=request.POST.get("apply_overwrites") in ("true", "1", "on"))
    return JsonResponse(result)
