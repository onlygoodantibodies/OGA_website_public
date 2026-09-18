"""
YCharOS Pipeline — Automated Report Generator

Generates .docx reports in the Zenodo/F1000 format for CRAC review.
Structure matches the SYT1 F1000 report (Biddle et al. 2024, 13:817):

    Title
    Abstract
    Introduction          ← from Report.introduction_text (manual)
    Results and Discussion ← templated from session/result data
    Table 1               ← cell lines used
    Table 2               ← antibodies tested
    Table 3               ← secondary antibodies (from session conditions)
    Figure 1              ← WB placeholder + legend
    Figure 2              ← IP placeholder + legend
    Figure 3              ← IF placeholder + legend
    Figure 4              ← FC placeholder + legend
    Methods               ← templated from ProtocolTemplate + session metadata
    Data Availability
    Acknowledgments

Usage:
    from pipeline.services.report_generator import generate_report
    path = generate_report(target_pk=42, output_path="/tmp/SYT1_report.docx")

All DB queries use .using('pipeline_db').
"""

import os
import logging
from datetime import date
from decimal import Decimal
from collections import OrderedDict

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml

from pipeline.services import depmap
# Clonality from the enum and `is_recombinant` together — neither column
# answers it alone. See pipeline/services/clonality.py.
from pipeline.services import clonality as clonality_svc

logger = logging.getLogger(__name__)

DB_ALIAS = 'pipeline_db'


# =============================================================================
# Helpers
# =============================================================================

def _db(qs):
    """Route any queryset to the pipeline database."""
    return qs.using(DB_ALIAS)


def _fmt_decimal(val, places=2):
    """Format a Decimal or None for display."""
    if val is None:
        return 'n/a'
    if isinstance(val, Decimal):
        return f"{val:.{places}f}"
    return str(val)


def _or_dash(val):
    """Return the value or '-' if blank/None."""
    if val is None or str(val).strip() == '':
        return '-'
    return str(val).strip()

def _fmt_rrid(rrid_value):
    """
    Strip the Antibody Registry URL prefix to show just the AB_ identifier,
    matching the F1000 published format: 'AB_2757511' not the full URL.
    """
    if not rrid_value or str(rrid_value).strip() == '':
        return '-'
    val = str(rrid_value).strip()
    # Strip common URL prefixes
    for prefix in [
        'https://www.antibodyregistry.org/',
        'http://www.antibodyregistry.org/',
        'https://antibodyregistry.org/',
    ]:
        if val.startswith(prefix):
            val = val[len(prefix):]
            break
    return val

def _vendor_apps(ab):
    """
    Build the vendor-recommended applications string from boolean flags.
    Matches the SYT1 report format: 'Wb, IP, IF' etc.
    """
    apps = []
    if ab.supplier_validated_wb:
        apps.append('Wb')
    if ab.supplier_validated_ip:
        apps.append('IP')
    if ab.supplier_validated_if:
        apps.append('IF')
    if ab.supplier_validated_fc:
        apps.append('FC')
    if ab.supplier_validated_ihc:
        apps.append('IHC')
    if ab.supplier_validated_elisa:
        apps.append('ELISA')
    # Fall back to free-text field if no booleans set
    if not apps and ab.supplier_validated_applications:
        return ab.supplier_validated_applications
    return ', '.join(apps) if apps else '-'


def _clonality_display(ab):
    """
    Clonality in the SYT1 report's hyphenated house style — 'Recombinant-mono',
    'Recombinant-poly', 'Monoclonal', 'Polyclonal'.

    The fact comes from services/clonality.py, which asks the enum and
    `is_recombinant` together; this only restyles it. The old mapping did two
    things wrong at once, and a report is the last place either belongs. It read
    the enum alone, so Leicester's recombinants printed 'Monoclonal' while
    McGill's identical clones printed 'Recombinant-mono'; and it printed
    '-mono' for **every** stored `recombinant`, which for McGill's rows is an
    invented claim — their Access column held `recombinant mono`, `recombinant
    poly` *and* `recombinant super`, collapsed to one value on import. A row
    that does not record which now prints a bare 'Recombinant'.
    """
    if not clonality_svc.is_recombinant(ab):
        return clonality_svc.label(ab)
    base = (ab.clonality or '').strip().lower()
    if base == clonality_svc.MONOCLONAL:
        return 'Recombinant-mono'
    if base == clonality_svc.POLYCLONAL:
        return 'Recombinant-poly'
    return 'Recombinant'


def _cat_number_with_markers(ab):
    """
    Append * for monoclonal and ** for recombinant, matching the SYT1
    report convention.
    """
    cat = _or_dash(ab.catalogue_number)
    # ** for a recombinant, * for a monoclonal that is not one. Asking the enum
    # alone put * on Leicester's recombinants and ** on McGill's copies of the
    # same clone, in one table — the markers are a legend the reader trusts.
    if clonality_svc.is_recombinant(ab):
        cat += '**'
    elif (ab.clonality or '').strip().lower() == clonality_svc.MONOCLONAL:
        cat += '*'
    return cat


# =============================================================================
# Styling helpers
# =============================================================================

FONT_NAME = 'Arial'
FONT_SIZE_BODY = Pt(10)
FONT_SIZE_SMALL = Pt(8)
FONT_SIZE_TITLE = Pt(14)
FONT_SIZE_HEADING = Pt(12)
FONT_SIZE_TABLE = Pt(8)
FONT_SIZE_TABLE_HEADER = Pt(8)

HEADER_SHADING = 'D5E8F0'  # Light blue header row


def _set_cell_shading(cell, color):
    """Apply background shading to a table cell."""
    shading_elm = parse_xml(
        f'<w:shd {nsdecls("w")} w:fill="{color}" w:val="clear"/>'
    )
    cell._tc.get_or_add_tcPr().append(shading_elm)


def _style_paragraph(paragraph, font_size=FONT_SIZE_BODY, bold=False,
                     italic=False, alignment=None, space_after=Pt(6),
                     space_before=Pt(0), font_name=FONT_NAME):
    """Apply consistent styling to a paragraph."""
    if alignment:
        paragraph.alignment = alignment
    pf = paragraph.paragraph_format
    pf.space_after = space_after
    pf.space_before = space_before
    for run in paragraph.runs:
        run.font.name = font_name
        run.font.size = font_size
        run.font.bold = bold
        run.font.italic = italic


def _add_styled_paragraph(doc_or_container, text, font_size=FONT_SIZE_BODY,
                          bold=False, italic=False,
                          alignment=None, space_after=Pt(6),
                          space_before=Pt(0)):
    """Add a paragraph with consistent styling."""
    p = doc_or_container.add_paragraph()
    run = p.add_run(text)
    run.font.name = FONT_NAME
    run.font.size = font_size
    run.font.bold = bold
    run.font.italic = italic
    pf = p.paragraph_format
    pf.space_after = space_after
    pf.space_before = space_before
    if alignment:
        p.alignment = alignment
    return p


def _add_heading(doc, text, level=1):
    """Add a heading styled to match the F1000 format."""
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.name = FONT_NAME
        run.font.color.rgb = RGBColor(0, 0, 0)
    return h


def _build_table(doc, headers, rows, col_widths_inches=None):
    """
    Build a formatted table matching the F1000 style:
    - Light-blue header row
    - Alternating row shading (optional)
    - Small font, compact layout
    """
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = 'Table Grid'

    # Header row
    header_row = table.rows[0]
    for i, header_text in enumerate(headers):
        cell = header_row.cells[i]
        cell.text = ''
        p = cell.paragraphs[0]
        run = p.add_run(header_text)
        run.font.name = FONT_NAME
        run.font.size = FONT_SIZE_TABLE_HEADER
        run.font.bold = True
        _set_cell_shading(cell, HEADER_SHADING)

    # Data rows
    for row_idx, row_data in enumerate(rows):
        row = table.rows[1 + row_idx]
        for col_idx, cell_text in enumerate(row_data):
            cell = row.cells[col_idx]
            cell.text = ''
            p = cell.paragraphs[0]
            run = p.add_run(str(cell_text))
            run.font.name = FONT_NAME
            run.font.size = FONT_SIZE_TABLE

    # Apply column widths if specified
    if col_widths_inches:
        for row in table.rows:
            for idx, width in enumerate(col_widths_inches):
                if idx < len(row.cells):
                    row.cells[idx].width = Inches(width)

    return table


