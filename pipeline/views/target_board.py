"""
Target board, nomination check and grant-writing portfolio.

The board is the replacement for Carl's master spreadsheet: the same A–T columns,
every site rather than one, editable in place, and downloadable back into Excel in
either his layout or a better one. The other two surfaces are the jobs he
described doing *with* the spreadsheet, which the spreadsheet is bad at — deciding
what to start next without duplicating another site's work, and summarising the
portfolio by protein class while writing a grant.

Logic lives in ``services/target_board.py`` (read) and
``services/target_list_io.py`` (write); these are thin.
"""
from __future__ import annotations

import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import (GrantingAgency, Member, Project, Report, Site,
                             Target, TargetClassification, TargetNomination)
from pipeline.services import board_page
from pipeline.services import doi as doi_svc
from pipeline.services import next_step
from pipeline.services import sites as site_svc
from pipeline.services import target_board as board
from pipeline.services import target_list_io as tio

logger = logging.getLogger(__name__)


def _target_columns():
    """The gene column and its example, from the one list four surfaces read."""
    from pipeline.views.imports import columns_and_example
    return columns_and_example("targets")

DB = "pipeline_db"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_FILTER_KEYS = ("q", "gene", "site", "project", "agency", "funded", "completed",
                "protein_class", "status")


def _member(request):
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        return Member.objects.using(DB).get(user_id=pu.pk, is_active=True)
    except Exception:
        return None


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


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

# Board column → the tip already written for the same column in the workbook, so
# the hover text on the page and the hover text in Excel cannot drift apart.
_HEADER_TIP_KEYS = {
    "gene": "gene", "protein": "protein", "sites": "sites_all",
    "funder": "granting_agency", "funded": "funding", "completed": "completed",
    "applications": "applications", "classes": "classes",
    "zenodo": "zenodo_doi", "f1000": "f1000", "notes": "comments",
}


@pipeline_member_required
@require_GET
def target_board(request):
    opts = board.filter_options()
    member = _member(request)
    return render(request, "pipeline/target_board.html", {
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
        # The upload dialog's "if a row has no site, treat it as" box arrived
        # hard-coded to McGill. A Leicester curator uploading their own sheet
        # without a site column nominated every row to another site, silently,
        # by accepting the default. It is your own site now — and blank rather
        # than a guess if we cannot tell whose it is, since a wrong site is worse
        # than being asked.
        "my_site": getattr(getattr(member, "site", None), "name", "") or "",
        # The Add panel's "whose list is this for" box, defaulted the same way
        # the upload dialog's is and the feasibility page's already was. A batch
        # of genes is one bench's worth, and until this existed the paste door
        # could only ever nominate to the adder's own site.
        "my_site_id": getattr(member, "site_id", None) or "",
        "projects": opts["projects"],
        "agencies": opts["agencies"],
        "classes": opts["classes"],
        "applications": board.APPLICATIONS,
        "tips": {col: tio.COLUMN_TIPS.get(key, "")
                 for col, key in _HEADER_TIP_KEYS.items()},
        # The new-entry table is one column: a gene name. Targets are created
        # by gene and enriched from UniProt, so there is nothing else to type.
        # Read from `views/imports.py` like the other three boards, so the
        # downloadable template and the grid cannot disagree about the columns.
        "new_columns": _target_columns()[0],
        "new_example": _target_columns()[1],
    })


@pipeline_member_required
@require_GET
def target_board_rows(request):
    page, per_page = board_page.read_params(request)
    data = board.board_page(page=page, per_page=per_page,
                            locate=board_page.locate_param(request),
                            **_filters(request))
    return JsonResponse({
        "ok": True,
        **data,
        # Consortium-wide, not per page: a gene two sites are both doing is
        # worth flagging wherever its row happens to be drawn, and the answer
        # would change as you paged if this were scoped to the slice.
        "duplicates": {d["gene"].upper(): d["sites"]
                       for d in board.duplicate_targets()},
    })


