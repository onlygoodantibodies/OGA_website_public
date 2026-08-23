"""
Whole-dataset download / upload (the internal "Downloads & uploads" hub).

**Internal, pipeline-members only** — NOT public. This is the file round-trip the
owner chose over a write MCP: download the *entire* pipeline scientific dataset
(all antibody-related data, **including unpublished**; academy/auth data is never
touched because everything here lives on ``pipeline_db``), edit it — optionally
with an LLM's help, e.g. fill missing F1000/Zenodo report DOIs and antibody
purchasing URLs across many genes — and upload the filled file in one go.

Design guarantees (mirrors the paste/bulk tools, hardened for a whole-file upsert):
  * **Upsert only** — insert new rows + update fields. There is NO row deletion,
    NO table replace: rows absent from the file are never visited, so they can't
    be removed.
  * **A blank cell never clears a field** — an empty incoming value is skipped
    before any assignment (the ``if val`` guard in ``_apply_metadata`` and the
    report writer), in both fill and overwrite modes.
  * **Gap-fill (empty → value) applies freely.** **Overwriting a populated value
    requires explicit confirmation** — ``plan_upload`` surfaces the exact field
    diffs and ``apply_upload`` only writes them when ``apply_overwrites=True``.
  * **Empty / malformed files are refused** structurally (no recognised sheet,
    zero data rows, or a missing key column → hard stop, nothing written).
  * **Deletion stays owner-only** (admin / Claude Code) — never via upload.

Reuses: ``search.py`` export columns/styling, ``cropper.metadata`` header parser,
``cropper.commit._apply_metadata`` (fill-only-blank/overwrite write engine),
``cropper.db`` (dedup-safe company + antibody matching), ``services.targets``.
"""
from __future__ import annotations

import io
import json
from decimal import Decimal, InvalidOperation

from django.db import transaction

from pipeline.models import (Target, Antibody, CellLine, Report, Company,
                             TargetAssignment, ReagentRequest, CellCultureEvent,
                             InventoryLocation)
from pipeline.services import concentration as concentration_svc
from pipeline.services import identity
from pipeline.services.cropper import db as cdb
from pipeline.services.cropper import metadata as meta
from pipeline.services.cropper.commit import _apply_metadata
from pipeline import rrid_utils

DB = "pipeline_db"

# Sheet titles (Excel) / top-level keys (JSON). Also the routing keys on upload.
SHEET_TARGETS = "Targets"
SHEET_ANTIBODIES = "Antibodies"
SHEET_CELL_LINES = "Cell lines"
SHEET_REPORTS = "Reports"
SHEET_ASSIGNMENTS = "Assignments"
SHEET_REAGENT_REQUESTS = "Reagent requests"
SHEET_CULTURE_EVENTS = "Cell culture events"
SHEET_INVENTORY = "Inventory"
SHEET_LEGEND = "Legend"

# Column layouts. The first column is always the DB ``id`` — the stable key the
# upload matches on (edit anything else and the row still lands on the right
# record). Non-id columns match the importer aliases so the file round-trips.
# `supplier recommendations`, not `applications` — one name for that column on
# every sheet the app hands out (`cropper/metadata.py::HEADER_ALIASES`, which
# still reads the old spelling so a sheet downloaded before this keeps working).
ANTIBODY_COLS = ["id", "gene", "catalogue", "company", "rrid", "host",
                 "clonality", "clone", "lot", "concentration", "product url",
                 "supplier recommendations"]
_ANTIBODY_APPS = [("supplier_validated_wb", "WB"), ("supplier_validated_ip", "IP"),
                  ("supplier_validated_if", "IF"), ("supplier_validated_fc", "FC")]

CELL_LINE_COLS = ["id", "c number", "name", "gene", "genotype", "parent",
                  "cellosaurus", "supplier", "catalogue", "lot", "medium", "clone"]

REPORT_COLS = ["id", "gene", "status", "zenodo doi", "f1000 doi", "zenodo date", "f1000 date"]

TARGET_COLS = ["id", "gene", "protein", "uniprot", "status", "aliases"]

# The editable field map for the Report round-trip: header alias → model field.
# (Dates are export-only reference for now; DOIs + status are the gap-fill point.)
_REPORT_FIELDS = [
    ("status", "status", "status"),
    ("zenodo doi", "zenodo_doi", "Zenodo DOI"),
    ("f1000 doi", "f1000_doi", "F1000 DOI"),
]

LEGEND_ROWS = [
    ("What this is", "The whole pipeline scientific dataset (incl. unpublished). "
     "Edit and re-upload to gap-fill / update. Members only; never public."),
    ("id column", "The database id. DO NOT EDIT — it is how the upload finds the "
     "right record. Leave blank on a brand-new row to create it."),
    ("Blank cells", "A blank cell NEVER clears a field. Leave a cell blank to keep "
     "the current value."),
    ("Gap-fill", "Filling an empty field (empty → value) applies straight away."),
    ("Overwrite", "Changing an already-populated value needs explicit confirmation "
     "— the upload preview lists every such change for you to approve."),
    ("Never deletes", "Upload is insert + update only. Removing a row from the file "
     "does NOT delete it. Deletion is owner-only, outside this tool."),
    ("catalogue / c number", "Key fields — used to match new rows. Editing a key on "
     "an existing (id'd) row is ignored, never applied, to avoid silent key changes."),
    ("supplier recommendations", "What the SUPPLIER says the antibody is for "
     "(WB/IP/IF/FC) — not OGA's verdict. Additive: listing an app sets its flag; "
     "it never clears an existing one."),
    ("Reports", "F1000 / Zenodo DOIs live on the Reports sheet (one per gene's "
     "report). Fill the blank DOI cells to publish links across many genes at once."),
    ("Cell lines / Targets / Samples", "Editable by id: fill or correct fields on "
     "rows that already exist (the same gap-fill / overwrite-confirm rules). "
     "*Creating* new cell lines, genes or samples stays on their dedicated pages."),
    ("Assignments / Requests / Culture events", "Also editable by id (same rules). "
     "People columns (who assigned / requested / performed) and site/company/project "
     "are export-only reference — never written back, so no individual's data round-trips."),
    ("Inventory", "A flat sheet (not nested under a gene): one row per physical "
     "storage location. 'attached_to' + 'parent id' + 'gene' say what it holds — "
     "export-only reference. Editable by id: storage_type and the building → position "
     "location fields (same gap-fill / overwrite-confirm rules)."),
]


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def resolve_targets(scope: str = "all", target_id=None, genes: str = ""):
    """Return the Target queryset for a download scope.

    scope='all' (default) → every target; target_id=<pk> → one target;
    genes='SNCA,MAPT' → those genes (case-insensitive). Ordered by gene for a
    stable file."""
    qs = Target.objects.using(DB).all()
    if target_id:
        qs = qs.filter(pk=target_id)
    elif (genes or "").strip():
        names = [g.strip() for g in genes.split(",") if g.strip()]
        if names:
            # Case-insensitive match so 'kif5a' finds the stored 'Kif5a'
            # (Django has no __iin, so OR a gene_name__iexact per name).
            from functools import reduce
            from operator import or_
            from django.db.models import Q
            qs = qs.filter(reduce(or_, (Q(gene_name__iexact=n) for n in names)))
    return qs.order_by("gene_name", "protein_name")


# ---------------------------------------------------------------------------
# Row serialisation (shared by Excel + JSON)
# ---------------------------------------------------------------------------

def _gene_of(t) -> str:
    return t.gene_name or t.protein_name or ""


def _antibody_row(ab, gene):
    apps = ", ".join(lbl for f, lbl in _ANTIBODY_APPS if getattr(ab, f))
    return [
        ab.pk, gene, ab.catalogue_number or "",
        cdb.company_label(ab.company) if ab.company else "",
        ab.rrid or "", ab.host_species or "", ab.clonality or "",
        ab.clone_id or "", ab.lot_number or "",
        str(ab.concentration) if ab.concentration is not None else "",
        ab.supplier_url or "", apps,
    ]


def _cell_line_row(cl, gene):
    parent = (cl.parent_line.name if cl.parent_line_id else "") or cl.parental_line_name or ""
    return [
        cl.pk, cl.c_number or "", cl.name or "", gene, cl.genotype or "", parent,
        cl.cellosaurus_id or "", cdb.company_label(cl.company) if cl.company else "",
        cl.catalogue_number or "", cl.lot_number or "", cl.medium or "", cl.clone or "",
    ]


def _report_row(rpt, gene):
    return [
        rpt.pk, gene, rpt.status or "", rpt.zenodo_doi or "", rpt.f1000_doi or "",
        rpt.zenodo_date.isoformat() if rpt.zenodo_date else "",
        rpt.f1000_date.isoformat() if rpt.f1000_date else "",
    ]


def _target_row(t):
    return [t.pk, _gene_of(t), t.protein_name or "", t.uniprot_id or "",
            t.status or "", t.aliases or ""]


def _iter_dataset(targets):
    """Yield (targets, antibodies, cell_lines, reports) rows for the queryset,
    each as (header-aligned) lists, plus target objects for JSON nesting."""
    targets = (targets
               .prefetch_related("antibodies__company", "cell_lines__company",
                                 "cell_lines__parent_line", "reports"))
    trows, arows, crows, rrows = [], [], [], []
    nested = []
    for t in targets:
        gene = _gene_of(t)
        trows.append(_target_row(t))
        abs_ = [_antibody_row(ab, gene) for ab in t.antibodies.all()]
        cls_ = [_cell_line_row(cl, gene) for cl in t.cell_lines.all()]
        rps_ = [_report_row(r, gene) for r in t.reports.all()]
        arows += abs_; crows += cls_; rrows += rps_
        nested.append((t, gene, abs_, cls_, rps_))
    return trows, arows, crows, rrows, nested


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

