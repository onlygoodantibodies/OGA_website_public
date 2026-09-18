"""The cell lines board — find a line and edit it in place.

Logic lives in ``services/cell_line_board.py``; these are thin.
"""
from __future__ import annotations

import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import CellLine
from pipeline.services import c_number as c_number_svc
from pipeline.services import cell_line_board as board
from pipeline.services import identity
from pipeline.services import lab_numbers
from pipeline.services import batches
from pipeline.services import board_columns
from pipeline.services import board_page
from pipeline.services import next_step
from pipeline.services import sites as site_svc
from pipeline.views.imports import columns_and_example

logger = logging.getLogger(__name__)

DB = "pipeline_db"

_FILTER_KEYS = ("q", "genotype", "gene", "site", "ko_validated", "received")

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
def cell_line_board(request):
    opts = board.filter_options()
    new_columns, new_example = columns_and_example("cell-lines")
    return render(request, "pipeline/cell_line_board.html", {
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
        "genotypes": opts["genotypes"],
        "tips": board.COLUMN_TIPS,
        # Headings, order and hover text from the one registry the
        # .xlsx download reads — services/board_columns.py.
        "columns": [{"key": c.key, "th": c.th, "unit": c.unit,
                     "tip": board.COLUMN_TIPS.get(c.key, "")}
                    for c in board_columns.board_columns("cell-lines")],
        # Same constant that builds the Excel template, so table, template and
        # parser cannot drift apart.
        "new_columns": new_columns,
        "new_example": new_example,
        # What each cell may hold — a `<select>` where the writer refuses
        # anything else, a `<datalist>` where an unlisted value is legitimate.
        # See the board service's `cell_choices`.
        "cell_choices": board.cell_choices(),
        # The Add panel offers two sets the grid has no cell for —
        # `genotype` is identity, chosen at creation and changed only
        # through the identity dialog afterwards.
        "panel_choices": board.panel_choices(),
    })


@pipeline_member_required
@require_GET
def cell_line_board_rows(request):
    page, per_page = board_page.read_params(request)
    data = board.board_page(page=page, per_page=per_page,
                            locate=board_page.locate_param(request),
                            **_filters(request))
    return JsonResponse({"ok": True, **data})


@pipeline_member_required
@require_POST
def cell_line_board_patch(request):
    """Save one cell.

    Name, target, genotype, parent line and supplier are not editable here — see
    the module docstring in ``services/cell_line_board.py`` for why.
    """
    line_id = request.POST.get("target_id") or request.POST.get("cell_line_id")
    field = (request.POST.get("field") or "").strip()
    value = request.POST.get("value", "")

    line = CellLine.objects.using(DB).filter(pk=line_id).first()
    if line is None:
        return JsonResponse({"ok": False, "error": "unknown cell line"}, status=404)

    try:
        if field in board.BOOLEAN_FIELDS:
            setattr(line, field, value.strip().lower() in _TRUE)
        elif field == "site":
            # Refused by name, with the sites on file listed — see the same branch
            # in ``views/antibody_board.py``. This board said only "no site called
            # 'X'", which tells you it is wrong and not what is right.
            try:
                line.site_id = site_svc.strict_id(value)
            except site_svc.UnknownSite as exc:
                return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        elif field in board.NUMERIC_FIELDS & board.EDITABLE_FIELDS:
            # Emptying the cell means "not written down", so it clears the field
            # rather than failing on int(""). Anything else goes through
            # ``services/c_number.py`` — the same reader the paste box uses, so
            # the grid and the Add panel cannot disagree about what a C-number
            # is. They did: this branch refused ``C-RUN11-01`` by name while the
            # paste path quietly filed it as 11.
            raw = value.strip()
            if not raw:
                setattr(line, field, None)
            else:
                number, err = c_number_svc.parse(
                    raw, field=field.replace("_", " "))
                if err:
                    return JsonResponse({"ok": False, "error": err}, status=400)
                # Two lines with one C-number at one site cannot be told apart
                # on a tube, which is the whole job of the number — and
                # `services/cell_lines.py::by_c_number` reads both this column
                # and the freeze-down batches, so the clash is checked against
                # both. Checked before saving rather than caught after: a failed
                # statement poisons the transaction on PostgreSQL.
                msg = lab_numbers.clash(
                    lab_numbers.CELL_LINE, line.site_id, number,
                    exclude_pk=line.pk,
                    site_name=line.site.name if line.site_id else "")
                if msg:
                    return JsonResponse({"ok": False, "error": msg}, status=400)
                setattr(line, field, number)
        elif field in board.EDITABLE_FIELDS:
            setattr(line, field, value.strip())
        else:
            return JsonResponse({"ok": False, "error": f"'{field}' is not editable"},
                                status=400)
        line.save(using=DB)
    except Exception:
        logger.exception("cell line board patch failed (line=%s field=%s)",
                         line_id, field)
        return JsonResponse(
            {"ok": False,
             "error": "Could not save that — the value has been left unchanged."},
            status=400)

    fresh = (board.apply_filters(board.board_queryset(), **_filters(request))
             .filter(pk=line.pk).first())
    if fresh is None:
        return JsonResponse({"ok": True, "matches": False, "row": None})
    return JsonResponse({"ok": True, "matches": True, "row": board.row_for(fresh)})