# Fields the board may edit in place. Identity fields are deliberately absent:
# they come from UniProt, and a disagreement is a conflict to review on the
# target page, not a cell to retype.
_TARGET_FIELDS = {"essential_gene"}
_NOMINATION_FIELDS = {"funded", "comments", "status_note", "conclusion_note",
                      "antibodies_requested", "site", "project", "granting_agency"}
_REPORT_FIELDS = {"zenodo_doi", "f1000_doi", "f1000_priority", "f1000_note",
                  "zenodo_date", "f1000_date"}


def _strict_site_id(value):
    """Resolve a typed site name, or refuse. Blank clears the allocation.

    Deliberately *not* ``target_list_io._resolve_site``, which creates a Site it
    has never seen. That is right for importing Carl's workbook, where the sheet
    is the source of truth for which sites exist; it is wrong for a cell someone
    types into, where the same behaviour turns "Leicster" into a real site that
    then appears in the site filter of all four boards forever.

    The lookup and the wording of the refusal are ``services/sites.py`` now, so
    all four boards and both importers say the same thing — this board named the
    sites on file and the other two did not, which left "no site called 'Leicster'"
    with nowhere to go next.
    """
    return site_svc.strict_id(value)


# The two columns that decide whether a gene reads as completed, and the one
# field on this board with no way back until now.
_DOI_FIELDS = {"zenodo_doi": "Zenodo DOI", "f1000_doi": "F1000 DOI"}

# The two dates, and what each one means — a refusal has to name the field, and
# "that is not a date" is the wrong sentence when the reader typed one.
_DATE_FIELDS = {"zenodo_date": "Zenodo deposit date",
                "f1000_date": "F1000 publication date"}


def _parse_report_date(value, field):
    """Read a typed date, or refuse it by name and with what it accepts.

    ``target_list_io.parse_date`` answers ``None`` for anything it cannot read,
    which is right for a spreadsheet cell — a workbook of 585 rows is full of
    things that are not dates and none of them should stop the import — and
    exactly wrong for a box somebody opened and typed into. Nothing was written,
    the cell redrew empty, and a save that stored nothing looked like one that
    worked. The same bargain ``services/doi.py`` and ``services/concentration.py``
    strike: convert what we understand, refuse the rest, say what we take.

    ``f1000_date`` is the field that decides whether a gene reads as completed
    (``target_board.completed_report_q``), so it is the last one that should fail
    quietly.
    """
    parsed, _raw = tio.parse_date(value)
    if parsed is None and (value or "").strip():
        raise ValueError(
            f"{_DATE_FIELDS.get(field, field)}: “{value.strip()}” is not a date "
            "I can read. Give it as 2026-08-06, or 2026-08 or 2026 if that is "
            "all that is known. Leave it empty to clear it.")
    return parsed