def _add_figure_placeholder(doc, figure_number, legend_text, width_inches=6.0,
                            height_inches=3.5):
    """
    Add a grey placeholder box for a figure, followed by the legend.
    Matches the SYT1 report layout where figures are full-width images
    with detailed legends below.
    """
    # Placeholder paragraph
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Create a grey rectangle as a placeholder using a bordered paragraph
    placeholder_p = doc.add_paragraph()
    placeholder_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = placeholder_p.add_run(f'[Figure {figure_number} — Image placeholder]')
    run.font.name = FONT_NAME
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(128, 128, 128)
    run.font.italic = True

    # Add shading to the placeholder paragraph to simulate a grey box
    pPr = placeholder_p._p.get_or_add_pPr()
    shading = parse_xml(
        f'<w:shd {nsdecls("w")} w:fill="E8E8E8" w:val="clear"/>'
    )
    pPr.append(shading)

    # Set paragraph spacing to create visual height
    pf = placeholder_p.paragraph_format
    pf.space_before = Pt(60)
    pf.space_after = Pt(60)

    # Legend
    legend_p = doc.add_paragraph()
    # Bold "Figure N." prefix
    bold_run = legend_p.add_run(f'Figure {figure_number}. ')
    bold_run.font.name = FONT_NAME
    bold_run.font.size = FONT_SIZE_SMALL
    bold_run.font.bold = True
    # Legend body
    body_run = legend_p.add_run(legend_text)
    body_run.font.name = FONT_NAME
    body_run.font.size = FONT_SIZE_SMALL
    legend_p.paragraph_format.space_after = Pt(12)

    return placeholder_p


# =============================================================================
# Data collection — all queries via .using('pipeline_db')
# =============================================================================

def _get_target(target_pk):
    """Fetch the target with related objects."""
    from pipeline.models import Target
    return _db(
        Target.objects.select_related('project', 'granting_agency', 'site')
    ).get(pk=target_pk)


def _get_report(target):
    """Fetch or create the Report record for this target."""
    from pipeline.models import Report
    report = _db(
        Report.objects.filter(target=target)
    ).order_by('-generated_at').first()
    return report


def _get_cell_lines(target):
    """
    Fetch cell lines for Table 1.
    Returns WT lines first, then KO, matching the SYT1 report order.

    A wild type is recorded once, with no gene, so ``filter(target=…)`` alone
    can never return one — and the whole document is a comparison of WT against
    KO. Table 1 listed only the knockout, and every figure legend fell back to
    the literal placeholder "[WT cell line]", in a document whose Method
    paragraph two lines later named HAP1 correctly. The parent is fetched by the
    knockout's FK, and the WT a session actually ran against is picked up too,
    since a session names its lysates even when the KO row's parent is blank.
    """
    from pipeline.models import CellLine, ExperimentSession
    lines = list(_db(
        CellLine.objects
        .filter(target=target)
        # `target` is select_related because Table 1 now names the gene each
        # knockout is a knockout *of*, and a lazy FK there is one query per row.
        .select_related('company', 'site', 'target')
    ))
    known = {cl.pk for cl in lines}
    wt_ids = {cl.parent_line_id for cl in lines if cl.parent_line_id}
    wt_ids |= set(_db(
        ExperimentSession.objects.filter(target=target)
    ).values_list('cell_line_wt_id', flat=True))
    wt_ids -= known | {None}
    if wt_ids:
        # `genotype='WT'` is not decoration. A session's `cell_line_wt` is
        # whatever was resolved into it, and the seventh field test resolved
        # another institution's PRKN knockout into a STMN2 session's wild-type
        # slot. It arrived here unchecked, and Table 1 printed
        # `McGill | ab280042 | - | SH-SY5Y | STMN2 KO` — a real row, at another
        # site, for a different gene, relabelled as this gene's knockout — in a
        # document written to be deposited. The resolver that let it in is fixed;
        # this is the second lock, because the bad rows it already wrote are
        # still in the database and a report must not launder them.
        lines += list(_db(
            CellLine.objects.filter(pk__in=wt_ids, genotype='WT')
            .select_related('company', 'site', 'target')
        ))
    # WT before KO (W > K alphabetically), then by name — the SYT1 report order.
    lines.sort(key=lambda cl: (cl.genotype != 'WT', cl.name or ''))
    return lines


def _session_conditions(sessions):
    """The protocol conditions a procedure's first session actually recorded.

    Template first, then the session's own overrides — the same order every
    Method builder uses, gathered here so a paragraph outside them can ask the
    same question rather than hardcoding an answer.
    """
    conditions = {}
    session = (sessions or [None])[0]
    if session is None:
        return conditions
    if session.protocol_template and session.protocol_template.conditions:
        conditions.update(session.protocol_template.conditions)
    if session.session_conditions:
        conditions.update(session.session_conditions)
    return conditions


def _recorded(conditions, *keys, gap):
    """A condition as it was actually recorded, or a **named gap**.

    Every one of these used to carry a plausible default: no lysis buffer on file
    printed "RIPA", no loading printed "40 µg", no cytometer printed "Attune
    NxT". The seventh field test generated a Data Note for a session whose
    conditions were empty and whose `protein_loading_ug` was null, and read back
    a Method section stating both — fluent, specific, and describing an
    experiment nobody had recorded. A draft that invents a number is worse than
    one with a hole in it, because the hole gets filled in and the number gets
    published.

    So an unrecorded value becomes `[lysis buffer]` — the same square-bracket
    convention the document already uses for an unknown wild type, and specific
    enough that whoever edits the draft knows exactly what to supply. A value
    whose absence merely omits a clause (a dilution, an objective) keeps its
    empty default: leaving words out invents nothing.
    """
    for key in keys:
        value = conditions.get(key)
        if value not in (None, ''):
            return str(value)
    return f'[{gap}]'


def _session_wt_name(session, cell_lines):
    """The wild type a Method paragraph should name.

    Table 1 and every figure legend resolve the wild type through the
    knockout's ``parent_line`` (see ``_get_cell_lines``); the Method paragraphs
    read ``session.cell_line_wt`` instead. Two resolvers, and the seventh field
    test found the second one blank on three of four procedures — because the
    workbook importer was dropping the column — so one document said **HAP1** in
    Table 1, said **HAP1** in all four legends, and printed a literal
    ``[WT cell line]`` in three Method paragraphs.

    The importer is fixed, but a Method paragraph must not depend on that: any
    session predating the fix, or recorded without a wild type, still has to
    name the line the rest of the document names. So this falls back to the same
    source Table 1 uses, and only then to the placeholder.
    """
    if session is not None and getattr(session, 'cell_line_wt', None):
        return session.cell_line_wt.name
    for cl in (cell_lines or []):
        if cl.genotype == 'WT' and cl.name:
            return cl.name
    return '[WT cell line]'


def _get_antibodies(target):
    """
    Fetch antibodies for Table 2, ordered by company then catalogue number.

    Two filters applied to match F1000 report conventions:
    1. Only antibodies with at least one experiment result (WB/IP/IF/FC)
    2. Deduplicated by (catalogue_number, company) — same antibody imported
       from multiple sites appears once, keeping the most complete record.
    """
    from django.db.models import Q, Count
    from pipeline.models import Antibody

    # Only antibodies that have at least one result in any procedure
    tested = _db(
        Antibody.objects
        .filter(target=target)
        .filter(
            Q(wb_results__isnull=False) |
            Q(ip_results__isnull=False) |
            Q(if_results__isnull=False) |
            Q(fc_results__isnull=False)
        )
        .select_related('company')
        .annotate(result_count=Count('wb_results') + Count('ip_results')
                  + Count('if_results') + Count('fc_results'))
        .order_by('company__name', 'catalogue_number')
        .distinct()
    )

    # Deduplicate by (catalogue_number, company) — keep the record with
    # the most results (i.e. the site that actually ran experiments)
    seen = {}
    for ab in tested:
        key = (
            (ab.catalogue_number or '').strip().upper(),
            ab.company_id,
        )
        existing = seen.get(key)
        if existing is None or ab.result_count > existing.result_count:
            seen[key] = ab

    return sorted(seen.values(), key=lambda a: (
        a.company.name if a.company else '',
        a.catalogue_number or '',
    ))