def build_workbook(targets, selection=None) -> bytes:
    """A multi-sheet .xlsx: Legend, Targets, Antibodies, Cell lines, Reports.

    ``selection`` (see ``parse_selection``) picks which families/fields to
    include; ``None`` reproduces the historical default export exactly."""
    if selection is not None:
        return _build_workbook_selected(targets, selection)

    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    trows, arows, crows, rrows, _ = _iter_dataset(targets)
    wb = openpyxl.Workbook()

    # Legend sheet first.
    ws = wb.active
    ws.title = SHEET_LEGEND
    ws.append(["Field", "Meaning"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="064C83")
    for k, v in LEGEND_ROWS:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 90

    def _sheet(title, cols, rows):
        s = wb.create_sheet(title[:31])
        s.append(cols)
        for c in s[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="064C83")
        for r in rows:
            s.append(r)
        s.freeze_panes = "A2"
        for i in range(1, len(cols) + 1):
            s.column_dimensions[get_column_letter(i)].width = 16
        return s

    _sheet(SHEET_TARGETS, TARGET_COLS, trows)
    _sheet(SHEET_ANTIBODIES, ANTIBODY_COLS, arows)
    _sheet(SHEET_CELL_LINES, CELL_LINE_COLS, crows)
    _sheet(SHEET_REPORTS, REPORT_COLS, rrows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_json(targets, selection=None) -> dict:
    """The whole dataset as a Target tree with stable keys + an embedded legend.

    ``selection`` (see ``parse_selection``) picks which families/fields to
    include; ``None`` reproduces the historical default export exactly."""
    if selection is not None:
        return _build_json_selected(targets, selection)

    _, _, _, _, nested = _iter_dataset(targets)

    def _obj(cols, row):
        return dict(zip(cols, row))

    out_targets = []
    for t, gene, abs_, cls_, rps_ in nested:
        out_targets.append({
            **_obj(TARGET_COLS, _target_row(t)),
            "antibodies": [_obj(ANTIBODY_COLS, r) for r in abs_],
            "cell_lines": [_obj(CELL_LINE_COLS, r) for r in cls_],
            "reports": [_obj(REPORT_COLS, r) for r in rps_],
        })
    return {
        "_legend": {k: v for k, v in LEGEND_ROWS},
        "_columns": {"targets": TARGET_COLS, "antibodies": ANTIBODY_COLS,
                     "cell_lines": CELL_LINE_COLS, "reports": REPORT_COLS},
        "targets": out_targets,
    }


# ---------------------------------------------------------------------------
# Upload — parse
# ---------------------------------------------------------------------------

def _read_sheets(f) -> dict:
    """Read an uploaded .xlsx into {sheet_title: [row_cells,...]} (header + data),
    or a single-sheet .csv/.tsv into {SHEET_ANTIBODIES-or-detected: rows}. Returns
    only sheets we recognise. Raises ValueError on an unreadable file."""
    name = (f.name or "").lower()
    try:
        f.seek(0)
    except Exception:
        pass
    sheets = {}
    if name.endswith((".csv", ".tsv", ".txt")):
        import csv
        raw = f.read().decode("utf-8-sig", errors="replace")
        delim = "\t" if name.endswith(".tsv") else ","
        rows = [list(r) for r in csv.reader(io.StringIO(raw), delimiter=delim)]
        title = _detect_single_sheet(rows)
        if title:
            sheets[title] = rows
        return sheets
    import openpyxl
    wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
    known = {SHEET_TARGETS, SHEET_ANTIBODIES, SHEET_CELL_LINES, SHEET_REPORTS, "Samples",
             SHEET_ASSIGNMENTS, SHEET_REAGENT_REQUESTS, SHEET_CULTURE_EVENTS, SHEET_INVENTORY}
    for ws in wb.worksheets:
        if ws.title in known:
            sheets[ws.title] = [list(r) for r in ws.iter_rows(values_only=True)]
    if not sheets:
        # A single-sheet file whose tab isn't one of ours — detect by header.
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        title = _detect_single_sheet(rows)
        if title:
            sheets[title] = rows
    return sheets


def _detect_single_sheet(rows):
    """Guess which entity a headerless-named sheet holds, from its header row."""
    header = [meta._norm_header(str(c)) for c in (rows[0] if rows else []) if c is not None]
    hset = set(header)
    if {"catalogue", "gene"} & hset and "zenodo doi" not in hset:
        if "genotype" in hset or "cellosaurus" in hset:
            return SHEET_CELL_LINES
        return SHEET_ANTIBODIES
    if "zenodo doi" in hset or "f1000 doi" in hset:
        return SHEET_REPORTS
    return None


def _cells(row):
    return ["" if c is None else str(c).strip() for c in row]


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _scalar_values_from_dict(d, editable):
    """{model_attr: (raw, kind, label)} for the editable keys present in a dict."""
    out = {}
    for k, v in d.items():
        key = _norm_col(k)
        if key in editable and v is not None and str(v).strip() != "":
            attr, kind = editable[key]
            out[attr] = (str(v), kind, key)
    return out


def _rows_from_json(payload) -> dict:
    """Flatten the downloaded JSON tree (``build_json`` shape) into the same
    per-entity rows the sheet parser produces. Antibody core fields run through the
    identical header parser so JSON and Excel edits behave the same; extras and the
    scalar families (targets / cell lines / samples / assignments / reagent
    requests / culture events) are matched by id."""
    antibodies, reports, targets_r, cell_lines_r, samples_r = [], [], [], [], []
    assignments_r, reagent_requests_r, culture_events_r, inventory_r = [], [], [], []

    def _scalar_children(node, key, editable, sink):
        for c in (node.get(key) or []):
            if not isinstance(c, dict):
                continue
            cid = _int_or_none(c.get("id"))
            vals = _scalar_values_from_dict(c, editable)
            if cid and vals:
                sink.append({"id": cid, "values": vals})

    empty = {"antibodies": antibodies, "reports": reports, "targets": targets_r,
             "cell_lines": cell_lines_r, "samples": samples_r,
             "assignments": assignments_r, "reagent_requests": reagent_requests_r,
             "culture_events": culture_events_r, "inventory": inventory_r}
    if not isinstance(payload, dict):
        return empty
    for t in (payload.get("targets") or []):
        if not isinstance(t, dict):
            continue
        tid = _int_or_none(t.get("id"))
        tvals = _scalar_values_from_dict(t, _EDITABLE_TARGETS)
        if tid and tvals:
            targets_r.append({"id": tid, "values": tvals})
        for ab in (t.get("antibodies") or []):
            if not isinstance(ab, dict):
                continue
            header = [str(k) for k in ab.keys()]
            cells = ["" if v is None else str(v) for v in ab.values()]
            m_list = meta._finalize([meta._row_by_header(header, cells)])
            m = m_list[0] if m_list else {}
            rec_id = _int_or_none(ab.get("id"))
            extras = _scalar_values_from_dict(ab, _EDITABLE_ANTIBODY_EXTRA)
            if not (rec_id or m.get("catalogue")):
                continue  # nothing to match/create; skip (never deletes)
            antibodies.append({"id": rec_id, "meta": m, "extras": extras})
        for cl in (t.get("cell_lines") or []):
            if not isinstance(cl, dict):
                continue
            cid = _int_or_none(cl.get("id"))
            cvals = _scalar_values_from_dict(cl, _EDITABLE_CELL_LINES)
            if cid and cvals:
                cell_lines_r.append({"id": cid, "values": cvals})
        for s in (t.get("samples") or []):
            if not isinstance(s, dict):
                continue
            sid = _int_or_none(s.get("id"))
            svals = _scalar_values_from_dict(s, _EDITABLE_SAMPLES)
            if sid and svals:
                samples_r.append({"id": sid, "values": svals})
        _scalar_children(t, "assignments", _EDITABLE_ASSIGNMENTS, assignments_r)
        _scalar_children(t, "reagent_requests", _EDITABLE_REAGENT_REQUESTS, reagent_requests_r)
        _scalar_children(t, "culture_events", _EDITABLE_CULTURE_EVENTS, culture_events_r)
        for r in (t.get("reports") or []):
            if not isinstance(r, dict):
                continue
            reports.append({
                "id": _int_or_none(r.get("id")),
                "gene": str(r.get("gene") or "").strip(),
                "status": str(r.get("status") or "").strip(),
                "zenodo_doi": str(r.get("zenodo doi") or "").strip(),
                "f1000_doi": str(r.get("f1000 doi") or "").strip(),
            })
    # Inventory is a FLAT top-level array (not nested under a gene).
    _scalar_children(payload, "inventory", _EDITABLE_INVENTORY, inventory_r)
    return {"antibodies": antibodies, "reports": reports, "targets": targets_r,
            "cell_lines": cell_lines_r, "samples": samples_r,
            "assignments": assignments_r, "reagent_requests": reagent_requests_r,
            "culture_events": culture_events_r, "inventory": inventory_r}


def parse_upload(f) -> dict:
    """Read the uploaded file into normalised per-entity rows. Returns
    {"antibodies": [...], "reports": [...], "sheets": set, "error": str|None}.

    Accepts the downloaded **.json** tree or a multi-sheet **.xlsx** (or a
    single-sheet .csv/.tsv). Each antibody row: {"id", "meta": <parsed metadata
    dict>}. Each report row: {"id", "gene", "status", "zenodo_doi", "f1000_doi"}.
    Empty / unrecognised files return an error (nothing downstream will write)."""
    name = (getattr(f, "name", "") or "").lower()
    if name.endswith(".json"):
        try:
            f.seek(0)
        except Exception:
            pass
        try:
            raw = f.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8-sig", errors="replace")
            payload = json.loads(raw)
        except Exception as e:
            return {"error": f"could not read the JSON file ({e})", "antibodies": [],
                    "reports": [], "sheets": set()}
        j = _rows_from_json(payload)
        has_any = any(j[k] for k in ("antibodies", "reports", "targets", "cell_lines",
                                     "samples", "assignments", "reagent_requests",
                                     "culture_events", "inventory"))
        error = None if has_any else (
            "the JSON has no editable rows (expected the downloaded dataset shape: "
            "targets[] with .antibodies / .cell_lines / .samples / .reports).")
        return {"error": error, "sheets": {"json"}, **j}

    try:
        sheets = _read_sheets(f)
    except Exception as e:  # unreadable workbook / decode error
        return {"error": f"could not read the file ({e})", "antibodies": [], "reports": [],
                "sheets": set()}
    if not sheets:
        return {"error": "no recognised sheet found (expected Antibodies / Reports / "
                "Cell lines / Targets).", "antibodies": [], "reports": [], "sheets": set()}

    antibodies, reports = [], []

    if SHEET_ANTIBODIES in sheets:
        rows = sheets[SHEET_ANTIBODIES]
        if len(rows) >= 2:
            header = _cells(rows[0])
            id_idx = _col_index(header, "id")
            for raw in rows[1:]:
                cells = _cells(raw)
                if not any(cells):
                    continue
                rec_id = _int_or_none(cells[id_idx]) if id_idx is not None and id_idx < len(cells) else None
                m_list = meta._finalize([meta._row_by_header(header, cells)])
                m = m_list[0] if m_list else {}
                extras = _extras_from_row(header, cells)
                # keep the row if we can match it (id) or create it (catalogue)
                if not (rec_id or m.get("catalogue")):
                    continue  # nothing to match / create; skip (never deletes)
                antibodies.append({"id": rec_id, "meta": m, "extras": extras})

    if SHEET_REPORTS in sheets:
        rows = sheets[SHEET_REPORTS]
        if len(rows) >= 2:
            header = _cells(rows[0])
            idx = {name: _col_index(header, name) for name in
                   ("id", "gene", "status", "zenodo doi", "f1000 doi")}

            def _get(cells, key):
                i = idx[key]
                return cells[i].strip() if i is not None and i < len(cells) else ""

            for raw in rows[1:]:
                cells = _cells(raw)
                if not any(cells):
                    continue
                reports.append({
                    "id": _int_or_none(_get(cells, "id")),
                    "gene": _get(cells, "gene"),
                    "status": _get(cells, "status"),
                    "zenodo_doi": _get(cells, "zenodo doi"),
                    "f1000_doi": _get(cells, "f1000 doi"),
                })

    targets_r = _parse_scalar_sheet(sheets.get(SHEET_TARGETS, []), _EDITABLE_TARGETS)
    cell_lines_r = _parse_scalar_sheet(sheets.get(SHEET_CELL_LINES, []), _EDITABLE_CELL_LINES)
    samples_r = _parse_scalar_sheet(sheets.get("Samples", []), _EDITABLE_SAMPLES)
    assignments_r = _parse_scalar_sheet(sheets.get(SHEET_ASSIGNMENTS, []), _EDITABLE_ASSIGNMENTS)
    reagent_requests_r = _parse_scalar_sheet(
        sheets.get(SHEET_REAGENT_REQUESTS, []), _EDITABLE_REAGENT_REQUESTS)
    culture_events_r = _parse_scalar_sheet(
        sheets.get(SHEET_CULTURE_EVENTS, []), _EDITABLE_CULTURE_EVENTS)
    inventory_r = _parse_scalar_sheet(sheets.get(SHEET_INVENTORY, []), _EDITABLE_INVENTORY)

    has_any = (antibodies or reports or targets_r or cell_lines_r or samples_r
               or assignments_r or reagent_requests_r or culture_events_r or inventory_r)
    error = None if has_any else (
        "the file has no editable rows (Antibodies / Reports / Cell lines / Targets / "
        "Samples / Assignments / Reagent requests / Cell culture events / Inventory).")
    return {"error": error, "sheets": set(sheets.keys()),
            "antibodies": antibodies, "reports": reports,
            "targets": targets_r, "cell_lines": cell_lines_r, "samples": samples_r,
            "assignments": assignments_r, "reagent_requests": reagent_requests_r,
            "culture_events": culture_events_r, "inventory": inventory_r}


def _col_index(header, alias):
    """Index of the column whose normalised header maps to `alias` (via the
    metadata aliases) or equals it. None if absent."""
    want = alias
    for i, h in enumerate(header):
        nh = meta._norm_header(h)
        if nh == alias or meta.HEADER_ALIASES.get(nh) == alias:
            return i
        if nh == want:
            return i
    return None


# ---------------------------------------------------------------------------
# Upload — incoming value normalisation (mirrors _apply_metadata's transforms)
# ---------------------------------------------------------------------------

def _incoming_antibody_values(m: dict) -> list:
    """The (field, label, new_value, comparable) tuples an antibody row would set,
    using the SAME transforms as ``_apply_metadata``. Only non-empty inputs; the
    additive supplier-app flags are handled separately (never an overwrite)."""
    out = []
    comp_name = (m.get("company") or "").strip()
    if comp_name:
        c = cdb.resolve_company(comp_name, m.get("catalogue", ""), create=False)
        # The name the *write* will store, which is the only thing a preview may
        # show. `display_name` is the public website's spelling; every reader of
        # a pipeline record shows `.name`. See cropper/db.py::resolved_company_name.
        out.append(("company_id", "company",
                    cdb.resolved_company_name(comp_name, m.get("catalogue", ""), existing=c),
                    ("comp", c.pk if c else None, comp_name)))
    rrid_raw = (m.get("rrid") or "").strip()
    if rrid_raw:
        bare = rrid_utils.normalize_rrid(rrid_raw)
        if bare:
            out.append(("rrid", "RRID", bare, bare))
    clon = (m.get("clonality") or "").strip().lower()
    if clon and clon != "unknown":
        out.append(("clonality", "clonality", clon, clon))
    for src, field, label in (("clone_id", "clone_id", "clone"),
                              ("host", "host_species", "host"),
                              ("supplier_url", "supplier_url", "product url"),
                              ("lot", "lot_number", "lot")):
        val = (m.get(src) or "").strip()
        if val:
            out.append((field, label, val, val))
    # The same reader the write uses, not a second copy of the rule. This held a
    # duplicate digit-scrape, so once `_apply_metadata` learned to convert units
    # the preview and the write disagreed by a thousand-fold on "1.0 mg/mL" — the
    # preview showing 1 and the write storing 1000. See services/concentration.py.
    conc = (m.get("concentration") or "").strip()
    if conc:
        value, _err = concentration_svc.parse(conc)
        if value is not None:
            out.append(("concentration", f"concentration ({concentration_svc.STORED_UNIT})",
                        str(value), value))
    return out


def _current_antibody_comparable(ab, field, comparable):
    """The existing value in the same comparable space as _incoming's 4th item,
    and a human string for the diff. Returns (is_blank, equal, old_display)."""
    if field == "company_id":
        old_pk = ab.company_id
        old_disp = identity.company_label(ab.company) if ab.company_id and ab.company else ""
        _, new_pk, _raw = comparable
        return (not old_pk, old_pk is not None and new_pk is not None and old_pk == new_pk, old_disp)
    if field == "concentration":
        old = ab.concentration
        old_disp = str(old) if old is not None else ""
        return (old is None, old is not None and old == comparable, old_disp)
    if field == "clonality":
        old = (ab.clonality or "")
        blank = old == "" or old == "unknown"
        return (blank, (not blank) and old == comparable, old)
    old = getattr(ab, field, "") or ""
    if field == "host_species":
        # the parser lowercases host, so a round-tripped "Rabbit" arrives as
        # "rabbit" — compare case-insensitively so an untouched host isn't
        # mistaken for an overwrite.
        blank = old.strip() == ""
        return (blank, (not blank) and old.strip().lower() == str(comparable).strip().lower(), old)
    return (old == "", old != "" and old == comparable, old)


# ---------------------------------------------------------------------------
# Upload — match, plan (diff), apply
# ---------------------------------------------------------------------------

def _match_antibody(rec_id, m):
    if rec_id:
        ab = Antibody.objects.using(DB).filter(pk=rec_id).select_related("company").first()
        if ab:
            return ab
    gene = (m.get("gene") or "").strip()
    cat = (m.get("catalogue") or "").strip()
    target = Target.objects.using(DB).filter(gene_name__iexact=gene).first() if gene else None
    if target and cat:
        return cdb.find_antibody(target, m.get("company", ""), cat)
    return None


def _match_report(rec_id, gene):
    if rec_id:
        r = Report.objects.using(DB).filter(pk=rec_id).select_related("target").first()
        if r:
            return r, None
    t = Target.objects.using(DB).filter(gene_name__iexact=(gene or "").strip()).first() if gene else None
    if not t:
        return None, ("no target" if gene else "no gene")
    rpts = list(t.reports.using(DB).all())
    if len(rpts) == 1:
        return rpts[0], None
    if len(rpts) == 0:
        return None, ("create", t)      # no report yet → create one for this target
    return None, "ambiguous"            # >1 report, no id → cannot disambiguate


def _label(gene, extra):
    return f"{gene}: {extra}" if gene else extra


def plan_upload(parsed: dict) -> dict:
    """Read-only. Classify every incoming change as create / fill / overwrite /
    blocked, with a field-level diff for fills & overwrites. No writes."""
    if parsed.get("error"):
        return {"ok": False, "error": parsed["error"]}

    ab_creates, ab_fills, ab_overwrites, ab_blocked = [], [], [], []
    for row in parsed.get("antibodies", []):
        m = row["meta"]
        gene = (m.get("gene") or "").strip()
        cat = (m.get("catalogue") or "").strip()
        ab = _match_antibody(row["id"], m)
        if ab is None:
            if gene and cat:
                ab_creates.append({"label": _label(gene, cat), "gene": gene, "catalogue": cat})
            else:
                ab_blocked.append({"label": _label(gene, cat or "(row)"),
                                   "reason": "no matching record and no gene+catalogue to create one"})
            continue
        rec_gene = (ab.target.gene_name if ab.target_id else gene) or gene
        for field, flabel, new_disp, comparable in _incoming_antibody_values(m):
            blank, equal, old_disp = _current_antibody_comparable(ab, field, comparable)
            if equal:
                continue
            item = {"label": _label(rec_gene, ab.catalogue_number or cat),
                    "field": flabel, "old": old_disp, "new": new_disp}
            (ab_fills if blank else ab_overwrites).append(item)
        # extra scalar columns (antigen, isotype, in_kind_value, flags, …)
        ef, eo = _antibody_extra_diffs(ab, row.get("extras", {}))
        ab_fills.extend(ef)
        ab_overwrites.extend(eo)

    rp_creates, rp_fills, rp_overwrites, rp_blocked = [], [], [], []
    for row in parsed.get("reports", []):
        gene = (row.get("gene") or "").strip()
        rpt, info = _match_report(row["id"], gene)
        if rpt is None:
            if isinstance(info, tuple) and info[0] == "create":
                # a create only matters if it actually carries a value
                vals = [(lbl, row.get(f)) for _a, f, lbl in _REPORT_FIELDS if (row.get(f) or "").strip()]
                if vals:
                    rp_creates.append({"label": gene, "fields": [lbl for lbl, _ in vals]})
                continue
            reason = {"ambiguous": "gene has >1 report — add the id column to target one",
                      "no target": "no target for this gene", "no gene": "no id and no gene"}.get(info, str(info))
            rp_blocked.append({"label": _label(gene, "report"), "reason": reason})
            continue
        for alias, field, flabel in _REPORT_FIELDS:
            new = (row.get(field) or "").strip()
            if not new:
                continue
            old = (getattr(rpt, field) or "").strip()
            if old == new:
                continue
            item = {"label": gene or f"report #{rpt.pk}", "field": flabel, "old": old, "new": new}
            (rp_fills if not old else rp_overwrites).append(item)

    tg_plan = _plan_scalar_family(parsed.get("targets", []), _EDITABLE_TARGETS, Target)
    cl_plan = _plan_scalar_family(parsed.get("cell_lines", []), _EDITABLE_CELL_LINES, CellLine)
    sm_plan = _plan_scalar_family(parsed.get("samples", []), _EDITABLE_SAMPLES, _sample_model())
    as_plan = _plan_scalar_family(parsed.get("assignments", []), _EDITABLE_ASSIGNMENTS, TargetAssignment)
    rr_plan = _plan_scalar_family(
        parsed.get("reagent_requests", []), _EDITABLE_REAGENT_REQUESTS, ReagentRequest)
    ce_plan = _plan_scalar_family(
        parsed.get("culture_events", []), _EDITABLE_CULTURE_EVENTS, CellCultureEvent)
    iv_plan = _plan_scalar_family(
        parsed.get("inventory", []), _EDITABLE_INVENTORY, InventoryLocation)

    groups = {
        "antibodies": {"creates": ab_creates, "fills": ab_fills,
                       "overwrites": ab_overwrites, "blocked": ab_blocked},
        "reports": {"creates": rp_creates, "fills": rp_fills,
                    "overwrites": rp_overwrites, "blocked": rp_blocked},
        "targets": tg_plan,
        "cell_lines": cl_plan,
        "samples": sm_plan,
        "assignments": as_plan,
        "reagent_requests": rr_plan,
        "culture_events": ce_plan,
        "inventory": iv_plan,
    }
    counts = {
        "creates": sum(len(g["creates"]) for g in groups.values()),
        "fills": sum(len(g["fills"]) for g in groups.values()),
        "overwrites": sum(len(g["overwrites"]) for g in groups.values()),
        "blocked": sum(len(g["blocked"]) for g in groups.values()),
    }
    return {"ok": True, "error": None, "counts": counts, **groups}


def apply_upload(parsed: dict, apply_overwrites: bool = False,
                 create_targets: bool = False, member=None) -> dict:
    """Write the upsert in one transaction. Always applies gap-fills + creates;
    applies overwrites only when ``apply_overwrites`` is True. A blank cell never
    clears; absent rows are untouched; nothing is ever deleted."""
    if parsed.get("error"):
        return {"ok": False, "error": parsed["error"]}

    from pipeline.services.targets import resolve_or_create_target

    out = {"ok": True, "antibodies_created": 0, "antibodies_updated": 0,
           "reports_created": 0, "reports_updated": 0, "targets_created": [],
           "targets_updated": 0, "cell_lines_updated": 0, "samples_updated": 0,
           "assignments_updated": 0, "reagent_requests_updated": 0,
           "culture_events_updated": 0, "inventory_updated": 0,
           "skipped": [], "overwrites_applied": bool(apply_overwrites)}

    with transaction.atomic(using=DB):
        # --- Antibodies ---
        for row in parsed.get("antibodies", []):
            m = row["meta"]
            gene = (m.get("gene") or "").strip()
            cat = (m.get("catalogue") or "").strip()
            ab = _match_antibody(row["id"], m)
            created = False
            if ab is None:
                if not (gene and cat):
                    out["skipped"].append(_label(gene, cat or "(antibody row)"))
                    continue
                target = Target.objects.using(DB).filter(gene_name__iexact=gene).first()
                if target is None:
                    if create_targets:
                        target, made = resolve_or_create_target(gene, member=member)
                        if made:
                            out["targets_created"].append(target.gene_name)
                    if target is None:
                        out["skipped"].append(_label(gene, cat))
                        continue
                ab = Antibody(target=target, catalogue_number=cat)
                if member is not None and getattr(member, "site_id", None):
                    ab.site_id = member.site_id
                created = True
            _apply_metadata(ab, m, overwrite=apply_overwrites)
            # a new antibody's extras are all gap-fills; overwrites on an existing
            # one still gate on apply_overwrites (never on create).
            _apply_antibody_extras(ab, row.get("extras", {}),
                                   apply_overwrites=apply_overwrites or created)
            ab.save(using=DB)
            out["antibodies_created" if created else "antibodies_updated"] += 1

        # --- Reports ---
        for row in parsed.get("reports", []):
            gene = (row.get("gene") or "").strip()
            rpt, info = _match_report(row["id"], gene)
            created = False
            if rpt is None:
                if isinstance(info, tuple) and info[0] == "create":
                    if not any((row.get(f) or "").strip() for _a, f, _l in _REPORT_FIELDS):
                        continue
                    rpt = Report(target=info[1])
                    created = True
                else:
                    out["skipped"].append(_label(gene, "report"))
                    continue
            changed = False
            for _alias, field, _label_ in _REPORT_FIELDS:
                new = (row.get(field) or "").strip()
                if not new:
                    continue                       # blank never clears
                old = (getattr(rpt, field) or "").strip()
                if old and not apply_overwrites and not created:
                    continue                       # populated → needs confirmation
                if old == new:
                    continue
                setattr(rpt, field, new)
                changed = True
            if created or changed:
                rpt.save(using=DB)
                out["reports_created" if created else "reports_updated"] += 1

        # --- Scalar families (update existing by id; create stays on their pages) ---
        out["targets_updated"] = _apply_scalar_family(
            parsed.get("targets", []), _EDITABLE_TARGETS, Target, apply_overwrites)
        out["cell_lines_updated"] = _apply_scalar_family(
            parsed.get("cell_lines", []), _EDITABLE_CELL_LINES, CellLine, apply_overwrites)
        out["samples_updated"] = _apply_scalar_family(
            parsed.get("samples", []), _EDITABLE_SAMPLES, _sample_model(), apply_overwrites)
        out["assignments_updated"] = _apply_scalar_family(
            parsed.get("assignments", []), _EDITABLE_ASSIGNMENTS, TargetAssignment, apply_overwrites)
        out["reagent_requests_updated"] = _apply_scalar_family(
            parsed.get("reagent_requests", []), _EDITABLE_REAGENT_REQUESTS, ReagentRequest,
            apply_overwrites)
        out["culture_events_updated"] = _apply_scalar_family(
            parsed.get("culture_events", []), _EDITABLE_CULTURE_EVENTS, CellCultureEvent,
            apply_overwrites)
        out["inventory_updated"] = _apply_scalar_family(
            parsed.get("inventory", []), _EDITABLE_INVENTORY, InventoryLocation, apply_overwrites)

    return out


# ===========================================================================
# Field selector — a full registry of exportable fields per family.
#
# Each family lists (header, kind, getter) tuples. ``kind`` is one of:
#   "key"     — always exported, locked (the id / natural key + gene context)
#   "default" — part of the historical default export (kept for round-trip)
#   "extra"   — a real model field newly exposed to the selector (download-side)
#
# The default column constants above ARE the key+default headers, in order —
# ``build_workbook(selection=None)`` still drives the original code path, so the
# registry only takes over when the user picks fields. ``getter(obj, gene)``
# returns the cell string; booleans render as yes/no, decimals as plain str.
# ===========================================================================

def _yn(v):
    return "yes" if v else "no"


def _dec(v):
    return str(v) if v is not None else ""


def _company_name(obj):
    c = getattr(obj, "company", None)
    return cdb.company_label(c) if c else ""


def _site_name(obj):
    s = getattr(obj, "site", None)
    return s.name if getattr(obj, "site_id", None) and s else ""


def _project_name(obj):
    p = getattr(obj, "project", None)
    return p.name if getattr(obj, "project_id", None) and p else ""


# InventoryLocation is polymorphic (antibody | cell_line | vial). These read the
# single set parent and its gene context — all export-only reference.
def _inv_attached_to(loc):
    if loc.antibody_id:
        return "antibody"
    if loc.vial_id:
        return "vial"
    if loc.cell_line_id:
        return "cell_line"
    return ""


def _inv_parent_id(loc):
    return loc.antibody_id or loc.vial_id or loc.cell_line_id or ""


def _inv_gene(loc):
    if loc.antibody_id and loc.antibody and loc.antibody.target_id:
        return loc.antibody.target.gene_name or ""
    if loc.cell_line_id and loc.cell_line and loc.cell_line.target_id:
        return loc.cell_line.target.gene_name or ""
    if (loc.vial_id and loc.vial and loc.vial.cell_line_id and loc.vial.cell_line
            and loc.vial.cell_line.target_id):
        return loc.vial.cell_line.target.gene_name or ""
    return ""


def _inv_parent_label(loc):
    if loc.antibody_id and loc.antibody:
        return loc.antibody.catalogue_number or f"antibody #{loc.antibody_id}"
    if loc.vial_id and loc.vial:
        return f"C{loc.vial.c_number}" if loc.vial.c_number else f"vial #{loc.vial_id}"
    if loc.cell_line_id and loc.cell_line:
        return loc.cell_line.name or f"cell line #{loc.cell_line_id}"
    return ""


def _cl_parent_name(cl):
    return (cl.parent_line.name if cl.parent_line_id else "") or cl.parental_line_name or ""


def _apps_str(ab):
    return ", ".join(lbl for f, lbl in _ANTIBODY_APPS if getattr(ab, f))


def _isodate(d):
    return d.isoformat() if d else ""


def _json_cell(v):
    if not v:
        return ""
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


# ---- Targets ----
TARGET_FIELDS = [
    ("id", "key", lambda t, g: t.pk),
    ("gene", "key", lambda t, g: g),
    ("protein", "default", lambda t, g: t.protein_name or ""),
    ("uniprot", "default", lambda t, g: t.uniprot_id or ""),
    ("status", "default", lambda t, g: t.status or ""),
    ("aliases", "default", lambda t, g: t.aliases or ""),
    ("alternative_name", "extra", lambda t, g: t.alternative_name or ""),
    ("protein_type", "extra", lambda t, g: t.protein_type or ""),
    ("theoretical_mass_kda", "extra", lambda t, g: _dec(t.theoretical_mass_kda)),
    ("depmap_expression", "extra", lambda t, g: _dec(t.depmap_expression)),
    ("commercial_ko_available", "extra", lambda t, g: _yn(t.commercial_ko_available)),
    ("ko_validated", "extra", lambda t, g: _yn(t.ko_validated)),
    ("citation", "extra", lambda t, g: t.citation or ""),
    ("cell_line_link", "extra", lambda t, g: t.cell_line_link or ""),
]

# ---- Antibodies (costs live here) ----
ANTIBODY_FIELDS = [
    ("id", "key", lambda ab, g: ab.pk),
    ("gene", "key", lambda ab, g: g),
    ("catalogue", "key", lambda ab, g: ab.catalogue_number or ""),
    ("company", "default", lambda ab, g: _company_name(ab)),
    ("rrid", "default", lambda ab, g: ab.rrid or ""),
    ("host", "default", lambda ab, g: ab.host_species or ""),
    ("clonality", "default", lambda ab, g: ab.clonality or ""),
    ("clone", "default", lambda ab, g: ab.clone_id or ""),
    ("lot", "default", lambda ab, g: ab.lot_number or ""),
    ("concentration", "default", lambda ab, g: _dec(ab.concentration)),
    ("product url", "default", lambda ab, g: ab.supplier_url or ""),
    ("supplier recommendations", "default", lambda ab, g: _apps_str(ab)),
    ("antigen", "extra", lambda ab, g: ab.antigen or ""),
    ("isotype", "extra", lambda ab, g: ab.isotype or ""),
    ("species_reactivity", "extra", lambda ab, g: ab.species_reactivity or ""),
    ("is_recombinant", "extra", lambda ab, g: _yn(ab.is_recombinant)),
    ("rrid_link", "extra", lambda ab, g: ab.rrid_link or ""),
    ("supplier_recommended_dilutions", "extra", lambda ab, g: _json_cell(ab.supplier_recommended_dilutions)),
    ("supplier_validated_ihc", "extra", lambda ab, g: _yn(ab.supplier_validated_ihc)),
    ("supplier_validated_elisa", "extra", lambda ab, g: _yn(ab.supplier_validated_elisa)),
    ("validation_details_wb", "extra", lambda ab, g: ab.validation_details_wb or ""),
    ("validation_details_if", "extra", lambda ab, g: ab.validation_details_if or ""),
    ("wb_recommended", "extra", lambda ab, g: _yn(ab.wb_recommended)),
    ("ip_recommended", "extra", lambda ab, g: _yn(ab.ip_recommended)),
    ("if_recommended", "extra", lambda ab, g: _yn(ab.if_recommended)),
    ("fc_recommended", "extra", lambda ab, g: _yn(ab.fc_recommended)),
    ("out_of_market", "extra", lambda ab, g: _yn(ab.out_of_market)),
    ("empty_vial", "extra", lambda ab, g: _yn(ab.empty_vial)),
    ("comments", "extra", lambda ab, g: ab.comments or ""),
    ("in_kind_value", "extra", lambda ab, g: _dec(ab.in_kind_value)),
    ("in_kind_currency", "extra", lambda ab, g: ab.in_kind_currency or ""),
    ("acquisition_method", "extra", lambda ab, g: ab.acquisition_method or ""),
]

# ---- Cell lines (costs live here) ----
CELL_LINE_FIELDS = [
    ("id", "key", lambda cl, g: cl.pk),
    ("c number", "key", lambda cl, g: cl.c_number or ""),
    ("name", "default", lambda cl, g: cl.name or ""),
    ("gene", "key", lambda cl, g: g),
    ("genotype", "default", lambda cl, g: cl.genotype or ""),
    ("parent", "default", lambda cl, g: _cl_parent_name(cl)),
    ("cellosaurus", "default", lambda cl, g: cl.cellosaurus_id or ""),
    ("supplier", "default", lambda cl, g: _company_name(cl)),
    ("catalogue", "default", lambda cl, g: cl.catalogue_number or ""),
    ("lot", "default", lambda cl, g: cl.lot_number or ""),
    ("medium", "default", lambda cl, g: cl.medium or ""),
    ("clone", "default", lambda cl, g: cl.clone or ""),
    ("growth_properties", "extra", lambda cl, g: cl.growth_properties or ""),
    ("species", "extra", lambda cl, g: cl.species or ""),
    ("origin", "extra", lambda cl, g: cl.origin or ""),
    ("origin_comments", "extra", lambda cl, g: cl.origin_comments or ""),
    ("ko_validated", "extra", lambda cl, g: _yn(cl.ko_validated)),
    ("ko_validation_notes", "extra", lambda cl, g: cl.ko_validation_notes or ""),
    ("parental_line_name", "extra", lambda cl, g: cl.parental_line_name or ""),
    ("received", "extra", lambda cl, g: _yn(cl.received)),
    ("thawed", "extra", lambda cl, g: _yn(cl.thawed)),
    ("location_original_vial", "extra", lambda cl, g: cl.location_original_vial or ""),
    ("in_kind_value", "extra", lambda cl, g: _dec(cl.in_kind_value)),
    ("in_kind_currency", "extra", lambda cl, g: cl.in_kind_currency or ""),
    ("acquisition_method", "extra", lambda cl, g: cl.acquisition_method or ""),
]

# ---- Cell-line batches (vials) — all extra; only exported when selected ----
VIAL_FIELDS = [
    ("id", "key", lambda v, g: v.pk),
    ("cell_line id", "key", lambda v, g: v.cell_line_id or ""),
    ("gene", "key", lambda v, g: g),
    ("c number", "extra", lambda v, g: v.c_number or ""),
    ("freeze_date", "extra", lambda v, g: _isodate(v.freeze_date)),
    ("passage_number", "extra", lambda v, g: _dec(v.passage_number)),
    ("vial_count", "extra", lambda v, g: _dec(v.vial_count)),
    ("received_date", "extra", lambda v, g: _isodate(v.received_date)),
    ("acquisition_method", "extra", lambda v, g: v.acquisition_method or ""),
    ("received", "extra", lambda v, g: _yn(v.received)),
    ("thawed", "extra", lambda v, g: _yn(v.thawed)),
    ("location_original_vial", "extra", lambda v, g: v.location_original_vial or ""),
    ("in_kind_value", "extra", lambda v, g: _dec(v.in_kind_value)),
    ("in_kind_currency", "extra", lambda v, g: v.in_kind_currency or ""),
    ("notes", "extra", lambda v, g: v.notes or ""),
]

# ---- Reports ----
REPORT_FIELDS = [
    ("id", "key", lambda r, g: r.pk),
    ("gene", "key", lambda r, g: g),
    ("status", "default", lambda r, g: r.status or ""),
    ("zenodo doi", "default", lambda r, g: r.zenodo_doi or ""),
    ("f1000 doi", "default", lambda r, g: r.f1000_doi or ""),
    ("zenodo date", "default", lambda r, g: _isodate(r.zenodo_date)),
    ("f1000 date", "default", lambda r, g: _isodate(r.f1000_date)),
    ("introduction_text", "extra", lambda r, g: r.introduction_text or ""),
]

# ---- Samples (lysates) — all extra; only exported when selected ----
SAMPLE_FIELDS = [
    ("id", "key", lambda s, g: s.pk),
    ("cell_line id", "key", lambda s, g: s.cell_line_id or ""),
    ("gene", "key", lambda s, g: g),
    ("sample_type", "extra", lambda s, g: s.sample_type or ""),
    ("preparation_date", "extra", lambda s, g: _isodate(s.preparation_date)),
    ("lysis_buffer", "extra", lambda s, g: s.lysis_buffer or ""),
    ("protease_inhibitor", "extra", lambda s, g: s.protease_inhibitor or ""),
    ("protein_concentration", "extra", lambda s, g: _dec(s.protein_concentration)),
    ("quantification_method", "extra", lambda s, g: s.quantification_method or ""),
    ("conditioning_time_hours", "extra", lambda s, g: _dec(s.conditioning_time_hours)),
    ("concentration_method", "extra", lambda s, g: s.concentration_method or ""),
    ("concentration_fold", "extra", lambda s, g: _dec(s.concentration_fold)),
    ("volume_remaining_ul", "extra", lambda s, g: _dec(s.volume_remaining_ul)),
    ("storage_location", "extra", lambda s, g: s.storage_location or ""),
    ("storage_temperature", "extra", lambda s, g: s.storage_temperature or ""),
    ("status", "extra", lambda s, g: s.status or ""),
    ("notes", "extra", lambda s, g: s.notes or ""),
]

# ---- Target work assignments (per-site task board; people columns omitted) ----
ASSIGNMENT_FIELDS = [
    ("id", "key", lambda a, g: a.pk),
    ("gene", "key", lambda a, g: g),
    ("site", "default", lambda a, g: _site_name(a)),
    ("task_type", "default", lambda a, g: a.task_type or ""),
    ("status", "default", lambda a, g: a.status or ""),
    ("priority", "extra", lambda a, g: _dec(a.priority)),
    ("assigned_date", "extra", lambda a, g: _isodate(a.assigned_date)),
    ("notes", "extra", lambda a, g: a.notes or ""),
]

# ---- Reagent requests (procurement line items; requester column omitted) ----
REAGENT_REQUEST_FIELDS = [
    ("id", "key", lambda r, g: r.pk),
    ("gene", "key", lambda r, g: g),
    ("item_type", "default", lambda r, g: r.item_type or ""),
    ("company", "default", lambda r, g: _company_name(r)),
    ("catalogue_numbers", "default", lambda r, g: r.catalogue_numbers or ""),
    ("quantity", "default", lambda r, g: _dec(r.quantity)),
    ("requested_volume_ul", "extra", lambda r, g: _dec(r.requested_volume_ul)),
    ("status", "default", lambda r, g: r.status or ""),
    ("requested_date", "extra", lambda r, g: _isodate(r.requested_date)),
    ("batch", "extra", lambda r, g: r.batch.title if r.batch_id else ""),
    ("site", "extra", lambda r, g: _site_name(r)),
    ("project", "extra", lambda r, g: _project_name(r)),
    ("comments", "extra", lambda r, g: r.comments or ""),
]

# ---- Cell-culture events (lifecycle log per cell line; operator column omitted) ----
CULTURE_EVENT_FIELDS = [
    ("id", "key", lambda e, g: e.pk),
    ("cell_line id", "key", lambda e, g: e.cell_line_id or ""),
    ("gene", "key", lambda e, g: g),
    ("event_type", "default", lambda e, g: e.event_type or ""),
    ("date", "default", lambda e, g: _isodate(e.date)),
    ("passage_number", "extra", lambda e, g: _dec(e.passage_number)),
    ("split_ratio", "extra", lambda e, g: e.split_ratio or ""),
    ("vials_frozen", "extra", lambda e, g: _dec(e.vials_frozen)),
    ("discard_reason", "extra", lambda e, g: e.discard_reason or ""),
    ("notes", "extra", lambda e, g: e.notes or ""),
]

# ---- Inventory locations (FLAT: one row per physical storage slot). The gene is
# derived from whichever parent the location hangs off; parent columns are
# export-only reference. Polymorphic, so this cannot nest under a single gene. ----
INVENTORY_FIELDS = [
    ("id", "key", lambda l, g: l.pk),
    ("attached_to", "key", lambda l, g: _inv_attached_to(l)),
    ("parent id", "key", lambda l, g: _inv_parent_id(l)),
    ("gene", "key", lambda l, g: _inv_gene(l)),
    ("parent", "default", lambda l, g: _inv_parent_label(l)),
    ("site", "default", lambda l, g: _site_name(l)),
    ("storage_type", "default", lambda l, g: l.storage_type or ""),
    ("building", "extra", lambda l, g: l.building or ""),
    ("room", "extra", lambda l, g: l.room or ""),
    ("freezer", "extra", lambda l, g: l.freezer or ""),
    ("shelf", "extra", lambda l, g: l.shelf or ""),
    ("rack", "extra", lambda l, g: l.rack or ""),
    ("box", "extra", lambda l, g: l.box or ""),
    ("position", "extra", lambda l, g: l.position or ""),
    ("notes", "extra", lambda l, g: l.notes or ""),
]

# Ordered family registry. Keys are the selection routing keys + sheet titles.
FAMILIES = {
    "targets":    {"title": SHEET_TARGETS,    "fields": TARGET_FIELDS},
    "antibodies": {"title": SHEET_ANTIBODIES, "fields": ANTIBODY_FIELDS},
    "cell_lines": {"title": SHEET_CELL_LINES, "fields": CELL_LINE_FIELDS},
    "vials":      {"title": "Cell-line batches", "fields": VIAL_FIELDS},
    "reports":    {"title": SHEET_REPORTS,    "fields": REPORT_FIELDS},
    "samples":    {"title": "Samples",        "fields": SAMPLE_FIELDS},
    "assignments":      {"title": SHEET_ASSIGNMENTS,      "fields": ASSIGNMENT_FIELDS},
    "reagent_requests": {"title": SHEET_REAGENT_REQUESTS, "fields": REAGENT_REQUEST_FIELDS},
    "culture_events":   {"title": SHEET_CULTURE_EVENTS,   "fields": CULTURE_EVENT_FIELDS},
    "inventory":        {"title": SHEET_INVENTORY,        "fields": INVENTORY_FIELDS},
}
FAMILY_ORDER = ["targets", "antibodies", "cell_lines", "vials", "reports", "samples",
                "assignments", "reagent_requests", "culture_events", "inventory"]

# Child families nest under each target in the JSON tree. Inventory is FLAT
# (a top-level sibling of `targets`), so it is deliberately NOT listed here.
_CHILD_FAMILIES = ["antibodies", "cell_lines", "vials", "reports", "samples",
                   "assignments", "reagent_requests", "culture_events"]


def family_catalogue():
    """UI-facing description of every family and its fields (for the selector)."""
    out = []
    for fkey in FAMILY_ORDER:
        fam = FAMILIES[fkey]
        out.append({
            "key": fkey, "title": fam["title"],
            "fields": [{"header": h, "kind": k} for h, k, _ in fam["fields"]],
        })
    return out


def parse_selection(fields_param: str):
    """Parse the ``fields`` query param into ``{family: [headers]}`` or None.

    Format: ``family:colA,colB|family2:colC`` — a family with no colon body
    (e.g. ``reports:``) exports its keys only; the literal ``all`` selects every
    field of every family. Unknown families/headers are ignored. Returns None for
    an empty param so the caller falls back to the historical default export.
    Keys are always included regardless of what is listed."""
    s = (fields_param or "").strip()
    if not s:
        return None
    if s.lower() == "all":
        return {fk: [h for h, k, _ in FAMILIES[fk]["fields"]] for fk in FAMILY_ORDER}
    sel = {}
    for part in s.split("|"):
        part = part.strip()
        if not part:
            continue
        fam, _, body = part.partition(":")
        fam = fam.strip()
        if fam not in FAMILIES:
            continue
        valid = {h for h, _k, _g in FAMILIES[fam]["fields"]}
        cols = [c.strip() for c in body.split(",") if c.strip() and c.strip() in valid]
        sel[fam] = cols
    return sel or None


def _selected_cols(fkey, selection):
    """Ordered [(header, getter)] for a family, or None if the family is absent
    from the selection. Keys are always included and lead the list."""
    if fkey not in selection:
        return None
    chosen = set(selection[fkey])
    return [(h, g) for h, k, g in FAMILIES[fkey]["fields"] if k == "key" or h in chosen]


def _prefetch_all(targets):
    return targets.prefetch_related(
        "antibodies__company", "cell_lines__company", "cell_lines__parent_line",
        "cell_lines__vials", "cell_lines__samples", "cell_lines__culture_events",
        "reports", "assignments__site", "reagent_requests__company",
        "reagent_requests__site", "reagent_requests__project", "reagent_requests__batch")


def _inventory_qs(targets):
    """InventoryLocation rows in scope. Locations are polymorphic (antibody |
    cell line | vial), so scope by whichever parent carries an in-scope target.
    A whole-dataset download (every target) returns every location, including any
    whose parent has no target."""
    from django.db.models import Q
    qs = InventoryLocation.objects.using(DB).select_related(
        "antibody__target", "cell_line__target", "vial__cell_line__target", "site")
    if targets.count() >= Target.objects.using(DB).count():
        return qs.order_by("pk")
    tids = list(targets.values_list("pk", flat=True))
    return qs.filter(
        Q(antibody__target_id__in=tids)
        | Q(cell_line__target_id__in=tids)
        | Q(vial__cell_line__target_id__in=tids)
    ).order_by("pk")


def _iter_child(fkey, target, gene):
    if fkey == "antibodies":
        for ab in target.antibodies.all():
            yield ab
    elif fkey == "cell_lines":
        for cl in target.cell_lines.all():
            yield cl
    elif fkey == "reports":
        for r in target.reports.all():
            yield r
    elif fkey == "vials":
        for cl in target.cell_lines.all():
            for v in cl.vials.all():
                yield v
    elif fkey == "samples":
        for cl in target.cell_lines.all():
            for s in cl.samples.all():
                yield s
    elif fkey == "assignments":
        for a in target.assignments.all():
            yield a
    elif fkey == "reagent_requests":
        for r in target.reagent_requests.all():
            yield r
    elif fkey == "culture_events":
        for cl in target.cell_lines.all():
            for e in cl.culture_events.all():
                yield e


def _write_sheet(wb, title, cols, rows):
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    s = wb.create_sheet(title[:31])
    s.append(cols)
    for c in s[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="064C83")
    for r in rows:
        s.append(r)
    s.freeze_panes = "A2"
    for i in range(1, len(cols) + 1):
        s.column_dimensions[get_column_letter(i)].width = 16
    return s


def _build_workbook_selected(targets, selection) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    targets = _prefetch_all(targets)
    materialised = list(targets)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET_LEGEND
    ws.append(["Field", "Meaning"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="064C83")
    for k, v in LEGEND_ROWS:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 90

    for fkey in FAMILY_ORDER:
        cols = _selected_cols(fkey, selection)
        if cols is None:
            continue
        headers = [h for h, _g in cols]
        rows = []
        if fkey == "targets":
            for t in materialised:
                gene = _gene_of(t)
                rows.append([g(t, gene) for _h, g in cols])
        elif fkey == "inventory":
            for loc in _inventory_qs(targets):        # flat: one row per location
                rows.append([g(loc, "") for _h, g in cols])
        else:
            for t in materialised:
                gene = _gene_of(t)
                for obj in _iter_child(fkey, t, gene):
                    rows.append([g(obj, gene) for _h, g in cols])
        _write_sheet(wb, FAMILIES[fkey]["title"], headers, rows)

    # Never hand back a Legend-only workbook.
    if len(wb.sheetnames) == 1:
        _write_sheet(wb, SHEET_TARGETS,
                     [h for h, _g in (_selected_cols("targets", {"targets": []}) or [])],
                     [])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_json_selected(targets, selection) -> dict:
    targets = _prefetch_all(targets)

    tcols = _selected_cols("targets", selection)
    if tcols is None:                        # targets always root the tree (keys only)
        tcols = [(h, g) for h, k, g in TARGET_FIELDS if k == "key"]
    child_cols = {ck: _selected_cols(ck, selection) for ck in _CHILD_FAMILIES}
    child_cols = {ck: c for ck, c in child_cols.items() if c is not None}

    out_targets = []
    for t in targets:
        gene = _gene_of(t)
        node = {h: g(t, gene) for h, g in tcols}
        for ck, cols in child_cols.items():
            node[ck] = [{h: g(obj, gene) for h, g in cols}
                        for obj in _iter_child(ck, t, gene)]
        out_targets.append(node)

    colmap = {"targets": [h for h, _g in tcols]}
    for ck, cols in child_cols.items():
        colmap[ck] = [h for h, _g in cols]

    result = {
        "_legend": {k: v for k, v in LEGEND_ROWS},
        "_columns": colmap,
        "targets": out_targets,
    }

    # Inventory is FLAT — a top-level array beside `targets`, not nested in the tree.
    inv_cols = _selected_cols("inventory", selection)
    if inv_cols is not None:
        result["inventory"] = [{h: g(loc, "") for h, g in inv_cols}
                               for loc in _inventory_qs(targets)]
        colmap["inventory"] = [h for h, _g in inv_cols]

    return result


# ===========================================================================
# Phase 3 — write-back for the extra fields and the new families.
#
# A generic, guarded scalar upsert layered ON TOP of the antibody-core
# (metadata parser) + report paths, which are unchanged. It carries the same
# guarantees: a blank cell never clears, gap-fill (empty -> value) applies
# freely, overwriting a populated value needs confirmation, and nothing is ever
# deleted. Booleans are always treated as an overwrite (never a silent flip).
#
# Scope rule (matches the dedicated-pages philosophy): targets / cell lines /
# samples are matched by id and UPDATED ONLY — creating them stays on their own
# pages. Antibody extras attach to the antibody already matched by the core path.
# ===========================================================================

from datetime import date as _date          # noqa: E402


def _norm_col(h) -> str:
    """Header normaliser for the new families — keeps underscores (unlike the
    metadata parser), so 'in_kind_value' stays intact."""
    return str(h or "").strip().lower()


# header -> (model_attr, kind). kind in text|int|decimal|date|bool.
# Keys, relational (company/parent), computed (applications) and cache/JSON
# fields are intentionally absent — edit those on their dedicated pages.
_EDITABLE_TARGETS = {
    "protein": ("protein_name", "text"), "uniprot": ("uniprot_id", "text"),
    "status": ("status", "text"), "aliases": ("aliases", "text"),
    "alternative_name": ("alternative_name", "text"),
    "protein_type": ("protein_type", "text"),
    "theoretical_mass_kda": ("theoretical_mass_kda", "decimal"),
    "commercial_ko_available": ("commercial_ko_available", "bool"),
    "ko_validated": ("ko_validated", "bool"),
    "citation": ("citation", "text"), "cell_line_link": ("cell_line_link", "text"),
}
_EDITABLE_CELL_LINES = {
    "name": ("name", "text"), "genotype": ("genotype", "text"),
    "cellosaurus": ("cellosaurus_id", "text"), "catalogue": ("catalogue_number", "text"),
    "lot": ("lot_number", "text"), "medium": ("medium", "text"), "clone": ("clone", "text"),
    "growth_properties": ("growth_properties", "text"), "species": ("species", "text"),
    "origin": ("origin", "text"), "origin_comments": ("origin_comments", "text"),
    "ko_validated": ("ko_validated", "bool"), "ko_validation_notes": ("ko_validation_notes", "text"),
    "parental_line_name": ("parental_line_name", "text"),
    "received": ("received", "bool"), "thawed": ("thawed", "bool"),
    "location_original_vial": ("location_original_vial", "text"),
    "in_kind_value": ("in_kind_value", "decimal"), "in_kind_currency": ("in_kind_currency", "text"),
    "acquisition_method": ("acquisition_method", "text"),
}
_EDITABLE_SAMPLES = {
    "sample_type": ("sample_type", "text"), "preparation_date": ("preparation_date", "date"),
    "lysis_buffer": ("lysis_buffer", "text"), "protease_inhibitor": ("protease_inhibitor", "text"),
    "protein_concentration": ("protein_concentration", "decimal"),
    "quantification_method": ("quantification_method", "text"),
    "conditioning_time_hours": ("conditioning_time_hours", "decimal"),
    "concentration_method": ("concentration_method", "text"),
    "concentration_fold": ("concentration_fold", "int"),
    "volume_remaining_ul": ("volume_remaining_ul", "decimal"),
    "storage_location": ("storage_location", "text"),
    "storage_temperature": ("storage_temperature", "text"),
    "status": ("status", "text"), "notes": ("notes", "text"),
}
# Antibody EXTRA scalars only (core stays on the metadata path; is_recombinant is
# derived from clonality there, so it's not repeated here).
_EDITABLE_ANTIBODY_EXTRA = {
    "antigen": ("antigen", "text"), "isotype": ("isotype", "text"),
    "species_reactivity": ("species_reactivity", "text"), "rrid_link": ("rrid_link", "text"),
    "validation_details_wb": ("validation_details_wb", "text"),
    "validation_details_if": ("validation_details_if", "text"),
    "comments": ("comments", "text"),
    "in_kind_value": ("in_kind_value", "decimal"), "in_kind_currency": ("in_kind_currency", "text"),
    "acquisition_method": ("acquisition_method", "text"),
    "supplier_validated_ihc": ("supplier_validated_ihc", "bool"),
    "supplier_validated_elisa": ("supplier_validated_elisa", "bool"),
    "wb_recommended": ("wb_recommended", "bool"), "ip_recommended": ("ip_recommended", "bool"),
    "if_recommended": ("if_recommended", "bool"), "fc_recommended": ("fc_recommended", "bool"),
    "out_of_market": ("out_of_market", "bool"), "empty_vial": ("empty_vial", "bool"),
}
# Logistics / workflow families (id-matched, update-only; person + relational
# columns are export-only, never written, so no individual's data round-trips).
_EDITABLE_ASSIGNMENTS = {
    "status": ("status", "text"), "priority": ("priority", "int"),
    "assigned_date": ("assigned_date", "date"), "notes": ("notes", "text"),
}
_EDITABLE_REAGENT_REQUESTS = {
    "item_type": ("item_type", "text"), "catalogue_numbers": ("catalogue_numbers", "text"),
    "quantity": ("quantity", "int"), "requested_volume_ul": ("requested_volume_ul", "int"),
    "status": ("status", "text"), "requested_date": ("requested_date", "date"),
    "comments": ("comments", "text"),
}
_EDITABLE_CULTURE_EVENTS = {
    "event_type": ("event_type", "text"), "date": ("date", "date"),
    "passage_number": ("passage_number", "int"), "split_ratio": ("split_ratio", "text"),
    "vials_frozen": ("vials_frozen", "int"), "discard_reason": ("discard_reason", "text"),
    "notes": ("notes", "text"),
}
# Inventory: only storage_type + the physical location fields are editable. The
# polymorphic parent links (antibody / cell_line / vial) and site are export-only
# reference, so a location can never be re-pointed at a different item via upload.
_EDITABLE_INVENTORY = {
    "storage_type": ("storage_type", "text"),
    "building": ("building", "text"), "room": ("room", "text"),
    "freezer": ("freezer", "text"), "shelf": ("shelf", "text"),
    "rack": ("rack", "text"), "box": ("box", "text"),
    "position": ("position", "text"), "notes": ("notes", "text"),
}

# family key -> (editable map, Model, sheet title)
_SCALAR_FAMILIES = {
    "targets":    (_EDITABLE_TARGETS, Target, SHEET_TARGETS),
    "cell_lines": (_EDITABLE_CELL_LINES, CellLine, SHEET_CELL_LINES),
    "samples":    (_EDITABLE_SAMPLES, None, "Samples"),   # Model resolved lazily
}

_TRUE = {"yes", "y", "true", "t", "1", "wt+", "on"}
_FALSE = {"no", "n", "false", "f", "0", "off"}


def _sample_model():
    from pipeline.models import Sample
    return Sample


def _coerce(kind, raw):
    """(ok, value). ok=False means blank/unparseable → skip (blank never clears)."""
    s = ("" if raw is None else str(raw)).strip()
    if s == "":
        return False, None
    if kind == "text":
        return True, s
    if kind == "bool":
        low = s.lower()
        if low in _TRUE:
            return True, True
        if low in _FALSE:
            return True, False
        return False, None
    if kind == "int":
        try:
            return True, int(float(s))
        except (TypeError, ValueError):
            return False, None
    if kind == "decimal":
        try:
            return True, Decimal(s)
        except (InvalidOperation, TypeError, ValueError):
            return False, None
    if kind == "date":
        try:
            return True, _date.fromisoformat(s[:10])
        except (TypeError, ValueError):
            return False, None
    return False, None


def _disp(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _classify_scalar(obj, model_attr, kind, raw):
    """None (skip) or (kind_of_change, model_attr, old_disp, new_disp, new_val).
    kind_of_change is 'fill' or 'overwrite'."""
    ok, new_val = _coerce(kind, raw)
    if not ok:
        return None                          # blank / unparseable → never clears
    old_val = getattr(obj, model_attr, None)
    if kind == "bool":
        if bool(old_val) == bool(new_val):
            return None
        return ("overwrite", model_attr, _disp(old_val), _disp(new_val), new_val)
    old_blank = old_val is None or (isinstance(old_val, str) and old_val.strip() == "")
    if not old_blank and _same_value(old_val, new_val):
        return None
    change = "fill" if old_blank else "overwrite"
    return (change, model_attr, _disp(old_val), _disp(new_val), new_val)


def _same_value(a, b):
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except (InvalidOperation, TypeError, ValueError):
            return str(a) == str(b)
    return str(a) == str(b)


def _index_editable(header, editable):
    """{model_attr: (col_index, kind, label)} for the editable columns present."""
    out = {}
    for i, h in enumerate(header):
        key = _norm_col(h)
        if key in editable:
            attr, kind = editable[key]
            out[attr] = (i, kind, key)
    return out


def _parse_scalar_sheet(rows, editable):
    """[{id, values:{model_attr:(raw,kind,label)}}] from a sheet's rows."""
    out = []
    if len(rows) < 2:
        return out
    header = _cells(rows[0])
    id_idx = _col_index(header, "id")
    idx = _index_editable(header, editable)
    if not idx:
        return out
    for raw in rows[1:]:
        cells = _cells(raw)
        if not any(cells):
            continue
        rec_id = _int_or_none(cells[id_idx]) if id_idx is not None and id_idx < len(cells) else None
        values = {}
        for attr, (i, kind, label) in idx.items():
            if i < len(cells) and cells[i].strip() != "":
                values[attr] = (cells[i], kind, label)
        if values:
            out.append({"id": rec_id, "values": values})
    return out


def _plan_scalar_family(records, editable, Model):
    creates, fills, overwrites, blocked = [], [], [], []
    for row in records:
        rec_id = row["id"]
        if not rec_id:
            blocked.append({"label": "(row)", "reason": "no id — edit these by id; create on the dedicated page"})
            continue
        obj = Model.objects.using(DB).filter(pk=rec_id).first()
        if obj is None:
            blocked.append({"label": f"#{rec_id}", "reason": "no record with that id"})
            continue
        label = _obj_label(obj)
        for attr, (raw, kind, flabel) in row["values"].items():
            res = _classify_scalar(obj, attr, kind, raw)
            if not res:
                continue
            change, _attr, old_disp, new_disp, _new = res
            item = {"label": label, "field": flabel, "old": old_disp, "new": new_disp}
            (fills if change == "fill" else overwrites).append(item)
    return {"creates": creates, "fills": fills, "overwrites": overwrites, "blocked": blocked}


def _apply_scalar_family(records, editable, Model, apply_overwrites):
    updated = 0
    for row in records:
        rec_id = row["id"]
        if not rec_id:
            continue
        obj = Model.objects.using(DB).filter(pk=rec_id).first()
        if obj is None:
            continue
        changed = False
        for attr, (raw, kind, _flabel) in row["values"].items():
            res = _classify_scalar(obj, attr, kind, raw)
            if not res:
                continue
            change, _attr, _old, _new_disp, new_val = res
            if change == "overwrite" and not apply_overwrites:
                continue
            setattr(obj, attr, new_val)
            changed = True
        if changed:
            obj.save(using=DB)
            updated += 1
    return updated


def _obj_label(obj):
    if obj.__class__.__name__ == "InventoryLocation":
        head = " / ".join(x for x in (_inv_gene(obj), _inv_parent_label(obj)) if x)
        return f"{head} — {obj}" if head else str(obj)
    gene = ""
    if getattr(obj, "target_id", None) and getattr(obj, "target", None):
        gene = obj.target.gene_name or ""
    elif hasattr(obj, "gene_name"):
        gene = obj.gene_name or ""
    elif getattr(obj, "cell_line_id", None) and getattr(obj, "cell_line", None):
        gene = obj.cell_line.name or ""
    name = getattr(obj, "name", None) or getattr(obj, "catalogue_number", None) or f"#{obj.pk}"
    return f"{gene}: {name}" if gene and gene != name else str(name)


def _antibody_extra_diffs(ab, extras):
    """(fills, overwrites) for an antibody's extra scalar columns."""
    fills, overwrites = [], []
    for attr, (raw, kind, flabel) in extras.items():
        res = _classify_scalar(ab, attr, kind, raw)
        if not res:
            continue
        change, _attr, old_disp, new_disp, _new = res
        item = {"label": _obj_label(ab), "field": flabel, "old": old_disp, "new": new_disp}
        (fills if change == "fill" else overwrites).append(item)
    return fills, overwrites


def _apply_antibody_extras(ab, extras, apply_overwrites):
    changed = False
    for attr, (raw, kind, _flabel) in extras.items():
        res = _classify_scalar(ab, attr, kind, raw)
        if not res:
            continue
        change, _attr, _old, _new_disp, new_val = res
        if change == "overwrite" and not apply_overwrites:
            continue
        setattr(ab, attr, new_val)
        changed = True
    return changed


def _extras_from_row(header, cells):
    """{model_attr: (raw, kind, label)} for antibody extra columns in a row."""
    out = {}
    for i, h in enumerate(header):
        key = _norm_col(h)
        if key in _EDITABLE_ANTIBODY_EXTRA and i < len(cells) and cells[i].strip() != "":
            attr, kind = _EDITABLE_ANTIBODY_EXTRA[key]
            out[attr] = (cells[i], kind, key)
    return out