def _save_report_field(target, field, value):
    """Write one dissemination cell onto the ``Report`` row the board is showing.

    Three things here, and each of them was wrong:

    **The value is read, not stored raw.** ``zenodo_doi`` is a ``URLField``, and a
    URLField validates nothing on ``save()`` — so ``definitely not a doi 12345``
    went in, marked the gene completed, and was drawn as a link that resolved
    against our own site. ``services/doi.py`` converts what it understands and
    refuses the rest by name; the ``ValueError`` reaches the page as its own
    refusal, and nothing is written.

    **Blank clears it.** A cell edit is how a person corrects a mistake, so an
    empty box empties the column — the same bargain ``_strict_site_id`` already
    makes one column over. This is the *typed* path only: the fill-only-blank rule
    that governs ``target_list_io`` and every paste box is untouched, because a
    blank cell in a 585-row spreadsheet means "I did not fill this in", where a
    blank cell you opened, cleared and pressed Enter on means what it says.

    **Completed is derived, so the status must come back down too.** It only ever
    went up: clear the DOI and ``completed`` flipped back to open, correctly, while
    the ``Report`` row kept a green **Published** pill on the gene page — two
    answers to one question, on the page with the fewest places left to check.

    **And the row itself must go.** That fix stepped the status down and left the
    record standing, so clearing a DOI turned a green pill into **REPORTS (1) ·
    Draft — No DOI linked yet** — a deposit that never existed, on a gene where
    Generate Report had never been pressed, under a panel promising a draft never
    appears there. A row stating nothing is not a record, so a write that empties
    one removes it (``target_board.records_nothing``); a row carrying anything
    else — Carl's F1000 column, a written introduction, a generated timestamp —
    is content and stays.
    """
    reports = list(Report.objects.using(DB).filter(target_id=target.pk).order_by("pk"))
    report = (board.report_showing(reports, field)
              or (reports[0] if reports else None)
              or Report(target_id=target.pk))

    if field in _DOI_FIELDS:
        url, error = doi_svc.parse(value, field=_DOI_FIELDS[field])
        if error:
            raise ValueError(error)
        setattr(report, field, url)
    elif field.endswith("_date"):
        setattr(report, field, _parse_report_date(value, field))
    else:
        setattr(report, field, value.strip())

    published = bool(report.zenodo_doi or report.f1000_date)
    if published:
        report.status = Report.ReportStatus.PUBLISHED
    elif report.status == Report.ReportStatus.PUBLISHED:
        # Step back to the highest state the record still has evidence for.
        # `submitted` is only ever set by services/deposit.py, which writes a
        # reserved DOI at the same moment, so it cannot be reached from here.
        report.status = (Report.ReportStatus.GENERATED if report.generated_at
                         else Report.ReportStatus.DRAFT)

    if board.records_nothing(report):
        # Never saved in the first place if this write created it, so the
        # ordinary "type a DOI, spot the typo, clear it" round trip leaves the
        # database exactly as it found it.
        if report.pk:
            report.delete(using=DB)
        return

    report.save(using=DB)


@pipeline_member_required
@require_POST
def target_board_patch(request):
    """Save one cell, and hand back everything the page needs to redraw that row.

    Returning the recomputed row is what keeps derived values truthful — the
    ``completed`` pill flips as soon as a Zenodo DOI lands — without the page
    refetching the whole board after every keystroke-sized edit.

    The board's filters ride along on the query string. An edit can move a row
    out of the active filter (unfund a row while filtering ``funded=yes``), so
    ``matches`` says whether it still belongs on screen; the page drops it when
    it doesn't. ``duplicate_sites`` refreshes just this gene's badge, which is
    the only badge a single-row edit can change.
    """
    target_id = request.POST.get("target_id")
    field = (request.POST.get("field") or "").strip()
    value = request.POST.get("value", "")
    nomination_id = request.POST.get("nomination_id") or None

    target = Target.objects.using(DB).filter(pk=target_id).first()
    if target is None:
        return JsonResponse({"ok": False, "error": "unknown target"}, status=404)

    try:
        if field in _TARGET_FIELDS:
            setattr(target, field, value.strip())
            target.save(using=DB)

        elif field in _NOMINATION_FIELDS:
            nom = (TargetNomination.objects.using(DB)
                   .filter(pk=nomination_id, target_id=target.pk).first()
                   if nomination_id else
                   TargetNomination.objects.using(DB)
                   .filter(target_id=target.pk).order_by("pk").first())
            if nom is None:
                nom = TargetNomination(target_id=target.pk,
                                       created_by_id=getattr(_member(request), "pk", None))
            if field == "funded":
                nom.funded = value.strip().lower() in ("1", "true", "yes", "on")
            elif field == "site":
                nom.site_id = _strict_site_id(value)
            elif field == "project":
                nom.project_id = getattr(tio._resolve_named(Project, value), "pk", None)
            elif field == "granting_agency":
                nom.granting_agency_id = getattr(
                    tio._resolve_named(GrantingAgency, value), "pk", None)
            else:
                setattr(nom, field, value.strip()[:255])
            nom.save(using=DB)

        elif field in _REPORT_FIELDS:
            _save_report_field(target, field, value)

        elif field == "add_class":
            label = value.strip()[:80]
            if label:
                TargetClassification.objects.using(DB).get_or_create(
                    target_id=target.pk, label=label,
                    source=TargetClassification.Source.MANUAL)
        elif field == "remove_class":
            TargetClassification.objects.using(DB).filter(
                target_id=target.pk, label=value.strip(),
                source=TargetClassification.Source.MANUAL).delete()
        else:
            return JsonResponse({"ok": False, "error": f"'{field}' is not editable"},
                                status=400)
    except ValueError as e:
        # A refusal we wrote ourselves, phrased for the person typing — unlike a
        # crash, its text is safe and useful to show.
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        # The message used to be str(e), which put raw Python exception text in
        # front of a bench scientist. The detail belongs in the log.
        logger.exception("board patch failed (target=%s field=%s)", target_id, field)
        return JsonResponse(
            {"ok": False,
             "error": "Could not save that — the value has been left unchanged."},
            status=400)

    fresh = (board.apply_filters(board.board_queryset(), **_filters(request))
             .filter(pk=target.pk).first())
    if fresh is None:
        return JsonResponse({"ok": True, "matches": False, "row": None})
    coverage = board.application_coverage([target.pk])
    return JsonResponse({
        "ok": True,
        "matches": True,
        "row": board.row_for(fresh, coverage),
        "duplicate_sites": board.duplicate_sites_for(fresh.gene_name or ""),
    })