def _get_sessions(target):
    """Fetch all experiment sessions for the target, grouped by procedure type."""
    from pipeline.models import ExperimentSession
    sessions = list(_db(
        ExperimentSession.objects
        .filter(target=target)
        .select_related(
            'protocol_template', 'experimenter', 'site',
            'cell_line_wt', 'cell_line_ko'
        )
        .prefetch_related(
            'wb_results__antibody__company',
            'ip_results__antibody__company',
            'if_results__antibody__company',
            'fc_results__antibody__company',
        )
        .order_by('procedure_type', 'date')
    ))
    grouped = OrderedDict()
    for s in sessions:
        # **A row nobody wrote a reading on is not an experiment**, and a report
        # is the last place that rule can still be applied. ELP3 had three WB
        # sessions — one with no result rows and two with a blank row each,
        # created while testing the quick panel — and the abstract announced
        # that three antibodies had been characterized for western blot, over a
        # full Method paragraph and a figure legend. `session_import._has_result`
        # already refuses to *create* such a session from a workbook; this is the
        # same sentence at the other end of the pipeline.
        if not _has_readings(s):
            continue
        grouped.setdefault(s.procedure_type, []).append(s)
    return grouped


_RESULT_ACCESSOR = {'WB': 'wb_results', 'IP': 'ip_results',
                    'IF': 'if_results', 'FC': 'fc_results'}


def _readings_in(session) -> int:
    """How many of this session's result rows anybody actually wrote on.

    Asked of the model's own result fields (`session_board.result_field_names`),
    never a hand-written list — there are three of those already and this must
    not become a fourth.
    """
    from pipeline.services.session_board import reading_fields

    accessor = _RESULT_ACCESSOR.get(session.procedure_type)
    if not accessor:
        return 0
    fields = reading_fields(session.procedure_type)
    count = 0
    for row in getattr(session, accessor).all():
        for name in fields:
            value = getattr(row, name, None)
            if value is None or value is False:
                continue
            if str(value).strip():
                count += 1
                break
    return count


def _has_readings(session) -> bool:
    """Did anybody write a reading on any of this session's result rows?"""
    return _readings_in(session) > 0


def draft_contents(target) -> dict:
    """What a draft Data Note for this gene would contain, before it is asked for.

    Generate Report on a gene with nothing on file produces a 39 KB document: an
    abstract reading *"we have characterized 0 antibodies"*, two header-only
    tables, and a title ending *"for use in [no application has a recorded result
    yet]"*. That is the honest skeleton this generator is meant to produce — the
    bracketed gaps are the same convention `_recorded` uses throughout, and a
    draft that named a plausible buffer nobody recorded would be far worse. But
    the gene page knows every one of those numbers **before** the press and said
    nothing, so the field test pressed it on an empty sandbox target and got a
    document that is 95% placeholder.

    Counted the way the *document* counts, never off the session rows. A session
    carrying one blank result row is not an experiment and `_get_sessions` drops
    it; `views/dashboard.py`'s own `procedure_summary` counts rows, so reading
    that here would put a number on the panel the file then disagrees with — two
    answers to one question on one screen, which is the shape that reads as data
    loss.

    Scoped to one target and bounded by it: the counts do not grow with the
    dataset, which is the cost rule these pages die on.
    """
    sessions_by_type = _get_sessions(target)
    sessions = [s for group in sessions_by_type.values() for s in group]
    readings = sum(_readings_in(s) for s in sessions)
    antibodies = len(_get_antibodies(target))
    cell_lines = len(_get_cell_lines(target))

    # Next to a number is where a grammar slip costs most — it makes a careful
    # reader distrust the number. `(s)` is the same slip written down in advance.
    def _n(count, one, many):
        return f"{count} {one if count == 1 else many}"

    out = {
        "antibodies": antibodies,
        "cell_lines": cell_lines,
        "sessions": len(sessions),
        "readings": readings,
        "procedures": sorted(sessions_by_type),
        "empty": not readings,
        # What the draft would be built from, in the document's own terms.
        "summary": (f"{_n(antibodies, 'antibody', 'antibodies')}, "
                    f"{_n(cell_lines, 'cell line', 'cell lines')} and "
                    f"{_n(readings, 'recorded reading', 'recorded readings')}"
                    + (f" across {', '.join(sorted(sessions_by_type))}"
                       if sessions_by_type else "")),
    }
    # Only when there is nothing to write up. A gene part-way through is exactly
    # what the bracketed gaps are for, and warning about it would nag about work
    # nobody has done yet — the same reason `gene_progress` reports no percentage.
    out["warning"] = ("" if readings else
                      "Nothing has been recorded against this gene yet, so the "
                      "draft would be a skeleton: empty tables, and every method "
                      "and result a bracketed gap for you to fill in.")
    return out


def _get_secondary_antibodies(sessions_by_type):
    """
    Extract secondary antibody info from session-level conditions and
    per-result records to build Table 3.

    Sources (checked in order):
    1. session.session_conditions JSON (keys like 'secondary_ab',
       'secondary_antibody', 'secondary_dilution')
    2. ProtocolTemplate.conditions JSON
    3. Per-result fields (WbResult.secondary_ab, IfResult.secondary_ab, etc.)
    """
    secondaries = OrderedDict()  # key: (procedure, description) → details dict

    for proc_type, sessions in sessions_by_type.items():
        for session in sessions:
            # Merge template conditions with session overrides
            conditions = {}
            if session.protocol_template and session.protocol_template.conditions:
                conditions.update(session.protocol_template.conditions)
            if session.session_conditions:
                conditions.update(session.session_conditions)

            # Extract secondary antibody from conditions JSON
            sec_ab = (
                conditions.get('secondary_antibody')
                or conditions.get('secondary_ab')
                or ''
            )
            sec_dilution = (
                conditions.get('secondary_dilution')
                or conditions.get('secondary_ab_dilution')
                or ''
            )
            sec_source = conditions.get('secondary_source', '')
            sec_cat = conditions.get('secondary_catalogue', '')

            if sec_ab:
                key = (proc_type, sec_ab)
                if key not in secondaries:
                    secondaries[key] = {
                        'procedure': proc_type,
                        'antibody': sec_ab,
                        'dilution': sec_dilution,
                        'source': sec_source,
                        'catalogue': sec_cat,
                    }

            # Also scan per-result secondary fields for WB
            if proc_type == 'WB':
                for r in session.wb_results.all():
                    if r.secondary_ab:
                        key = ('WB', r.secondary_ab)
                        if key not in secondaries:
                            secondaries[key] = {
                                'procedure': 'WB',
                                'antibody': r.secondary_ab,
                                'dilution': r.secondary_ab_dilution or sec_dilution,
                                'source': sec_source,
                                'catalogue': sec_cat,
                            }

            # IF secondary fields
            if proc_type == 'IF':
                for r in session.if_results.all():
                    if r.secondary_ab:
                        key = ('IF', r.secondary_ab)
                        if key not in secondaries:
                            secondaries[key] = {
                                'procedure': 'IF',
                                'antibody': r.secondary_ab,
                                'dilution': '',
                                'source': sec_source,
                                'catalogue': sec_cat,
                            }

            # IP secondary fields
            if proc_type == 'IP':
                for r in session.ip_results.all():
                    if r.secondary_ab:
                        key = ('IP', r.secondary_ab)
                        if key not in secondaries:
                            secondaries[key] = {
                                'procedure': 'IP',
                                'antibody': r.secondary_ab,
                                'dilution': r.secondary_ab_dilution or '',
                                'source': sec_source,
                                'catalogue': sec_cat,
                            }

    return list(secondaries.values())


def _group_by_procedure(secondaries):
    """`{procedure: [entry, …]}`, in the order the procedures first appear."""
    grouped = OrderedDict()
    for sec in secondaries:
        grouped.setdefault(sec['procedure'], []).append(sec)
    return grouped


def _get_wb_dilutions(sessions_by_type):
    """
    Collect per-antibody dilutions from WB results for the Figure 1 legend.
    Returns dict: catalogue_number → dilution string.
    """
    dilutions = OrderedDict()
    for session in sessions_by_type.get('WB', []):
        for r in session.wb_results.all():
            ab = r.antibody
            cat = _cat_number_with_markers(ab)
            dil = r.dilution or r.primary_ab_dilution or '-'
            if cat not in dilutions:
                dilutions[cat] = dil
    return dilutions


def _get_if_dilutions(sessions_by_type):
    """Collect per-antibody dilutions/concentrations from IF results for the Figure 3 legend."""
    dilutions = OrderedDict()
    for session in sessions_by_type.get('IF', []):
        for r in session.if_results.all():
            ab = r.antibody
            cat = _cat_number_with_markers(ab)
            dil = r.primary_ab_dilution or r.best_concentration or '-'
            if cat not in dilutions:
                dilutions[cat] = dil
    return dilutions