# ---------------------------------------------------------------------------
# Identity — the deliberate change, not the inline one
# ---------------------------------------------------------------------------
#
# Name, gene, genotype and parent are what the line *is*: every session that used
# it points at this row, and the whole interpretation of those results depends on
# which line was the WT and which the KO. The board refuses them in a cell, and
# that refusal needs somewhere to send people — this, rather than the legacy edit
# page it replaces.

@pipeline_member_required
@require_GET
def cell_line_identity(request):
    line = (CellLine.objects.using(DB)
            .select_related("target", "parent_line")
            .filter(pk=request.GET.get("cell_line_id")).first())
    if line is None:
        return JsonResponse({"ok": False, "error": "unknown cell line"}, status=404)
    return JsonResponse({"ok": True, "identity": identity.cell_line_identity(line)})


@pipeline_member_required
@require_POST
def cell_line_identity_save(request):
    line = (CellLine.objects.using(DB).select_related("target", "parent_line")
            .filter(pk=request.POST.get("cell_line_id")).first())
    if line is None:
        return JsonResponse({"ok": False, "error": "unknown cell line"}, status=404)
    try:
        identity.change_cell_line_identity(line, request.POST)
    except identity.Refused as refusal:
        return JsonResponse({"ok": False, "error": str(refusal)}, status=400)
    except Exception:
        logger.exception("cell line identity change failed (line=%s)", line.pk)
        return JsonResponse(
            {"ok": False,
             "error": "Could not save that — nothing has been changed."},
            status=400)

    fresh = (board.apply_filters(board.board_queryset(), **_filters(request))
             .filter(pk=line.pk).first())
    if fresh is None:
        return JsonResponse({"ok": True, "matches": False, "row": None})
    return JsonResponse({"ok": True, "matches": True, "row": board.row_for(fresh)})


def _asker(request):
    """Who is asking — their Member row, and whether they are a superuser.

    Same shape as ``views/deletion.py`` and ``views/attachments.py``: the
    pipeline user is matched by **username** across databases, because a
    `Member` lives in `pipeline_db` and the login in `academy_db`. There is no
    `request.member`; reaching for one returns None and every add is refused
    with "your account has no site", which is what happened here.
    """
    from django.contrib.auth.models import User
    from pipeline.models import Member
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        member = Member.objects.using(DB).select_related("site").get(
            user_id=pu.pk, is_active=True)
    except Exception:
        member = None
    return member, bool(getattr(request.user, "is_superuser", False))


@require_POST
@pipeline_member_required
def cell_line_add_batch(request):
    """Add one freeze-down batch to a line, and hand back the redrawn row.

    Thin, like the rest: `services/batches.py` decides, `lab_numbers` numbers,
    and this returns the row the save produced so the board redraws that one row
    rather than refetching the grid.
    """
    line = (CellLine.objects.using(DB).select_related("site")
            .filter(pk=request.POST.get("cell_line_id")).first())
    if line is None:
        return JsonResponse({"ok": False, "error": "unknown cell line"}, status=404)
    try:
        member, is_superuser = _asker(request)
        out = batches.add(
            line, member=member, is_superuser=is_superuser,
            vial_count=(request.POST.get("vial_count") or None),
            freeze_date=(request.POST.get("freeze_date") or None),
            c_number=(request.POST.get("c_number") or None),
            notes=(request.POST.get("notes") or ""))
    except Exception:
        logger.exception("add batch failed (line=%s)", line.pk)
        return JsonResponse(
            {"ok": False,
             "error": "Could not add that batch — nothing has been changed."},
            status=400)
    if not out.get("ok"):
        return JsonResponse(out, status=400)

    line.refresh_from_db()
    fresh = (board.apply_filters(board.board_queryset(), **_filters(request))
             .filter(pk=line.pk).first())
    out["matches"] = fresh is not None
    out["row"] = board.row_for(fresh) if fresh is not None else None
    return JsonResponse(out)
