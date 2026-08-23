"""
Per-gene session template (download side of the dedicated sessions tool).

Given a gene, build one Excel workbook with **a tab per application** (WB, IP,
IF, FC). Each tab is *one shared session* for that gene: the antibodies and the
WT/KO cell lines already in the database are listed as result rows, the session
conditions are pre-filled from the member's **site default protocol**
(``ProtocolTemplate``), and the result columns are left blank to complete. Fill
it in and re-upload (upload/write-back is a later phase).

Download-only, members-only. Reads ``pipeline_db`` and never writes.

Layout of every tab (left → right):
  * key/context columns — a shared ``session_ref`` per tab, gene, the antibody
    (catalogue), its company, and the WT/KO cell-line names, pre-filled;
  * condition columns — one per key in the site default protocol's ``conditions``
    dict, pre-filled with the protocol value (identical on every row);
  * result columns — the real ``*Result`` model fields, left blank.
"""
from __future__ import annotations

import io

from pipeline.models import Target, ProtocolTemplate
from pipeline.services.cropper import db as cdb
# How a protocol condition is spelled as a column, so it can never collide with
# a result field of the same name. Taken from the sessions board rather than
# spelled again here: its results panel already edits conditions as ``cond:<key>``
# cells over the ``session_conditions`` JSON, and one convention that two files
# agree on by accident is one that drifts.
from pipeline.services.session_board import CONDITION_PREFIX

DB = "pipeline_db"

PROCEDURES = ["WB", "IP", "IF", "FC"]

# What `session_ref` says about the tab it heads, and the only thing that decides
# whether an upload **creates** a session or **fills one in**.
#
# The workbook could only ever create. Planning a session through the
# step-by-step form and then downloading the workbook from the same page looked
# like one task in two halves; it was two, and it left a Planned session with no
# results beside the Complete one the upload made. Meanwhile a session's own
# bench sheet could only ever update. Neither could do the other's job, and
# nothing on either screen said which door you were standing in.
#
# So the ref carries the answer: `(new)` creates, `#480` fills in session 480.
# It is human-readable on purpose — it is the thing a person checks before
# uploading a file they filled in three days ago.
SESSION_REF_NEW = "(new)"


def session_ref(proc, gene, session=None) -> str:
    """The marker repeated down a tab — all its rows are one session."""
    stem = f"{proc} · {gene}"
    return f"{stem} #{session.pk}" if session is not None else f"{stem} {SESSION_REF_NEW}"

# Shared context columns (pre-filled from the DB). ``session_ref`` is the same
# marker on every row of a tab — it signals "these rows are one session".
_CONTEXT_COLS = ["session_ref", "gene", "antibody", "company",
                 "cell_line_wt", "cell_line_ko", "experimenter", "date"]

# Result columns per procedure — the real fields on WbResult / IpResult /
# IfResult / FcResult, left blank for the bench scientist to complete.
_RESULT_COLS = {
    "WB": ["dilution", "secondary_ab", "secondary_ab_dilution",
           "exposure_time", "signal", "rating", "gel", "membrane", "ecl",
           "detection_system", "comments"],
    "IP": ["enrichment", "amount_of_antibody", "amount_of_lysate", "bead_type",
           "lysis_buffer", "detection_ab", "detection_ab_dilution", "secondary_ab",
           "secondary_ab_dilution", "gel", "membrane", "ecl", "detection_system",
           "sm_assessment", "ub_assessment", "ip_assessment", "exposure_time",
           "ecl_type", "comments"],
    "IF": ["specific_signal", "wt_ko_ratio_1", "wt_ko_ratio_2", "concentration_1",
           "concentration_2", "best_concentration", "fixative", "blocking",
           "permeabilisation", "primary_ab_dilution", "dilution_buffer",
           "primary_ab_condition", "secondary_ab", "secondary_ab_condition",
           "plate_number", "well_number", "image_acquired_by", "image_analysed_by",
           "microscope", "objective", "comments"],
    "FC": ["concentration", "histogram_shift", "median_fluorescence_wt",
           "median_fluorescence_ko", "gating_strategy", "comments"],
}