def _get_fc_dilutions(sessions_by_type):
    """Collect per-antibody concentrations from FC results for the Figure 4 legend."""
    dilutions = OrderedDict()
    for session in sessions_by_type.get('FC', []):
        for r in session.fc_results.all():
            ab = r.antibody
            cat = _cat_number_with_markers(ab)
            conc = r.concentration or '-'
            if cat not in dilutions:
                dilutions[cat] = conc
    return dilutions


# =============================================================================
# Methods text generation
# =============================================================================

def _build_wb_methods(target, sessions, cell_lines, antibodies):
    """
    Generate WB methods paragraph from session data and protocol templates.
    Templated from the SYT1 report and Nature Protocols paper.
    """
    if not sessions:
        return None

    session = sessions[0]  # Use first WB session as representative
    conditions = {}
    if session.protocol_template and session.protocol_template.conditions:
        conditions.update(session.protocol_template.conditions)
    if session.session_conditions:
        conditions.update(session.session_conditions)

    wt_name = _session_wt_name(session, cell_lines)
    ko_name = session.cell_line_ko.name if session.cell_line_ko else '[KO cell line]'
    gene = target.gene_name or target.protein_name

    lysis_buffer = _recorded(conditions, 'lysis_buffer', gap='lysis buffer')
    protein_ug = _recorded(conditions, 'protein_loading_ug', gap='protein loading')
    gel = _recorded(conditions, 'gel_chemistry', 'gel', gap='gel chemistry')
    membrane = _recorded(conditions, 'membrane', gap='membrane')
    blocking = _recorded(conditions, 'blocking', gap='blocking buffer')
    ecl = _recorded(conditions, 'ecl_type', 'ecl', gap='ECL substrate')
    imaging = _recorded(conditions, 'imaging_system', 'detection_system', gap='imaging system')
    sec_ab = _recorded(conditions, 'secondary_antibody', 'secondary_ab', gap='secondary antibody')
    sec_dil = conditions.get('secondary_dilution', '')

    num_abs = len(antibodies)
    mass_kda = _fmt_decimal(target.theoretical_mass_kda) if target.theoretical_mass_kda else '[XX]'

    text = (
        f"For western blot experiments, {wt_name} WT and {gene} KO protein "
        # No trailing "buffer": the recorded value is already a buffer name and
        # often carries the word itself ("Pierce IP Lysis Buffer"), and a gap
        # reads as "[lysis buffer] buffer" with it.
        f"lysates were prepared using {lysis_buffer} and {protein_ug} \u00b5g of "
        f"protein were processed for western blot with the indicated "
        f"{target.protein_name} antibodies. Proteins were separated on precast "
        f"midi {gel} polyacrylamide gels and transferred onto {membrane} "
        f"membranes. Membranes were blocked with {blocking} and probed with "
        f"primary antibodies at the dilutions indicated in Table 2 and "
        f"Figure 1. Detection was performed using {sec_ab}"
    )
    if sec_dil:
        text += f" at {sec_dil}"
    text += (
        f" and {ecl} substrate, with images acquired on the {imaging}. "
        f"The Ponceau stained transfers of each blot are presented to show "
        f"equal loading of WT and KO lysates and protein transfer efficiency. "
        f"Predicted band size: {mass_kda} kDa."
    )
    return text


def _build_ip_methods(target, sessions, cell_lines=None):
    """Generate IP methods paragraph."""
    if not sessions:
        return None

    session = sessions[0]
    conditions = {}
    if session.protocol_template and session.protocol_template.conditions:
        conditions.update(session.protocol_template.conditions)
    if session.session_conditions:
        conditions.update(session.session_conditions)

    wt_name = _session_wt_name(session, cell_lines)
    gene = target.gene_name or target.protein_name
    bead_type = _recorded(conditions, 'bead_type', gap='bead type')
    ab_amount = _recorded(conditions, 'antibody_amount_ug', gap='antibody amount')
    gel = _recorded(conditions, 'gel_chemistry', 'gel', gap='gel chemistry')

    # Find the detection antibody used for IP-WB step
    detection_ab = None
    for r in session.ip_results.all():
        if r.detection_ab:
            detection_ab = r.detection_ab
            break
    if not detection_ab:
        detection_ab = _recorded(conditions, 'detection_antibody', gap='detection antibody')

    detection_dil = ''
    for r in session.ip_results.all():
        if r.detection_ab_dilution:
            detection_dil = r.detection_ab_dilution
            break
    if not detection_dil:
        detection_dil = conditions.get('detection_dilution', '')

    text = (
        f"{wt_name} lysates were prepared, and immunoprecipitation was "
        f"performed using {ab_amount} \u00b5g of the indicated {target.protein_name} "
        f"antibodies pre-coupled to {bead_type}. Samples were washed and "
        f"processed for western blot with the indicated {target.protein_name} "
        f"antibody on a precast midi {gel} polyacrylamide gel. "
        f"For western blot, {detection_ab} was used"
    )
    if detection_dil:
        text += f" at {detection_dil}"
    text += (
        ". The Ponceau stained transfers of each blot are shown. "
        "SM=4% starting material; UB=4% unbound fraction; "
        "IP=immunoprecipitate; HC=antibody heavy chain."
    )
    return text


def _build_if_methods(target, sessions, cell_lines):
    """Generate IF methods text — references Nature Protocols mosaic strategy."""
    if not sessions:
        return None

    session = sessions[0]
    conditions = {}
    if session.protocol_template and session.protocol_template.conditions:
        conditions.update(session.protocol_template.conditions)
    if session.session_conditions:
        conditions.update(session.session_conditions)

    wt_name = _session_wt_name(session, cell_lines)
    ko_name = session.cell_line_ko.name if session.cell_line_ko else '[KO cell line]'
    gene = target.gene_name or target.protein_name

    fixation = _recorded(conditions, 'fixation', 'fixative', gap='fixative')
    permeab = _recorded(conditions, 'permeabilisation', gap='permeabilisation')
    blocking = _recorded(conditions, 'blocking', gap='blocking buffer')
    sec_ab = _recorded(conditions, 'secondary_antibody', 'secondary_ab', gap='secondary antibody')
    microscope = _recorded(conditions, 'microscope', gap='microscope')
    objective = conditions.get('objective', '')

    text = (
        f"For immunofluorescence, antibodies were screened using a mosaic "
        f"strategy. {wt_name} WT and {gene} KO cells were labelled with "
        f"different fluorescent dyes in order to distinguish the two cell "
        f"lines. WT and KO cells were mixed and plated at a 1:1 ratio in a "
        f"96-well plate with optically clear flat-bottom. Cells were fixed with "
        f"{fixation}, permeabilised with {permeab}, and blocked with {blocking}. "
        f"Cells were stained with the indicated {target.protein_name} antibodies "
        f"and with the corresponding {sec_ab} including DAPI. "
        f"Acquisition of the blue (nucleus-DAPI), green (WT), red (antibody "
        f"staining) and far-red (KO) channels was performed"
    )
    if microscope and microscope != '[microscope]':
        text += f" using the {microscope}"
        if objective:
            text += f" with {objective} objective"
    text += (
        ". Representative images of the merged blue and red (grayscale) "
        "channels are shown. WT and KO cells are outlined with green and "
        "magenta dashed line, respectively."
    )
    return text


def _build_fc_methods(target, sessions, cell_lines=None):
    """
    Generate FC methods text — this was included in full in the SYT1 report
    body (not just referenced to the Nature Protocols paper) since FC is a
    newer addition to the YCharOS platform.
    """
    if not sessions:
        return None

    session = sessions[0]
    conditions = {}
    if session.protocol_template and session.protocol_template.conditions:
        conditions.update(session.protocol_template.conditions)
    if session.session_conditions:
        conditions.update(session.session_conditions)

    wt_name = _session_wt_name(session, cell_lines)
    ko_name = session.cell_line_ko.name if session.cell_line_ko else '[KO cell line]'
    gene = target.gene_name or target.protein_name

    tracker_green = _recorded(conditions, 'tracker_dye_wt', gap='WT tracker dye')
    tracker_violet = _recorded(conditions, 'tracker_dye_ko', gap='KO tracker dye')
    fixative = _recorded(conditions, 'fixation', 'fixative', gap='fixative')
    permeab = _recorded(conditions, 'permeabilisation', gap='permeabilisation')
    blocking = _recorded(conditions, 'blocking', gap='blocking buffer')
    sec_ab = _recorded(conditions, 'secondary_antibody', 'secondary_ab',
                       gap='secondary antibody')
    sec_conc = _recorded(conditions, 'secondary_concentration', gap='secondary concentration')
    cytometer = _recorded(conditions, 'flow_cytometer', gap='flow cytometer')
    analysis_sw = _recorded(conditions, 'analysis_software', gap='analysis software')
    cell_count = _recorded(conditions, 'cell_count', gap='cell count')
    sub_protocol = session.get_fc_sub_protocol_display() if session.fc_sub_protocol != 'na' else ''

    text = (
        f"{wt_name} WT and {gene} KO cells were detached, and three million "
        f"cells were labelled with {tracker_green} or {tracker_violet} fluorescent "
        f"dyes, respectively. WT and KO cells were then combined at a 1:1 ratio, "
        f"fixed with {fixative} for 20 min on ice, and permeabilised with "
        f"{permeab}. Cells were blocked with {blocking} for 30 min on ice. "
        f"{cell_count} cells were aliquoted into individually labelled tubes and "
        f"incubated with primary {target.protein_name} antibodies for 30 min on "
        f"ice. Cells were then incubated with {sec_ab} ({sec_conc}) for 30 min "
        f"on ice. Data was acquired using the {cytometer}. Data was analysed "
        f"using {analysis_sw} with the following gates: the cell population was "
        f"gated on FSC-A vs SSC-A, within that gate single cells were selected "
        f"by FSC-A vs FSC-H, and then KO and WT cells were isolated using a "
        f"quadrant gate."
    )
    return text


