"""The antibodies board — find an antibody and edit it in place.

Replaces the four-way split (search page, read-only detail, separate edit form,
bulk paste box) with the shape the target and session boards already use.

Logic lives in ``services/antibody_board.py``; these are thin.
"""
from __future__ import annotations

import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Antibody
from pipeline.services import antibody_board as board
from pipeline.services import concentration as concentration_svc
from pipeline.services import identity
from pipeline.services import lab_numbers
from pipeline.services import board_columns
from pipeline.services import board_page
from pipeline.services import next_step
from pipeline.services import sites as site_svc
from pipeline.views.imports import columns_and_example

logger = logging.getLogger(__name__)

DB = "pipeline_db"

_FILTER_KEYS = ("q", "company", "site", "gene", "recommended", "clonality",
                "application", "out_of_market")

_TRUE = {"1", "true", "yes", "on"}


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


@pipeline_member_required
@require_GET
def antibody_board(request):
    opts = board.filter_options()
    new_columns, new_example = columns_and_example("antibodies")
    return render(request, "pipeline/antibody_board.html", {
        # One gene's progress and its next step, when the board is
        # filtered to one gene — services/next_step.py, derived from
        # records rather than from any stored status.
        **next_step.context(_filters(request).get("gene", "")),
        "filters": _filters(request),
        "companies": opts["companies"],
        "sites": opts["sites"],
        # The pks the site <select> carries, so a ?site= that names no
        # site on file can be rendered as the selected option rather than
        # dropped — see templates/pipeline/_unknown_site_option.html.
        "site_pks": [str(s.pk) for s in opts["sites"]],
        "clonalities": opts["clonalities"],
        "applications": opts["applications"],
        "tips": board.COLUMN_TIPS,
        # Headings, order and hover text from the one registry the
        # .xlsx download reads — services/board_columns.py.
        "columns": [{"key": c.key, "th": c.th, "unit": c.unit,
                     "tip": board.COLUMN_TIPS.get(c.key, "")}
                    for c in board_columns.board_columns("antibodies")],
        # The new-entry table's headings, from the same constant that builds the
        # Excel template — so the table, the template and the parser agree.
        "new_columns": new_columns,
        "new_example": new_example,
    })


@pipeline_member_required
@require_GET
def antibody_board_rows(request):
    page, per_page = board_page.read_params(request)
    data = board.board_page(page=page, per_page=per_page,
                            locate=board_page.locate_param(request),
                            **_filters(request))
    return JsonResponse({"ok": True, **data})