# A column this sheet used to ship, and the field it now writes to.
#
# **One field has one column name** (CLAUDE.md), and WB broke it twice over:
# `WbResult.dilution` and `WbResult.primary_ab_dilution` are the same
# measurement — the Access import wrote `1AbDilution` into both — and the
# workbook offered a column for each, so one blot could come back with two
# dilutions and `report_generator` would print whichever `dilution or
# primary_ab_dilution` picked. The board drew both too, one in the readings and
# one under Method, with nothing to say how they differ.
#
# Dropping the column outright is the trap this alias exists to avoid: a header
# the sheet no longer knows is read as a **session condition**
# (`session_import._condition_cols`), so every workbook already on somebody's
# disk would have filed its dilution as protocol metadata, one value for the
# whole tab, and said nothing about it. Reading it as the field it always meant
# costs one lookup and keeps those sheets working.
RESULT_COL_ALIASES = {"WB": {"primary_ab_dilution": "dilution"}}


def aliases_for(proc, col) -> list:
    """Older column names that now write to ``col``."""
    return [old for old, new in RESULT_COL_ALIASES.get(proc, {}).items() if new == col]


def result_value(obj, proc, col) -> str:
    """What a sheet pre-fills ``col`` with, falling back to the columns it replaced.

    Without the fallback a row recorded before the merge — `primary_ab_dilution`
    set, `dilution` blank — downloads with an empty dilution cell, which reads as
    nobody having written the dilution down.
    """
    for name in [col, *aliases_for(proc, col)]:
        value = getattr(obj, name, "") if obj is not None else ""
        if value not in (None, ""):
            return value
    return ""


def resolve_target(gene: str):
    """Case-insensitive gene → Target, or None."""
    g = (gene or "").strip()
    if not g:
        return None
    return (Target.objects.using(DB)
            .filter(gene_name__iexact=g)
            .prefetch_related("antibodies__company", "cell_lines__parent_line")
            .first())


def _default_template(site_id, proc):
    """The site's default ProtocolTemplate for a procedure, with graceful
    fallbacks: site default → any site template → any default → any template."""
    qs = ProtocolTemplate.objects.using(DB).filter(procedure_type=proc)
    t = None
    if site_id:
        t = qs.filter(site_id=site_id, is_default=True).first() or qs.filter(site_id=site_id).first()
    if t is None:
        t = qs.filter(is_default=True).first() or qs.first()
    return t


def _company_name(ab):
    c = ab.company
    return cdb.company_label(c) if c else ""


def _first_genotype(cell_lines, genotype):
    return next((cl for cl in cell_lines
                 if (cl.genotype or "").strip().upper() == genotype), None)


def _line_cell(line):
    """How a cell line is written into the sheet: the **full label**, site and
    all, not the bare name.

    A bare `SH-SY5Y` is shared by five rows and a bare `HAP1` by hundreds, so a
    name on its own is not an identity — the quick New-session panel resolved
    one arbitrarily and attached another institution's knockout as a wild-type
    control. `services/cell_lines.py` reads either spelling back, so an older
    sheet carrying bare names still imports; what the label buys is that a sheet
    written *today* cannot be ambiguous tomorrow.
    """
    from pipeline.services import cell_lines as clines
    return clines.label(line) if line is not None else ""


def _tab_plan(target, proc, site_id):
    """(columns, rows) for one procedure tab. Rows: one per antibody, sharing a
    session; conditions pre-filled from the site default protocol."""
    tmpl = _default_template(site_id, proc)
    conditions = dict(tmpl.conditions) if (tmpl and isinstance(tmpl.conditions, dict)) else {}
    # Prefixed, because a protocol condition is very often named after the result
    # field it describes — IP's `bead_type`, `lysis_buffer`, `gel`, `membrane`;
    # IF's `blocking`, `permeabilisation`. Unprefixed they appeared twice in one
    # header row, and `session_import.parse_template` builds {header: cell} per
    # row, so the rightmost copy won and the pre-filled protocol value was lost on
    # the way back in. `session_io.SESSION_PREFIX` exists for the same reason on
    # the sessions sheet, and the sessions board already edits conditions as
    # `cond:<key>` — one convention, both directions.
    cond_cols = [f"{CONDITION_PREFIX}{k}" for k in sorted(conditions.keys())]

    columns = _CONTEXT_COLS + cond_cols + _RESULT_COLS[proc]

    cell_lines = list(target.cell_lines.all())
    ko = _first_genotype(cell_lines, "KO")
    # A wild type is recorded once, with no gene, so it is never in
    # `target.cell_lines` and looking for one there could only ever come back
    # empty — `cell_line_wt` was blank on every row of every tab. The parent of
    # the knockout is the WT this gene was tested against.
    wt = _first_genotype(cell_lines, "WT") or (ko.parent_line if ko else None)
    gene_name = target.gene_name or target.protein_name or ""
    ref = session_ref(proc, gene_name)

    rows = []
    for ab in target.antibodies.all():
        row = {
            "session_ref": ref,
            "gene": gene_name,
            "antibody": ab.catalogue_number or "",
            "company": _company_name(ab),
            "cell_line_wt": _line_cell(wt),
            "cell_line_ko": _line_cell(ko),
            "experimenter": "",
            "date": "",
        }
        for k in cond_cols:
            row[k] = conditions.get(k[len(CONDITION_PREFIX):], "")
        for k in _RESULT_COLS[proc]:
            row.setdefault(k, "")
        rows.append([row.get(c, "") for c in columns])

    return columns, rows, (tmpl.name if tmpl else None)


