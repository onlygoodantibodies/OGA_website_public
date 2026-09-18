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
from pipeline.services import members as member_svc
from pipeline.services import board_columns
from pipeline.services import board_page
from pipeline.services import next_step
from pipeline.services import received as received_svc
from pipeline.services import renumber as renumber_svc
from pipeline.services import sites as site_svc
from pipeline.services import storage as storage_svc
from pipeline.views.imports import columns_and_example

logger = logging.getLogger(__name__)

DB = "pipeline_db"

_FILTER_KEYS = ("q", "company", "site", "gene", "recommended", "clonality",
                "application", "out_of_market", "numbered")

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
    member, _is_superuser = _asker(request)
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
        # What each cell may hold — a `<select>` for a closed set, a `<datalist>`
        # for a convention. Every cell on this board was a bare text box,
        # including three the server then refused. See `board.cell_choices`.
        "cell_choices": board.cell_choices(),
        # **A number in a caveat with nothing to click is half a message.**
        # Logging no longer mints an A-number, so "the ones I have not numbered
        # yet" is a real and growing set — and a bench that cannot find it will
        # discover the gap on a bench sheet with a blank `ab #` column. The
        # count links to the filter that shows them.
        "unnumbered": board.unnumbered_count(member),
    })


@pipeline_member_required
@require_GET
def antibody_board_rows(request):
    page, per_page = board_page.read_params(request)
    data = board.board_page(page=page, per_page=per_page,
                            locate=board_page.locate_param(request),
                            **_filters(request))
    return JsonResponse({"ok": True, **data})


def _renumber_ids(request):
    """The antibodies to renumber, in the order the board is showing them.

    **The whole filtered set, not the drawn page** — the same rule a download
    follows. `page` is board state, and a renumbering that silently covered
    fifty of ninety rows would leave the rest holding numbers from the old
    order, which is the one outcome worse than not doing it at all.

    The order is the board's own (`_ordered`): gene, then supplier, then
    catalogue. That is what makes "number them as shown" mean "group each
    protein's antibodies together", which is the thing that was asked for.
    """
    return list(board._ordered(**_filters(request))
                .values_list("pk", flat=True))


def _asker(request):
    """Who is asking — their `Member` row and whether they are a superuser.

    `members.for_request` rather than a sixth private copy of the cross-database
    username join: five views have written it out for themselves, and its own
    docstring asks new callers to use it.
    """
    return (member_svc.for_request(request),
            bool(getattr(request.user, "is_superuser", False)))


def _renumber_start(request):
    raw = (request.POST.get("start") or "").strip()
    if not raw:
        return None, ""
    number, err = lab_numbers.parse(raw, kind=lab_numbers.ANTIBODY,
                                    field="first number")
    return number, err


@pipeline_member_required
@require_POST
def antibody_renumber_plan(request):
    """What renumbering the filtered set would do. Writes nothing."""
    start, err = _renumber_start(request)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)
    member, is_superuser = _asker(request)
    return JsonResponse({"ok": True,
                         **renumber_svc.plan(_renumber_ids(request), start=start,
                                             member=member,
                                             is_superuser=is_superuser)})


@pipeline_member_required
@require_POST
def antibody_renumber_apply(request):
    """Write the mapping the preview showed, in one transaction."""
    start, err = _renumber_start(request)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)
    try:
        consented = int(request.POST.get("consented_count") or -1)
    except ValueError:
        consented = -1
    try:
        member, is_superuser = _asker(request)
        result = renumber_svc.apply(
            _renumber_ids(request), start=start, consented_count=consented,
            # What the preview actually showed, not only how many rows it
            # showed — see `renumber._stamp`.
            stamp=(request.POST.get("stamp") or "").strip(),
            member=member, is_superuser=is_superuser)
    except renumber_svc.Refused as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        logger.exception("antibody renumbering failed")
        return JsonResponse(
            {"ok": False,
             "error": "Could not renumber those — nothing has been changed."},
            status=400)
    return JsonResponse({"ok": True, **result})


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
        elif field == "clonality":
            # A closed set, and a dropdown is only half of enforcing one — the
            # picker narrows what a person can send, the writer decides what is
            # stored. This cell fell through to the bare `setattr` below, so
            # `mono` typed into it was saved verbatim and `clonality.label`
            # then had a value its own vocabulary does not contain. The paste
            # path has always checked (`commit._apply_metadata`); this was the
            # one door that did not.
            raw = value.strip().lower()
            allowed = dict(Antibody.Clonality.choices)
            if raw and raw not in allowed:
                return JsonResponse(
                    {"ok": False,
                     "error": (f"'{value.strip()}' is not a clonality — it is one "
                               f"of {', '.join(allowed)}. Whether it is a "
                               f"recombinant is the separate tick beside it, "
                               f"because the two are separate columns.")},
                    status=400)
            antibody.clonality = raw or Antibody.Clonality.UNKNOWN
        elif field == "acquisition_method":
            # The other closed set on this board, refused the same way and for
            # the same reason: the picker narrows what a person can send, the
            # writer decides what is stored.
            raw = value.strip().lower().replace(" ", "_").replace("-", "_")
            allowed = dict(Antibody.AcquisitionMethod.choices)
            if raw and raw not in allowed:
                return JsonResponse(
                    {"ok": False,
                     "error": (f"'{value.strip()}' is not an acquisition method "
                               f"— it is one of {', '.join(allowed)}. "
                               f"'in_kind' means the supplier contributed it; "
                               f"'purchased' means the lab bought it.")},
                    status=400)
            antibody.acquisition_method = raw or Antibody.AcquisitionMethod.UNKNOWN
        elif field in board.LOCATION_FIELDS:
            # A freezer and a box are not columns on the antibody — they are an
            # `InventoryLocation` row, and `services/storage.py` is the one
            # place that writes one. It creates the row on the first value,
            # deletes it when the last one is cleared, and refuses by name when
            # the vial is recorded in two places or has no site to hang a
            # freezer off. It writes, so there is nothing for `antibody.save()`
            # below to do — but the save is harmless and keeps one exit path.
            writer = (storage_svc.set_type if field == "storage"
                      else storage_svc.set_field)
            _loc, err = (writer(antibody, value)
                         if field == "storage"
                         else writer(antibody, field, value))
            if err:
                return JsonResponse({"ok": False, "error": err}, status=400)
        elif field in board.DATE_FIELDS:
            # As much of the date as anybody knows, and no more. `2026-08` is
            # August and stays August; a slashed date whose two numbers could
            # each be the month is refused rather than guessed, because a wrong
            # arrival date is the quiet kind of wrong — nothing on any screen
            # contradicts it and it turns up in a methods section years later.
            raw = value.strip()
            if not raw:
                antibody.received_date = None
                antibody.received_precision = received_svc.DAY
            else:
                when, precision, err = received_svc.parse(raw)
                if err:
                    return JsonResponse({"ok": False, "error": err}, status=400)
                antibody.received_date = when
                antibody.received_precision = precision
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