@pipeline_member_required
@require_POST
def antibody_board_patch(request):
    """Save one cell.

    Identity — target, company, catalogue number — is deliberately not editable:
    it is the antibody's key, and retyping one part of it silently turns the row
    into a different antibody. RRID is not editable either; it is written
    through ``rrid_utils`` so the bare AB_<n> and the registry link stay in step.
    """
    antibody_id = request.POST.get("target_id") or request.POST.get("antibody_id")
    field = (request.POST.get("field") or "").strip()
    value = request.POST.get("value", "")

    antibody = Antibody.objects.using(DB).filter(pk=antibody_id).first()
    if antibody is None:
        return JsonResponse({"ok": False, "error": "unknown antibody"}, status=404)

    try:
        if field in board.BOOLEAN_FIELDS:
            setattr(antibody, field, value.strip().lower() in _TRUE)
        elif field == "site":
            # A refusal that names no alternative leaves you guessing at spellings
            # of your own institution. `services/sites.py` writes the message, so
            # all four boards refuse an unknown site the same way and all four
            # list the ones on file.
            try:
                antibody.site_id = site_svc.strict_id(value)
            except site_svc.UnknownSite as exc:
                return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        elif field == "concentration":
            # The column has no unit of its own — the number means µg/mL. So a
            # unit typed into the cell has to be converted, not discarded and
            # not refused as "not a number", which is what a supplier's own
            # "1.0 mg/mL" got. One reader for every write path.
            raw = value.strip()
            if not raw:
                antibody.concentration = None
            else:
                parsed, err = concentration_svc.parse(raw)
                if err:
                    return JsonResponse({"ok": False, "error": err}, status=400)
                antibody.concentration = parsed
        elif field in board.NUMERIC_FIELDS & board.EDITABLE_FIELDS:
            # The lab's own A-number. Emptying the cell means "not written
            # down", so it clears the field rather than failing on int("");
            # anything else goes through `services/lab_numbers.py`, the same
            # reader the Add grid uses, so the grid and the paste box cannot
            # disagree about what an A-number is the way they once did about
            # C-numbers.
            raw = value.strip()
            if not raw:
                setattr(antibody, field, None)
            else:
                number, err = lab_numbers.parse(raw, kind=lab_numbers.ANTIBODY)
                if err:
                    return JsonResponse({"ok": False, "error": err}, status=400)
                # Two antibodies with one number at one site cannot be told
                # apart on a freezer box, which is the whole job of the number.
                # Checked before saving, not caught afterwards: on PostgreSQL a
                # failed statement poisons the transaction, so the handler that
                # wants to name the other record could not run the query it
                # needs.
                msg = lab_numbers.clash(
                    lab_numbers.ANTIBODY, antibody.site_id, number,
                    exclude_pk=antibody.pk,
                    site_name=antibody.site.name if antibody.site_id else "")
                if msg:
                    return JsonResponse({"ok": False, "error": msg}, status=400)
                setattr(antibody, field, number)
        elif field in board.EDITABLE_FIELDS:
            setattr(antibody, field, value.strip())
        else:
            return JsonResponse({"ok": False, "error": f"'{field}' is not editable"},
                                status=400)

        # Lot and site are the two parts of the identity key a cell can move, so
        # a cell edit can push this row onto another one. Say which row, in the
        # words the identity dialog uses — the bare `except` below turned that
        # collision into "Could not save that", which names nothing.
        if field in ("lot_number", "site"):
            clash = identity.find_vial_clash(antibody)
            if clash is not None:
                return JsonResponse(
                    {"ok": False,
                     "error": identity.vial_clash_message(
                         clash, antibody.catalogue_number,
                         identity.company_label(
                             antibody.company if antibody.company_id else None),
                         antibody.target.gene_name if antibody.target_id else "(no gene)")},
                    status=400)

        antibody.save(using=DB)
    except Exception:
        logger.exception("antibody board patch failed (antibody=%s field=%s)",
                         antibody_id, field)
        return JsonResponse(
            {"ok": False,
             "error": "Could not save that — the value has been left unchanged."},
            status=400)

    fresh = (board.apply_filters(board.board_queryset(), **_filters(request))
             .filter(pk=antibody.pk).first())
    if fresh is None:
        return JsonResponse({"ok": True, "matches": False, "row": None})
    return JsonResponse({"ok": True, "matches": True, "row": board.row_for(fresh)})


# ---------------------------------------------------------------------------
# Identity — the deliberate change, not the inline one
# ---------------------------------------------------------------------------
#
# The board refuses catalogue, supplier and gene in a cell, because retyping one
# in a grid turns the row into a different antibody while every result recorded
# against it stays attached. That refusal only works if there is somewhere to do
# it properly, and until now that was the antibody edit page — a legacy page kept
# alive for this one job. This is what replaces it.

@pipeline_member_required
@require_GET
def antibody_identity(request):
    """What this antibody is, and what would follow it if that changed."""
    ab = (Antibody.objects.using(DB)
          .select_related("target", "company", "site")
          .filter(pk=request.GET.get("antibody_id")).first())
    if ab is None:
        return JsonResponse({"ok": False, "error": "unknown antibody"}, status=404)
    return JsonResponse({"ok": True, "identity": identity.antibody_identity(ab)})


@pipeline_member_required
@require_POST
def antibody_identity_save(request):
    ab = (Antibody.objects.using(DB).select_related("target", "company", "site")
          .filter(pk=request.POST.get("antibody_id")).first())
    if ab is None:
        return JsonResponse({"ok": False, "error": "unknown antibody"}, status=404)
    try:
        identity.change_antibody_identity(ab, request.POST)
    except identity.Refused as refusal:
        # A refusal is a sentence the page can show, not a stack trace.
        return JsonResponse({"ok": False, "error": str(refusal)}, status=400)
    except Exception:
        logger.exception("antibody identity change failed (ab=%s)", ab.pk)
        return JsonResponse(
            {"ok": False,
             "error": "Could not save that — nothing has been changed."},
            status=400)

    fresh = (board.apply_filters(board.board_queryset(), **_filters(request))
             .filter(pk=ab.pk).first())
    if fresh is None:
        return JsonResponse({"ok": True, "matches": False, "row": None})
    return JsonResponse({"ok": True, "matches": True, "row": board.row_for(fresh)})