def _session_tab_plan(session):
    """(columns, rows) for a workbook built **for one existing session**.

    Same shape as a gene tab — same context columns, the same ``cond:`` columns,
    the same model-derived result columns — so one parser reads both. What
    differs is where the values come from and what the ref says:

    * conditions are the session's own ``session_conditions``, with the site
      protocol's keys offered blank alongside, so a sheet can *add* a condition
      that was never recorded;
    * rows are the session's own result rows, **pre-filled with what is already
      recorded**, which is what makes this a round trip rather than a second
      blank form. A session with no result rows yet falls back to the gene's
      vials as a picking list — the same fallback the bench sheet uses, and the
      thing that stops a session planned through the step-by-step form being a
      dead end;
    * ``session_ref`` carries ``#<id>``, so the importer fills this session in
      instead of creating a second one beside it.
    """
    from pipeline.services import planning
    from pipeline.services import sessions as sess

    proc = session.procedure_type
    target = session.target
    gene_name = target.gene_name or target.protein_name or ""

    stored = dict(session.session_conditions or {})
    tmpl = _default_template(session.site_id, proc)
    offered = dict(tmpl.conditions) if (tmpl and isinstance(tmpl.conditions, dict)) else {}
    # The session's own values win; the protocol's keys are offered blank so a
    # condition nobody recorded still has a column to be written in.
    keys = sorted(set(stored) | set(offered))
    cond_cols = [f"{CONDITION_PREFIX}{k}" for k in keys]

    columns = _CONTEXT_COLS + cond_cols + _RESULT_COLS[proc]

    model = sess.RESULT_MODEL_MAP.get(proc)
    recorded = {}
    if model is not None:
        for r in (model.objects.using(DB)
                  .filter(session_id=session.pk, antibody__isnull=False)
                  .select_related("antibody", "antibody__company")):
            recorded.setdefault(r.antibody_id, r)

    ref = session_ref(proc, gene_name, session=session)
    wt_cell = _line_cell(session.cell_line_wt)
    ko_cell = _line_cell(session.cell_line_ko)
    # `str()`, not `.isoformat()`: a `date` stringifies to ISO anyway, and an
    # in-memory session can be holding a string — `session_board._set_session_field`
    # assigns the raw cell value and lets the model coerce it on save.
    when = str(session.date) if session.date else ""
    who = str(session.experimenter) if session.experimenter_id else ""

    rows = []
    for ab in planning._session_antibodies(session):
        existing = recorded.get(ab.pk)
        row = {
            "session_ref": ref,
            "gene": gene_name,
            "antibody": ab.catalogue_number or "",
            "company": _company_name(ab),
            "cell_line_wt": wt_cell,
            "cell_line_ko": ko_cell,
            "experimenter": who,
            "date": when,
        }
        for k, col in zip(keys, cond_cols):
            row[col] = stored.get(k, offered.get(k, "")) or ""
        for f in _RESULT_COLS[proc]:
            val = result_value(existing, proc, f)
            row[f] = "" if val is None else str(val)
        rows.append([row.get(c, "") for c in columns])

    return columns, rows


