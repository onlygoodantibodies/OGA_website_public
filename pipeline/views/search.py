"""Antibody and cell-line downloads.

What is left of what was once four pages. The search page, the two detail pages
and the two edit forms were all replaced by the boards and are gone; the last
thing keeping the edit forms alive was that they were the only place identity
could be changed, and that is the boards' identity dialog now
(``services/identity.py``).

These two exports remain, and they take the boards' own filters so that
"Download" means the rows on the screen above it.
"""

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from pipeline.decorators import pipeline_member_required
from django.core.paginator import Paginator
from django.db.models import Q, Prefetch, Count

from pipeline.models import (
    Antibody, CellLine, CellLineVial, Company, Member, Site, Target,
    InventoryLocation, WbResult, IpResult, IfResult, FcResult,
    ExperimentSession,
)
from pipeline import rrid_utils

DB = 'pipeline_db'

# Download columns, matching what the importers expect, so a downloaded sheet
# can be edited and uploaded straight back. They used to sit further down the
# module between the functions that used them; they live with the other
# module-level constants now, where deleting a neighbouring view cannot take
# them with it.
#
# `site` is in both because without it the round trip re-homes rows. A row is one
# vial, and site is half of what makes it that vial, so a sheet that does not say
# whose vial it is gets stamped with whoever uploads it: McGill's 22 SOD1
# antibodies, downloaded and put back by a Leicester user, previewed as 22 new
# Leicester rows. Both importers read this column (`services/sites.py::for_row`)
# and fall back to the uploader only when the cell is blank.
#
# The columns themselves live in ``services/board_columns.py`` — one ordered
# registry per entity, read by the board's own <thead> as well as by this sheet,
# because the owner's review found the two disagreeing about both which fields
# there are and what order they come in.
def _write_sheet(ws, kind, rows):
    """The sheet, in board order, with read-only columns marked.

    A column an upload cannot write back is kept and *named* rather than
    dropped — the OGA recommendation is a verdict the public pages and the MCP
    server read, and a stale spreadsheet must not re-assert it. Its header says
    so and is filled grey rather than blue, so the two kinds of column are
    distinguishable on the sheet without reading the text.
    """
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from pipeline.services import board_columns

    cols = board_columns.registry(kind)
    ws.append([c.heading for c in cols])
    for cell, col in zip(ws[1], cols):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid",
                               fgColor="6B7A8C" if col.upload == "read" else "064C83")
    for obj in rows:
        ws.append(board_columns.row_values(kind, obj))
    ws.freeze_panes = "A2"
    for i in range(1, len(cols) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 16



# =============================================================================
# Antibody Search
# =============================================================================

@pipeline_member_required
def antibody_export(request):
    """Download antibodies as an .xlsx whose columns match the importer — for a
    single target (?target=<pk>) or the current search filters — so it can be
    edited in Excel and re-uploaded via Add antibodies (tick 'update existing
    values' there to apply edits, not just fill blanks). The 'ab #' column is a
    reference only; the importer ignores it."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from django.http import HttpResponse

    from pipeline.services import antibody_board as abboard
    from pipeline.views.antibody_board import _filters as _board_filters

    # `locations` prefetched because the sheet now carries the freezer and the
    # box — without it `board_columns._storage_types` is one query per exported
    # row, and this export is routinely the whole board. Same reason the
    # cell-line export below prefetches `vials`.
    qs = (Antibody.objects.using(DB)
          .select_related('target', 'company', 'site')
          .prefetch_related('locations'))
    fname = "antibodies"

    target_id = request.GET.get('target')
    if target_id:
        qs = qs.filter(target_id=target_id)
        t = Target.objects.using(DB).filter(pk=target_id).first()
        if t:
            fname = f"{t.gene_name or t.protein_name or 'target'}_antibodies"
    else:
        # The board's own filters, not a second set that looks similar. This
        # download used to reimplement the retired search page's filters, so
        # "Download" on a board filtered to one gene handed you the whole
        # dataset — and the file you edited was not the rows you were looking at.
        filters = _board_filters(request)
        qs = abboard.apply_filters(qs, **filters)
        gene = filters.get("gene", "")
        if gene:
            fname = f"{gene}_antibodies"

    qs = qs.order_by('target__gene_name', 'company__name', 'catalogue_number')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Antibodies"
    _write_sheet(ws, "antibodies", qs)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    safe = fname.replace('/', '_').replace(' ', '_')
    resp["Content-Disposition"] = f'attachment; filename="{safe}.xlsx"'
    return resp


# =============================================================================
# Antibody Detail
# =============================================================================

@pipeline_member_required
def cell_line_export(request):
    """Download cell lines as an .xlsx whose columns match the importer — for a
    single target (?target=<pk>) or the current filters — so they can be edited in
    Excel and re-uploaded via Add cell lines (tick 'update existing values' to
    apply edits).

    Freeze-down batches are in the sheet **read-only**: they are added one press
    at a time from the board's own `+ batch`, and their numbers come from
    `services/lab_numbers.py`, so the file carries what is written on the tubes
    without inviting anybody to type a number into a spreadsheet. Storage
    locations are still managed per line."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from django.http import HttpResponse

    # `vials` prefetched because the sheet now carries the freeze-down batches:
    # without it `board_columns._batches` is one query per exported row.
    qs = (CellLine.objects.using(DB)
          .select_related('target', 'company', 'parent_line', 'site')
          .prefetch_related('vials'))
    fname = "cell_lines"

    target_id = request.GET.get('target')
    if target_id:
        qs = qs.filter(target_id=target_id)
        t = Target.objects.using(DB).filter(pk=target_id).first()
        if t:
            fname = f"{t.gene_name or t.protein_name or 'target'}_cell_lines"
    else:
        # The board's own filters — see the note in antibody_export. A download
        # that does not match the grid above it is a file you edit and upload
        # believing it was the rows you had chosen.
        from pipeline.services import cell_line_board as clboard
        from pipeline.views.cell_line_board import _filters as _board_filters
        filters = _board_filters(request)
        qs = clboard.apply_filters(qs, **filters)
        gene = filters.get("gene", "")
        if gene:
            fname = f"{gene}_cell_lines"

    qs = qs.order_by('target__gene_name', 'genotype', 'name')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cell lines"
    _write_sheet(ws, "cell-lines", qs)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    safe = fname.replace('/', '_').replace(' ', '_')
    resp["Content-Disposition"] = f'attachment; filename="{safe}.xlsx"'
    return resp
# =============================================================================
# Cell Line Detail — ADD TO END OF search.py
# =============================================================================