# =============================================================================
# Figure legend builders
# =============================================================================

def _wb_legend(target, sessions_by_type, cell_lines, antibodies):
    """Build the standardised WB figure legend matching SYT1 report Figure 1."""
    wt_names = [cl.name for cl in cell_lines if cl.genotype == 'WT']
    ko_names = [cl.name for cl in cell_lines if cl.genotype == 'KO']
    wt_str = ', '.join(wt_names) if wt_names else '[WT cell line]'
    ko_str = ', '.join(ko_names) if ko_names else '[KO cell line]'
    gene = target.gene_name or target.protein_name

    sessions = sessions_by_type.get('WB', [])
    conditions = {}
    if sessions:
        s = sessions[0]
        if s.protocol_template and s.protocol_template.conditions:
            conditions.update(s.protocol_template.conditions)
        if s.session_conditions:
            conditions.update(s.session_conditions)

    protein_ug = _recorded(conditions, 'protein_loading_ug', gap='protein loading')
    gel = _recorded(conditions, 'gel_chemistry', 'gel', gap='gel chemistry')
    membrane = _recorded(conditions, 'membrane', gap='membrane')
    mass_kda = _fmt_decimal(target.theoretical_mass_kda) if target.theoretical_mass_kda else '[XX]'

    # Per-antibody dilutions
    dilutions = _get_wb_dilutions(sessions_by_type)
    dil_parts = [f"{cat} at {dil}" for cat, dil in dilutions.items()]
    dil_str = ', '.join(dil_parts) if dil_parts else '[dilutions to be added]'

    legend = (
        f"{target.protein_name} antibody screening by western blot. "
        f"Lysates of {wt_str} (WT and {gene} KO) were prepared and "
        f"{protein_ug} \u00b5g of protein were processed for western blot "
        f"with the indicated {target.protein_name} antibodies. The Ponceau "
        f"stained transfers of each blot are presented to show equal loading "
        f"of WT and KO lysates and protein transfer efficiency from the "
        f"precast midi {gel} polyacrylamide gels to the {membrane} membrane. "
        f"Antibody dilutions were chosen according to the recommendations "
        f"of the antibody supplier. Antibody dilution used: {dil_str}. "
        f"Predicted band size: {mass_kda} kDa. "
        f"*Monoclonal antibody; **Recombinant antibody."
    )
    return legend


def _ip_legend(target, sessions_by_type, cell_lines):
    """Build the standardised IP figure legend matching SYT1 report Figure 2."""
    wt_names = [cl.name for cl in cell_lines if cl.genotype == 'WT']
    wt_str = ', '.join(wt_names) if wt_names else '[WT cell line]'
    gene = target.gene_name or target.protein_name

    sessions = sessions_by_type.get('IP', [])
    conditions = {}
    if sessions:
        s = sessions[0]
        if s.protocol_template and s.protocol_template.conditions:
            conditions.update(s.protocol_template.conditions)
        if s.session_conditions:
            conditions.update(s.session_conditions)

    ab_amount = _recorded(conditions, 'antibody_amount_ug', gap='antibody amount')
    bead_type = _recorded(conditions, 'bead_type', gap='bead type')
    gel = _recorded(conditions, 'gel_chemistry', 'gel', gap='gel chemistry')

    # Find detection antibody
    detection_ab = '[detection antibody]'
    detection_dil = ''
    for session in sessions:
        for r in session.ip_results.all():
            if r.detection_ab:
                detection_ab = r.detection_ab
                detection_dil = r.detection_ab_dilution or ''
                break
        if detection_ab != '[detection antibody]':
            break

    legend = (
        f"{target.protein_name} antibody screening by immunoprecipitation. "
        f"{wt_str} lysates were prepared, and immunoprecipitation was performed "
        f"using {ab_amount} \u00b5g of the indicated {target.protein_name} antibodies "
        f"pre-coupled to {bead_type}. Samples were washed and processed for "
        f"western blot with the indicated {target.protein_name} antibody on a "
        f"precast midi {gel} polyacrylamide gel. For western blot, "
        f"{detection_ab} was used"
    )
    if detection_dil:
        legend += f" at {detection_dil}"
    legend += (
        ". The Ponceau stained transfers of each blot are shown. "
        "SM=4% starting material; UB=4% unbound fraction; "
        "IP=immunoprecipitate; HC=antibody heavy chain. "
        "*Monoclonal antibody; **Recombinant antibody."
    )
    return legend


def _if_legend(target, sessions_by_type, cell_lines):
    """Build the standardised IF figure legend matching SYT1 report Figure 3."""
    wt_names = [cl.name for cl in cell_lines if cl.genotype == 'WT']
    ko_names = [cl.name for cl in cell_lines if cl.genotype == 'KO']
    wt_str = ', '.join(wt_names) if wt_names else '[WT cell line]'
    ko_str = ', '.join(ko_names) if ko_names else '[KO cell line]'
    gene = target.gene_name or target.protein_name

    sessions = sessions_by_type.get('IF', [])
    conditions = {}
    if sessions:
        s = sessions[0]
        if s.protocol_template and s.protocol_template.conditions:
            conditions.update(s.protocol_template.conditions)
        if s.session_conditions:
            conditions.update(s.session_conditions)

    sec_ab = _recorded(conditions, 'secondary_antibody', 'secondary_ab', gap='secondary antibody')

    # Per-antibody dilutions
    dilutions = _get_if_dilutions(sessions_by_type)
    dil_parts = [f"{cat} at {dil}" for cat, dil in dilutions.items()]
    dil_str = ', '.join(dil_parts) if dil_parts else '[dilutions to be added]'

    legend = (
        f"{target.protein_name} antibody screening by immunofluorescence. "
        f"{wt_str} WT and {gene} KO cells were labelled with a green or a "
        f"far-red fluorescent dye, respectively. WT and KO cells were mixed "
        f"and plated to a 1:1 ratio in a 96-well plate with optically clear "
        f"flat-bottom. Cells were stained with the indicated "
        f"{target.protein_name} antibodies and with the corresponding "
        f"{sec_ab} including DAPI. Acquisition of the blue (nucleus-DAPI), "
        f"green (WT), red (antibody staining) and far-red (KO) channels was "
        f"performed. Representative images of the merged blue and red "
        f"(grayscale) channels are shown. WT and KO cells are outlined with "
        f"green and magenta dashed line, respectively. Antibody dilution "
        f"used: {dil_str}. Bars = 10 \u00b5m. "
        f"*Monoclonal antibody; **Recombinant antibody."
    )
    return legend