# ---------------------------------------------------------------------------
# File round-trip — the compatibility floor
# ---------------------------------------------------------------------------

@pipeline_member_required
@require_GET
def target_board_export(request):
    """?layout=carl reproduces his sheet; default adds the computed columns."""
    layout = "carl" if (request.GET.get("layout") == "carl") else "board"
    data = tio.export_bytes(layout=layout, **_filters(request))
    resp = HttpResponse(data, content_type=_XLSX)
    name = "target_list_carl_layout.xlsx" if layout == "carl" else "target_list.xlsx"
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@pipeline_member_required
@require_POST
def target_board_upload_preview(request):
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    parsed = tio.parse_workbook(f)
    result = tio.plan(parsed, default_site=request.POST.get("default_site", ""))
    if not result.get("ok"):
        return JsonResponse(result, status=400)
    return JsonResponse(result)


@pipeline_member_required
@require_POST
def target_board_upload_commit(request):
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    parsed = tio.parse_workbook(f)
    if not parsed.get("ok"):
        return JsonResponse(parsed, status=400)
    result = tio.apply(
        parsed,
        member=_member(request),
        apply_overwrites=request.POST.get("apply_overwrites") in ("true", "1", "on"),
        default_site=request.POST.get("default_site", ""),
    )
    return JsonResponse(result)


# ---------------------------------------------------------------------------
# Nomination check + portfolio
# ---------------------------------------------------------------------------

@pipeline_member_required
@require_GET
def nomination_check(request):
    """One gene, or a pasted list — the pre-flight before adding targets."""
    genes = [g.strip() for g in
             (request.GET.get("genes") or request.GET.get("gene") or "")
             .replace(",", "\n").replace("\t", "\n").split("\n") if g.strip()]
    if not genes:
        return JsonResponse({"ok": False, "error": "no gene given"}, status=400)
    results = [board.nomination_check(g) for g in genes[:200]]
    return JsonResponse({
        "ok": True,
        "results": results,
        "summary": {
            "checked": len(results),
            "already_on_list": sum(1 for r in results if r.get("exists")),
            "with_warnings": sum(1 for r in results if r.get("warnings")),
        },
    })


@pipeline_member_required
@require_GET
def target_portfolio(request):
    data = board.portfolio()
    if request.GET.get("format") == "json":
        return JsonResponse({"ok": True, **data})
    return render(request, "pipeline/target_portfolio.html", {"p": data})