_GENE_README = [
    ("One tab per application", "WB, IP, IF and FC each hold ONE shared session for this gene."),
    ("session_ref", "The same marker repeats down a tab — all its rows are one session. "
                    f"It ends {SESSION_REF_NEW}, which means uploading this file CREATES sessions."),
    ("Pre-filled cells", "Antibodies, WT/KO cell lines and the site default protocol conditions are filled in."),
    ("Blank result columns", "Complete the blank columns with your results, then upload the file "
                             "from this gene's page or the sessions board."),
    (f"{CONDITION_PREFIX}… columns", "Protocol conditions. Prefixed so they never collide with a "
                                     "result column of the same name — change one and the reading "
                                     "beside it is untouched."),
    ("Tabs you leave alone", "A tab with no results written on it is not recorded. Fill in the one "
                             "you ran; the other three are ignored."),
    # Still true of THIS file, and the tab is the only guidance somebody has at a
    # bench: promising an update would send them to upload a corrected sheet
    # expecting it to replace a reading, and get a second session instead.
    ("Only ever adds", "Uploading creates new sessions — it never edits or deletes what is "
                       "already recorded. Upload the same sheet twice and the work is "
                       "recorded twice."),
    ("To fill in a session you already planned",
     "Download the workbook from that session instead — on the sessions board, open the row "
     "and use its own workbook button. Its session_ref carries the session number, and "
     "uploading it fills that session in rather than making a second one."),
]


def _session_readme(session, gene_name):
    return [
        ("This workbook is session "
         f"#{session.pk}", f"{session.get_procedure_type_display()} · {gene_name} · "
                           f"{session.date or 'no date'} · {session.get_status_display()}."),
        ("session_ref", f"Every row reads '{session_ref(session.procedure_type, gene_name, session=session)}'. "
                        "The number is what tells the upload to fill THIS session in. "
                        "Do not edit it — a number that is not on file is refused, and the "
                        "rows are not recorded somewhere else instead."),
        ("Already recorded", "Results already on file are filled in. Change what you need to; "
                             "a cell you leave blank keeps whatever is stored."),
        ("Rows", "One row per antibody in this session. If the session has no results yet, "
                 "these are the gene's vials to choose from — fill in the ones you ran and "
                 "leave the rest blank. A blank row is not an experiment."),
        (f"{CONDITION_PREFIX}… columns", "Protocol conditions for the session. Prefixed so they never "
                                         "collide with a result column of the same name."),
        ("Uploading", "Open this session on the sessions board and use its upload button. "
                      "It updates this session — it does not create a second one."),
    ]


def _write_workbook(readme, tabs) -> bytes:
    """One writer for both modes, so a gene tab and a session tab cannot drift
    into two spreadsheet dialects that one parser has to guess between."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # A short read-me tab first, so the rule that decides create-versus-fill-in
    # is on the tin rather than in the app that produced the file.
    info = wb.create_sheet("How to use")
    info.append(["Field", "Meaning"])
    for cell in info[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="064C83")
    for k, v in readme:
        info.append([k, v])
    info.column_dimensions["A"].width = 30
    info.column_dimensions["B"].width = 92

    for name, cols, rows in tabs:
        ws = wb.create_sheet(name)
        ws.append(cols)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="064C83")
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        for i in range(1, len(cols) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_gene_template(gene: str, site_id=None) -> bytes:
    """Build the multi-tab .xlsx for a gene. Raises ValueError if the gene isn't
    a target. Tabs with no antibodies still carry their header row.

    Every tab is stamped ``(new)``: this file captures a week nobody has recorded
    yet. To fill in a session that already exists, use `build_session_template`.
    """
    target = resolve_target(gene)
    if target is None:
        raise ValueError(f"'{gene}' is not a target in the pipeline database.")

    tabs = []
    for proc in PROCEDURES:
        cols, rows, _tmpl_name = _tab_plan(target, proc, site_id)
        tabs.append((proc, cols, rows))
    return _write_workbook(_GENE_README, tabs)


def build_session_template(session) -> bytes:
    """The same workbook, for **one session that already exists** — one tab,
    pre-filled with what is recorded, stamped with the session's number.

    This is the half that was missing. A session planned through the
    step-by-step form had no way to be filled in from a spreadsheet: the
    workbook only created, and the bench sheet's importer could not carry
    conditions. Downloading from the session bakes its id into every row, so the
    file knows where it goes and a person does not have to remember.
    """
    gene_name = (session.target.gene_name or session.target.protein_name or "")
    cols, rows = _session_tab_plan(session)
    return _write_workbook(_session_readme(session, gene_name),
                           [(session.procedure_type, cols, rows)])