def _fc_legend(target, sessions_by_type, cell_lines):
    """Build the standardised FC figure legend matching SYT1 report Figure 4."""
    wt_names = [cl.name for cl in cell_lines if cl.genotype == 'WT']
    ko_names = [cl.name for cl in cell_lines if cl.genotype == 'KO']
    wt_str = ', '.join(wt_names) if wt_names else '[WT cell line]'
    ko_str = ', '.join(ko_names) if ko_names else '[KO cell line]'
    gene = target.gene_name or target.protein_name

    sessions = sessions_by_type.get('FC', [])
    conditions = {}
    if sessions:
        s = sessions[0]
        if s.protocol_template and s.protocol_template.conditions:
            conditions.update(s.protocol_template.conditions)
        if s.session_conditions:
            conditions.update(s.session_conditions)

    fixative = _recorded(conditions, 'fixation', 'fixative', gap='fixative')
    permeab = _recorded(conditions, 'permeabilisation', gap='permeabilisation')
    sec_ab = _recorded(conditions, 'secondary_antibody', 'secondary_ab',
                       gap='secondary antibody')
    cytometer = _recorded(conditions, 'flow_cytometer', gap='flow cytometer')

    # Per-antibody concentrations. The blanket "diluted to 1 \u00b5g/ml" was a number
    # nobody had recorded, stated about every antibody in the figure.
    dilutions = _get_fc_dilutions(sessions_by_type)
    default_conc = _recorded(conditions, 'primary_concentration',
                             gap='primary antibody concentration')

    legend = (
        f"{target.protein_name} antibody screening by flow cytometry. "
        f"{wt_str} WT and {gene} KO cells were labelled with a green or "
        f"violet fluorescent dye, respectively. WT and KO cells were mixed "
        f"in a 1:1 ratio, fixed in {fixative} and permeabilised in {permeab}. "
        f"Cells were stained with the indicated {target.protein_name} "
        f"antibodies and {sec_ab}. Antibody staining was quantified using "
        f"the {cytometer} with representative images showing the staining "
        f"intensity in the KO population (pink histogram, dashed line) "
        f"compared to the WT cells (green histogram, solid line). Histograms "
        f"with dotted lines represent secondary antibody-only controls in "
        f"both WT and KO cells. All primary antibodies were diluted to "
        f"{default_conc} unless otherwise noted. "
        f"*Monoclonal antibody; **Recombinant antibody."
    )
    return legend


# =============================================================================
# Main report assembly
# =============================================================================

def generate_report(target_pk, output_path=None):
    """
    Generate a .docx report for the given target in Zenodo/F1000 format.

    Args:
        target_pk: Primary key of the Target record.
        output_path: Optional file path for the output .docx.
                     If None, generates a default path in /tmp/.

    Returns:
        str: Absolute path to the generated .docx file.

    Raises:
        pipeline.models.Target.DoesNotExist: If the target_pk is invalid.
    """
    # ── Collect all data ──────────────────────────────────────────────
    target = _get_target(target_pk)
    report = _get_report(target)
    cell_lines = _get_cell_lines(target)
    antibodies = _get_antibodies(target)
    sessions_by_type = _get_sessions(target)
    secondary_abs = _get_secondary_antibodies(sessions_by_type)

    gene = target.gene_name or target.protein_name
    protein = target.protein_name
    uniprot = target.uniprot_id or '[UniProt ID]'
    num_abs = len(antibodies)

    # Determine which procedures have data
    has_wb = 'WB' in sessions_by_type
    has_ip = 'IP' in sessions_by_type
    has_if = 'IF' in sessions_by_type
    has_fc = 'FC' in sessions_by_type

    procedures_list = []
    if has_wb:
        procedures_list.append('western blot')
    if has_ip:
        procedures_list.append('immunoprecipitation')
    if has_if:
        procedures_list.append('immunofluorescence')
    if has_fc:
        procedures_list.append('flow cytometry')
    procedures_str = ', '.join(procedures_list[:-1])
    if len(procedures_list) > 1:
        procedures_str += f' and {procedures_list[-1]}'
    elif procedures_list:
        procedures_str = procedures_list[0]
    else:
        # Nothing has a reading on it yet. Now that a session with only blank
        # result rows no longer counts as a procedure run, this is reachable —
        # and a sentence reading "characterized 7 antibodies for  using a
        # standardized protocol" is worse than the named gap the rest of the
        # draft uses. Same square-bracket convention as `_recorded`.
        procedures_str = '[no application has a recorded result yet]'

    logger.info(
        "Generating report for %s (%s): %d antibodies, %d cell lines, "
        "procedures: %s",
        protein, gene, num_abs, len(cell_lines), procedures_str
    )

    # ── Create document ───────────────────────────────────────────────
    doc = Document()

    # Set default font
    style = doc.styles['Normal']
    font = style.font
    font.name = FONT_NAME
    font.size = FONT_SIZE_BODY

    # Set narrow margins for more table space (matching journal style)
    for section in doc.sections:
        section.top_margin = Cm(2.0)
        section.bottom_margin = Cm(2.0)
        section.left_margin = Cm(2.0)
        section.right_margin = Cm(2.0)

    # ── Title ─────────────────────────────────────────────────────────
    _add_styled_paragraph(
        doc, 'DATA NOTE',
        font_size=FONT_SIZE_SMALL, bold=True,
        space_after=Pt(4)
    )

    title_text = (
        f"A guide to selecting high-performing antibodies for "
        f"{protein} (Uniprot ID {uniprot}) for use in "
        f"{procedures_str}"
    )
    _add_styled_paragraph(
        doc, title_text,
        font_size=FONT_SIZE_TITLE, bold=True,
        space_after=Pt(12)
    )

    # Author placeholder
    _add_styled_paragraph(
        doc,
        '[Author list to be completed]',
        font_size=FONT_SIZE_BODY, italic=True,
        space_after=Pt(12)
    )

    # ── Abstract ──────────────────────────────────────────────────────
    _add_heading(doc, 'Abstract', level=2)
    abstract = (
        f"{protein} [brief protein description — to be completed]. "
        f"Here we have characterized {num_abs} {protein} commercial "
        f"antibodies for {procedures_str} using a standardized experimental "
        f"protocol based on comparing read-outs in knockout cell lines and "
        f"isogenic parental controls. These studies are part of a larger, "
        f"collaborative initiative seeking to address antibody reproducibility "
        f"issues by characterizing commercially available antibodies for human "
        f"proteins and publishing the results openly as a resource for the "
        f"scientific community. While use of antibodies and protocols vary "
        f"between laboratories, we encourage readers to use this report as a "
        f"guide to select the most appropriate antibodies for their specific "
        f"needs."
    )
    _add_styled_paragraph(doc, abstract, space_after=Pt(12))

    # ── Introduction ──────────────────────────────────────────────────
    _add_heading(doc, 'Introduction', level=2)
    intro_text = (
        report.introduction_text
        if report and report.introduction_text
        else (
            f"[INTRODUCTION PLACEHOLDER — manually written disease context "
            f"paragraph for {protein} ({gene}) to be inserted here. This "
            f"section should describe the protein's function, disease "
            f"associations, and the importance of identifying high-quality "
            f"research reagents.]"
        )
    )
    _add_styled_paragraph(doc, intro_text, space_after=Pt(6))

    # Standard YCharOS initiative paragraph
    initiative_para = (
        "This research is part of a broader collaborative initiative in "
        "which academics, funders and commercial antibody manufacturers are "
        "working together to address antibody reproducibility issues by "
        "characterizing commercial antibodies for human proteins using "
        "standardized protocols, and openly sharing the data. Here we "
        f"evaluated the performance of {num_abs} commercial antibodies for "
        f"{protein} for use in {procedures_str} enabling biochemical and "
        f"cellular assessment of {protein} properties and function. The "
        "platform for antibody characterization used to carry out this study "
        "was endorsed by a committee of industry academic representatives. "
        "It consists of identifying human cell lines with adequate target "
        "protein expression and the development/contribution of equivalent "
        "knockout (KO) cell lines, followed by antibody characterization "
        "procedures using most commercially available antibodies against the "
        "corresponding protein. The standardized consensus antibody "
        "characterization protocols are openly available on Protocol Exchange."
    )
    _add_styled_paragraph(doc, initiative_para, space_after=Pt(6))

    # Disclaimer paragraph
    disclaimer = (
        "The authors do not engage in result analysis or offer explicit "
        "antibody recommendations. A limitation of this study is the use of "
        "universal protocols - any conclusions remain relevant within the "
        "confines of the experimental setup and cell line used in this study."
    )
    _add_styled_paragraph(doc, disclaimer, space_after=Pt(12))

    # ── Results and Discussion ────────────────────────────────────────
    _add_heading(doc, 'Results and discussion', level=2)

    # DepMap / cell line selection paragraph
    wt_lines = [cl for cl in cell_lines if cl.genotype == 'WT']
    ko_lines = [cl for cl in cell_lines if cl.genotype == 'KO']
    wt_name = wt_lines[0].name if wt_lines else '[cell line]'

    # \u2500\u2500 The cut-off sentence has to agree with the number it quotes \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    #
    # It asserted "was identified as a suitable cell line" unconditionally, two
    # clauses after quoting the 2.5 log\u2082(TPM+1) threshold \u2014 so run 11's STMN2
    # draft declared HAP1 suitable while the app's own feasibility page said HAP1
    # expresses STMN2 at 0.03. Both numbers come from the same column; only the
    # document drew a conclusion from it, and it drew the wrong one. A scientist
    # editing the draft at six o'clock would very likely leave the sentence in.
    #
    # So the verdict is derived, and where it cannot be, it is a named gap in the
    # square brackets this document already uses \u2014 the same rule as `_recorded`.
    expression = target.depmap_expression
    depmap_val = _fmt_decimal(expression) if expression else '[X.X]'
    if expression is None:
        verdict = (f"expresses the {protein} transcript at {depmap_val} log\u2082 "
                   f"(TPM+1) \u2014 [confirm this line meets the cut-off] \u2014 and was")
    elif float(expression) >= depmap.EXPRESSION_THRESHOLD:
        verdict = (f"expresses the {protein} transcript at {depmap_val} log\u2082 "
                   f"(TPM+1), was identified as a suitable cell line and was")
    else:
        verdict = (f"expresses the {protein} transcript at {depmap_val} log\u2082 "
                   f"(TPM+1), which is **below** that cut-off \u2014 [explain why this "
                   f"line was used] \u2014 and was")
    results_intro = (
        f"Our standard protocol involves comparing readouts from WT (wild "
        f"type) and KO cells. The first step is to identify a cell line(s) "
        f"that expresses sufficient levels of a given protein to generate a "
        f"measurable signal using antibodies. To this end, we examined the "
        f"DepMap transcriptomics database to identify all cell lines that "
        f"express the target at levels greater than "
        f"{depmap.EXPRESSION_THRESHOLD} log\u2082 (transcripts "
        f"per million \u201cTPM\u201d + 1), which we have found to be a suitable "
        f"cut-off (Cancer Dependency Map Portal, RRID:SCR_017655). "
        f"The {wt_name} cell line {verdict} "
        f"modified with CRISPR/Cas9 to KO the corresponding {gene} "
        f"gene (Table 1)."
    )
    _add_styled_paragraph(doc, results_intro, space_after=Pt(6))

    # Brief results paragraphs per procedure
    if has_wb:
        # The membrane was stated as fact — "transferred onto nitrocellulose
        # membranes" — whether or not anybody had recorded one. Same rule as the
        # Method section: name the gap, do not fill it with something plausible.
        membrane = _recorded(_session_conditions(sessions_by_type.get('WB')),
                             'membrane', gap='membrane')
        wb_text = (
            f"For western blot experiments, WT and {gene} KO protein lysates "
            f"were ran on SDS-PAGE, transferred onto {membrane} membranes, "
            f"and then probed with {num_abs} {protein} antibodies in parallel "
            f"(Table 2, Figure 1)."
        )
        _add_styled_paragraph(doc, wb_text, space_after=Pt(6))

    if has_ip:
        ip_text = (
            f"We then assessed the capability of all {num_abs} antibodies to "
            f"capture {protein} from {wt_name} protein extracts using "
            f"immunoprecipitation techniques, followed by western blot "
            f"analysis (Figure 2)."
        )
        _add_styled_paragraph(doc, ip_text, space_after=Pt(6))

    if has_if:
        if_text = (
            f"For immunofluorescence, antibodies were screened using a mosaic "
            f"strategy. WT and KO cells were labelled with different "
            f"fluorescent dyes, mixed and plated at a 1:1 ratio, and stained "
            f"with the indicated {protein} antibodies (Figure 3)."
        )
        _add_styled_paragraph(doc, if_text, space_after=Pt(6))

    if has_fc:
        fc_text = (
            f"For flow cytometry, WT and KO cells were labelled with distinct "
            f"fluorescent dyes and combined at a 1:1 ratio. Both cell lines "
            f"were fixed, permeabilized and blocked prior to antibody staining "
            f"(Figure 4)."
        )
        _add_styled_paragraph(doc, fc_text, space_after=Pt(6))

    # Conclusion paragraph
    conclusion = (
        f"In conclusion, we have screened {num_abs} {protein} commercial "
        f"antibodies by {procedures_str} by comparing the signal produced by "
        f"the antibodies in human {wt_name} WT and {gene} KO cells."
    )
    _add_styled_paragraph(doc, conclusion, space_after=Pt(12))

    # ── Table 1 — Cell Lines ─────────────────────────────────────────
    _add_styled_paragraph(
        doc,
        'Table 1. Summary of the cell lines used.',
        font_size=FONT_SIZE_BODY, bold=True,
        space_after=Pt(4), space_before=Pt(12)
    )
    table1_headers = [
        'Institution', 'Catalog number', 'RRID (Cellosaurus)',
        'Cell line', 'Genotype'
    ]
    table1_rows = []
    for cl in cell_lines:
        institution = cl.company.name if cl.company else (cl.site.name if cl.site else '-')
        table1_rows.append([
            institution,
            _or_dash(cl.catalogue_number),
            _or_dash(cl.cellosaurus_id),
            cl.name,
            f"{cl.name} {cl.get_genotype_display()}" if cl.genotype == 'KO'
            else cl.get_genotype_display(),
        ])
    # Genotype column: "WT", or "<gene> KO" naming **the gene the line is a
    # knockout of** — not the gene this report is about. Those are the same
    # thing for every row that belongs here, and when they differ the row is a
    # mistake that must not be dressed up as a correct one: this line stamped
    # every knockout with the report's gene, so a PRKN knockout that reached
    # Table 1 through a mis-resolved session was published as a STMN2 knockout.
    for i, cl in enumerate(cell_lines):
        if cl.genotype == 'KO':
            own = getattr(getattr(cl, 'target', None), 'gene_name', '') or gene
            table1_rows[i][4] = f"{own} KO"
        else:
            table1_rows[i][4] = 'WT'

    _build_table(
        doc, table1_headers, table1_rows,
        col_widths_inches=[1.8, 1.2, 1.3, 1.0, 1.0]
    )
    doc.add_paragraph()  # Spacer

    # ── Table 2 — Antibodies ─────────────────────────────────────────
    _add_styled_paragraph(
        doc,
        f'Table 2. Summary of the {protein} antibodies tested.',
        font_size=FONT_SIZE_BODY, bold=True,
        space_after=Pt(4), space_before=Pt(12)
    )

    table2_headers = [
        'Company', 'Catalog\nnumber',
        'Lot number\n(used in Wb,\nIP and IF)',
        'Lot number\n(used in FC)',
        'RRID\n(Antibody\nRegistry)', 'Clonality', 'Clone\nID',
        # \u00b5g/mL, because that is the unit `Antibody.concentration` is stored in.
        # The heading said \u00b5g/\u00b5l \u2014 a thousand-fold mislabel, in the one table
        # that leaves the building.
        'Host', 'Concentration\n(\u00b5g/mL)',
        'Vendors\nrecommended\napplications'
    ]

    # Collect FC lot numbers keyed by (catalogue_number, company_id)
    # FC results come from Leicester with potentially different lots
    fc_lots = {}
    for session in sessions_by_type.get('FC', []):
        for r in session.fc_results.all():
            ab = r.antibody
            key = (
                (ab.catalogue_number or '').strip().upper(),
                ab.company_id,
            )
            if key not in fc_lots and ab.lot_number:
                fc_lots[key] = ab.lot_number

    table2_rows = []
    for ab in antibodies:
        company_name = ab.company.name if ab.company else '-'
        # Look up FC lot for this antibody
        ab_key = (
            (ab.catalogue_number or '').strip().upper(),
            ab.company_id,
        )
        fc_lot = fc_lots.get(ab_key, '-')
        # If this antibody's own lot is the FC lot, still show it in both
        # columns if it was tested in both
        wb_ip_if_lot = _or_dash(ab.lot_number)

        table2_rows.append([
            company_name,
            _cat_number_with_markers(ab),
            wb_ip_if_lot,
            fc_lot,
            _fmt_rrid(ab.rrid),
            _clonality_display(ab),
            _or_dash(ab.clone_id),
            _or_dash(ab.host_species),
            _fmt_decimal(ab.concentration) if ab.concentration else 'n/a',
            _vendor_apps(ab),
        ])

    _build_table(
        doc, table2_headers, table2_rows,
        col_widths_inches=[1.1, 0.7, 0.7, 0.7, 0.7, 0.8, 0.5, 0.5, 0.6, 0.6]
    )

    # Table 2 footnote
    footnote = (
        "Wb=western blot; IF=immunofluorescence; IP=immunoprecipitation; "
        "FC=flow cytometry; n/a=not available.\n"
        "*Monoclonal antibody.\n**Recombinant antibody."
    )
    _add_styled_paragraph(
        doc, footnote,
        font_size=FONT_SIZE_SMALL, italic=True,
        space_after=Pt(12)
    )

    # ── Table 3 — Secondary Antibodies (if data exists) ──────────────
    if secondary_abs:
        _add_styled_paragraph(
            doc,
            'Table 3. Summary of secondary antibodies used.',
            font_size=FONT_SIZE_BODY, bold=True,
            space_after=Pt(4), space_before=Pt(12)
        )

        table3_headers = [
            'Procedure', 'Secondary antibody', 'Dilution',
            'Source', 'Catalog number'
        ]
        # **One row per procedure, not one per secondary.** A western blot whose
        # session named a secondary and whose three result rows each named their
        # own printed as three "Western blot" rows — all true, and read by anybody
        # who has not seen the sessions as three western blots. A procedure is run
        # once here; the secondaries it used are a list inside its row.
        table3_rows = []
        for proc, group in _group_by_procedure(secondary_abs).items():
            proc_display = {
                'WB': 'Western blot',
                'IP': 'Immunoprecipitation',
                'IF': 'Immunofluorescence',
                'FC': 'Flow cytometry',
            }.get(proc, proc)
            # **The lists have to line up positionally.** Collapsing each column
            # independently gave three antibodies against two dilutions — both
            # de-duplicated, both true, and no way for a reader to tell which
            # dilution belonged to which antibody. Dedupe on the *antibody*, then
            # carry that row's own dilution/source/catalogue along, so position N
            # means the same antibody in every column.
            seen, kept = set(), []
            for s in group:
                name = str(s['antibody'] or '').strip()
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                kept.append(s)
            join = lambda key: '; '.join(
                (str(s[key] or '').strip() or '-') for s in kept)
            table3_rows.append([
                proc_display,
                _or_dash('; '.join(str(s['antibody']).strip() for s in kept)),
                _or_dash(join('dilution')),
                _or_dash(join('source')),
                _or_dash(join('catalogue')),
            ])

        _build_table(
            doc, table3_headers, table3_rows,
            col_widths_inches=[1.5, 2.0, 1.0, 1.5, 1.2]
        )
        doc.add_paragraph()  # Spacer

    # ── Figure 1 — Western Blot ──────────────────────────────────────
    if has_wb:
        doc.add_page_break()
        _add_figure_placeholder(
            doc, 1,
            _wb_legend(target, sessions_by_type, cell_lines, antibodies)
        )

    # ── Figure 2 — Immunoprecipitation ───────────────────────────────
    if has_ip:
        doc.add_page_break()
        _add_figure_placeholder(
            doc, 2,
            _ip_legend(target, sessions_by_type, cell_lines)
        )

    # ── Figure 3 — Immunofluorescence ────────────────────────────────
    if has_if:
        doc.add_page_break()
        _add_figure_placeholder(
            doc, 3,
            _if_legend(target, sessions_by_type, cell_lines)
        )

    # ── Figure 4 — Flow Cytometry ────────────────────────────────────
    if has_fc:
        doc.add_page_break()
        _add_figure_placeholder(
            doc, 4,
            _fc_legend(target, sessions_by_type, cell_lines)
        )

    # ── Methods ───────────────────────────────────────────────────────
    doc.add_page_break()
    _add_heading(doc, 'Method', level=2)

    # Standard preamble
    methods_preamble = (
        "The standardized protocols used to carry out this KO cell line-based "
        "antibody characterization platform was established and approved by a "
        "collaborative group of academics, industry researchers and antibody "
        "manufacturers. The detailed materials and step-by-step protocols used "
        "to characterize antibodies in western blot, immunoprecipitation and "
        "immunofluorescence are openly available on Protocol Exchange "
        "(DOI: 10.21203/rs.3.pex-2607/v1)."
    )
    _add_styled_paragraph(doc, methods_preamble, space_after=Pt(8))

    # Antibodies and cell line used
    _add_heading(doc, 'Antibodies and cell line used', level=3)
    ab_cl_text = (
        f"Cell lines used and primary antibodies tested in this study are "
        f"listed in Tables 1 and 2, respectively. To ensure that the cell "
        f"lines and antibodies are cited properly and can be easily "
        f"identified, we have included their corresponding Research Resource "
        f"Identifiers, or RRID."
    )
    _add_styled_paragraph(doc, ab_cl_text, space_after=Pt(8))

    # WB methods
    if has_wb:
        _add_heading(doc, 'Antibody screening by western blot', level=3)
        wb_methods = _build_wb_methods(
            target, sessions_by_type['WB'], cell_lines, antibodies
        )
        if wb_methods:
            _add_styled_paragraph(doc, wb_methods, space_after=Pt(8))

    # IP methods
    if has_ip:
        _add_heading(doc, 'Antibody screening by immunoprecipitation', level=3)
        ip_methods = _build_ip_methods(target, sessions_by_type['IP'], cell_lines)
        if ip_methods:
            _add_styled_paragraph(doc, ip_methods, space_after=Pt(8))

    # IF methods
    if has_if:
        _add_heading(doc, 'Antibody screening by immunofluorescence', level=3)
        if_methods = _build_if_methods(
            target, sessions_by_type['IF'], cell_lines
        )
        if if_methods:
            _add_styled_paragraph(doc, if_methods, space_after=Pt(8))

    # FC methods (full text in report body, per SYT1 convention)
    if has_fc:
        _add_heading(doc, 'Antibody screening by flow cytometry', level=3)
        fc_methods = _build_fc_methods(target, sessions_by_type['FC'], cell_lines)
        if fc_methods:
            _add_styled_paragraph(doc, fc_methods, space_after=Pt(8))

    # ── Data Availability ─────────────────────────────────────────────
    doc.add_page_break()
    _add_heading(doc, 'Data availability', level=2)
    _add_styled_paragraph(doc, 'Underlying data', bold=True, space_after=Pt(4))

    zenodo_text = (
        f"Zenodo: Dataset for the {protein} antibody screening study "
        f"[DOI to be assigned upon deposit]."
    )
    if report and report.zenodo_doi:
        zenodo_text = (
            f"Zenodo: Dataset for the {protein} antibody screening study, "
            f"{report.zenodo_doi}."
        )
    _add_styled_paragraph(doc, zenodo_text, space_after=Pt(4))
    _add_styled_paragraph(
        doc,
        "Data are available under the terms of the Creative Commons "
        "Attribution 4.0 International license (CC-BY 4.0).",
        space_after=Pt(12)
    )

    # ── Acknowledgments ───────────────────────────────────────────────
    _add_heading(doc, 'Acknowledgment', level=2)
    ack_text = (
        "We would like to thank the NeuroSGC/YCharOS/EDDU collaborative "
        "group for their important contribution to the creation of an open "
        "scientific ecosystem of antibody manufacturers and KO cell line "
        "suppliers, for the development of community-agreed protocols, and "
        "for their shared ideas, resources, and collaboration."
    )
    _add_styled_paragraph(doc, ack_text, space_after=Pt(6))

    sgc_text = (
        "Thank you to the Structural Genomics Consortium, a registered "
        "charity (no. 1097737), for your support on this project."
    )
    _add_styled_paragraph(doc, sgc_text, space_after=Pt(12))

    # ── Save ──────────────────────────────────────────────────────────
    if output_path is None:
        safe_gene = gene.replace('/', '_').replace('\\', '_').replace(' ', '_')
        output_path = os.path.join(
            '/tmp',
            f"{safe_gene}_antibody_characterization_report.docx"
        )

    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    doc.save(output_path)
    logger.info("Report saved to %s", output_path)

    # Update Report record if it exists.
    #
    # Only ever forwards. `status` is a lifecycle — draft, generated, submitted,
    # published — and two curated paths set it to published: a DOI typed on the
    # target board, and Carl's workbook. Stamping 'generated' unconditionally
    # meant that pressing Generate Report on an already-published target quietly
    # demoted its publication record, from a button that reads as read-only and
    # gives no sign it wrote anything at all.
    if report:
        from django.utils import timezone
        from pipeline.models import Report
        fields = ['generated_at']
        report.generated_at = timezone.now()
        if report.status == Report.ReportStatus.DRAFT:
            report.status = Report.ReportStatus.GENERATED
            fields.append('status')
        report.save(using=DB_ALIAS, update_fields=fields)
        logger.info("Report record %d re-generated (status %s)",
                    report.pk, report.status)

    return output_path
