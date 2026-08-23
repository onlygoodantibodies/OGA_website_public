"""Record experiment results from a filled-in bench sheet.

The companion to ``services/planning.py``: a session's bench sheet is printed,
filled in at the bench, then uploaded here to record results. Rows are matched
back to antibodies by the ``Ab#`` key column (falling back to catalogue number),
and the filled result columns are written to the session's procedure result rows
(``WbResult`` / ``IpResult`` / ``IfResult`` / ``FcResult``) — upserting one row
per antibody. ``plan()`` previews; ``apply()`` writes.
"""
from __future__ import annotations

import re

from django.db import transaction

from pipeline.models import Antibody
from pipeline.services import lab_numbers
from pipeline.services import sessions as sess
from pipeline.services import workbook as wbk
from pipeline.services.session_import import CONDITION_JOIN

DB = "pipeline_db"

# Header label (lower-cased) → result field, per procedure. Covers both the
# labels planning.py writes and the variants on Riham's original sheets.
RESULT_HEADER_ALIASES = {
    "WB": {
        "used dilution": "dilution", "wb used dilution": "dilution", "dilution": "dilution",
        # One measurement, one field. `WbResult` has `primary_ab_dilution` too and
        # it means the same thing — see `session_template.RESULT_COL_ALIASES`. A
        # sheet naming it must land where the report reads, not in the duplicate.
        "primary ab dilution": "dilution", "primary_ab_dilution": "dilution",
        "signal": "signal", "rating": "rating",
        "exposure time": "exposure_time", "exposure": "exposure_time",
    },
    "IP": {
        "ab amount (µl)": "amount_of_antibody", "ab amount (ul)": "amount_of_antibody",
        "ip (v in µl)": "amount_of_antibody", "ip (v in ul)": "amount_of_antibody",
        "amount of ab": "amount_of_antibody",
        "lysate (µg)": "amount_of_lysate", "lysate (ug)": "amount_of_lysate",
        "amount of lysate": "amount_of_lysate",
        "enrichment": "enrichment", "ip assessment": "ip_assessment",
        "sm assessment": "sm_assessment", "ub assessment": "ub_assessment",
    },
    "IF": {
        "used dilution": "primary_ab_dilution", "primary ab dilution": "primary_ab_dilution",
        "best concentration": "best_concentration", "specific signal": "specific_signal",
        "permeabilisation": "permeabilisation", "permeabilization": "permeabilisation",
        "plate number": "plate_number", "well number": "well_number",
    },
    "FC": {
        "used concentration": "concentration", "concentration": "concentration",
        "histogram shift": "histogram_shift",
        "mfi wt": "median_fluorescence_wt", "mfi ko": "median_fluorescence_ko",
        "gating strategy": "gating_strategy", "gating": "gating_strategy",
    },
}

_KEY_HEADERS = {"ab#", "ab #", "antibody number", "antibodynumber", "ab number"}
_CAT_HEADERS = {"catnumber", "cat number", "catalogue", "catalogue number", "cat #", "cat#"}
_COMPANY_HEADERS = {"company", "supplier", "vendor"}
# IF fields that are aggregated (many well rows → one result row per antibody).
_IF_JOIN_FIELDS = {"primary_ab_dilution", "specific_signal"}

# Identity columns the sheet ships that are context, not readings, and not a
# scientist's own invention. Everything else unrecognised is treated the way the
# workbook treats it — as a session condition — rather than dropped.
_CONTEXT_HEADERS = ({"gene", "tube", "clonality", "clone", "host",
                     "conc.", "conc", "concentration",
                     "conc. (µg/ml)", "conc. (ug/ml)", "conc. (mg/ml)",
                     "wb recommended dilution", "recommended dilution",
                     "plate number", "well number", "name of histogram"}
                    | _KEY_HEADERS | _CAT_HEADERS | _COMPANY_HEADERS)


def _distinctive_headers():
    """Per procedure, the column labels that belong to *only* that procedure's sheet.

    Derived from ``RESULT_HEADER_ALIASES`` — the parser's own contract — rather
    than typed out, so widening what a procedure reads cannot leave a second list
    behind saying something else.

    Two exclusions, both of which stop a false accusation. A label several
    procedures read (``used dilution`` is WB's and IF's) says nothing about which
    sheet this is. And a label that is also an identity column (``concentration``
    sits in FC's aliases *and* in ``_CONTEXT_HEADERS`` as the Conc. column every
    sheet ships) would make a hand-made WB sheet with a bare Concentration column
    look like a flow cytometry one.
    """
    procs_by_header = {}
    for proc, aliases in RESULT_HEADER_ALIASES.items():
        for h in aliases:
            procs_by_header.setdefault(h, set()).add(proc)
    out = {proc: set() for proc in RESULT_HEADER_ALIASES}
    for h, procs in procs_by_header.items():
        if len(procs) == 1 and h not in _CONTEXT_HEADERS:
            out[next(iter(procs))].add(h)
    return out


DISTINCTIVE_HEADERS = _distinctive_headers()


def sheet_procedure(headers):
    """Which procedure's bench sheet this is, read from the columns it ships.

    ``None`` when the sheet does not say — either it carries nothing distinctive
    (a hand-made sheet of ``Ab#`` and ``Used dilution`` belongs to no procedure in
    particular) or it carries two procedures' worth, which is somebody's own
    combined sheet rather than one of ours. Both mean *don't refuse*: this answer
    is only ever used to catch a file that is unmistakably something else.
    """
    named = [proc for proc, distinctive in DISTINCTIVE_HEADERS.items()
             if distinctive & {_norm(h).lower() for h in headers}]
    return named[0] if len(named) == 1 else None


def _read_rows(f):
    """Uploaded .xlsx / .csv / .tsv → list of cell-value lists.

    Through ``services/workbook.py``, so the sheet is chosen by what is in it
    rather than by which tab Excel had selected when the file was saved. This
    read ``wb.active``: a scientist who added a notes tab to a printed bench
    sheet and saved with it in front uploaded the notes.
    """
    return wbk.read(f, recognise=wbk.header_scorer(_KEY_HEADERS | _CAT_HEADERS)).rows


def _norm(v):
    return ("" if v is None else str(v)).strip()


def _condition_key(header):
    """The key an unrecognised column is stored under in ``session_conditions``.

    Snake-cased, so it lands in the same namespace as every other condition and
    the sessions board can round-trip it. Same rule, same spelling, as
    ``session_import._condition_key`` — one convention across both importers, or
    a column named `Owner notes` arrives once as ``owner_notes`` and once as
    ``"owner notes"`` and nothing can read both.
    """
    return "_".join(str(header or "").strip().lower().split())


def _sheet_session_id(rows):
    """The session a printed sheet says it belongs to, or None.

    A bench sheet is printed, carried around and typed up days later, and the
    only thing tying the paper to the record was the person holding it. Written
    by ``planning._session_info_line``; read here so uploading Monday's sheet
    into Wednesday's session is caught rather than recorded.
    """
    for r in rows[:6]:
        for c in r:
            m = re.search(r"session\s*#(\d+)", _norm(c), re.I)
            if m:
                return int(m.group(1))
    return None


def _fold_condition_values(rows, headers):
    """``{header: [distinct values, in the order the sheet gives them]}``.

    One reader for the preview and for the write, so what the panel says will be
    kept is what is kept.

    A condition belongs to the session, so a column the sheet did not ship cannot
    become one condition per row. It used to become the **first** row's value —
    and on a plate map the first row is not a summary of the plate, it is the
    first well: a nine-well `Specific signal` column arrived as `Yes` and the
    other eight readings were gone, with a line underneath admitting it. Every
    distinct value is kept and joined instead, the same way ``_IF_JOIN_FIELDS``
    already joins a reading repeated across one antibody's wells. A joined list
    is visibly not a single reading; an arbitrary single value is
    indistinguishable from a real one, which is the worse half.
    """
    out = {}
    for h in headers:
        vals = []
        for r in rows:
            v = (r.get("extra") or {}).get(h, "")
            if v and v not in vals:
                vals.append(v)
        out[h] = vals
    return out


def parse(f, procedure):
    """Read a filled bench sheet into normalised rows for the procedure.

    Returns ``{"rows": [...], "unknown": [...], "sheet_session_id": n|None,
    "sheet_procedure": "WB"|…|None}``, where a row is
    ``{"ab_key","catalogue","company","fields":{result_field: value},"extra":{}}``.
    Title/control rows are skipped. Raises ValueError on an unreadable or
    header-less sheet.

    ``extra`` holds the columns this sheet did not ship. They used to be dropped
    without a word: the seventh field test added `Owner notes` with a different
    page reference on each of three rows, got *"3 field(s) filled in"* three
    times, no mention of a fourth column, and `session_conditions == {}`
    afterwards. The workbook had already learned to name such a column and say
    what it would keep; this surface had not, and it is the one where the loss
    was total. Carried on the row so it survives the JSON round trip to the
    commit endpoint.
    """
    proc = (procedure or "").upper()
    aliases = RESULT_HEADER_ALIASES.get(proc, {})
    rows = _read_rows(f)
    if not rows:
        raise ValueError("the sheet is empty")

    # Find the header row: the first row that names the Ab# key or a catalogue col.
    header_idx = None
    for i, r in enumerate(rows[:15]):
        cells = {_norm(c).lower() for c in r}
        if cells & _KEY_HEADERS or cells & _CAT_HEADERS:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("couldn't find the header row (no 'Ab#' or 'CatNumber' column)")

    header = [_norm(c).lower() for c in rows[header_idx]]
    col = {h: j for j, h in enumerate(header) if h}

    def _get(cells, names):
        for n in names:
            if n in col and col[n] < len(cells):
                v = _norm(cells[col[n]])
                if v:
                    return v
        return ""

    # Columns that are neither a reading this procedure knows nor part of the
    # sheet's own identity block. Same rule as the workbook: keep them, as
    # session conditions, and say so before anything is written.
    extra_headers = [h for h in header
                     if h and h not in aliases and h not in _CONTEXT_HEADERS]

    out = []
    for cells in rows[header_idx + 1:]:
        if not any(_norm(c) for c in cells):
            continue
        ab_key = _get(cells, _KEY_HEADERS)
        catalogue = _get(cells, _CAT_HEADERS)
        if not ab_key and not catalogue:
            continue  # control / blank row
        company = _get(cells, _COMPANY_HEADERS)
        fields = {}
        for h, field in aliases.items():
            if h in col and col[h] < len(cells):
                v = _norm(cells[col[h]])
                if v:
                    fields[field] = v
        extra = {}
        for h in extra_headers:
            if col[h] < len(cells):
                v = _norm(cells[col[h]])
                if v:
                    extra[h] = v
        out.append({"ab_key": ab_key, "catalogue": catalogue,
                    "company": company, "fields": fields, "extra": extra})

    # A condition belongs to the session, so several rows' values have to share
    # one. They are joined rather than thinned to the first — see
    # `_fold_condition_values` — and the preview says how many went into it.
    unknown = []
    for h, values in _fold_condition_values(out, extra_headers).items():
        if not values:
            continue
        unknown.append({
            "header": h,
            "key": _condition_key(h),
            "value": CONDITION_JOIN.join(values),
            "values": len(values),
            "rows": sum(1 for r in out if r["extra"].get(h, "")),
        })

    return {"rows": out, "unknown": unknown,
            "sheet_session_id": _sheet_session_id(rows),
            "sheet_procedure": sheet_procedure(header)}


def _resolve_ab(target, ab_key, catalogue, company, session=None):
    """The antibody one bench-sheet row is about.

    The ``Ab#`` cell used to be a *record* id and this was the exact key it
    matched on. It is the A-number or blank now, because printing a record id
    under that heading told a reader their vial's number was 4550 when the
    board said it had none — so the ordinary case is a row with a supplier and
    a catalogue number and, for anything Leicester recorded before A-numbers
    were issued, nothing in the number cell.

    That is not a weaker key. A bench sheet's rows **are** the session's result
    rows, so the session's own antibodies are the set this is choosing from;
    matching within it is narrower than the ``.first()`` over every vial of the
    gene that the catalogue branch used to fall back to, which could pick
    another site's vial of the same product.
    """
    qs = Antibody.objects.using(DB).filter(target=target)
    k = _norm(ab_key)
    if k:
        # `A-118` is the lab's own number; a bare integer is the record id this
        # column has always carried. Only the prefix makes it the former — a
        # sheet downloaded before numbers were issued again holds record ids
        # here, and guessing they were A-numbers would resolve to a different
        # antibody with the same digits. `services/lab_numbers.py` is the one
        # reader for both spellings.
        kind, number = lab_numbers.read_reference(ab_key)
        if kind == "number":
            ab = qs.filter(ab_number=number).first()
            if ab:
                return ab
        ab = qs.filter(access_id=k).first()
        if not ab and k.isdigit():
            ab = qs.filter(pk=int(k)).first()
        if ab:
            return ab
    cat = _norm(catalogue)
    if cat:
        f = qs.filter(catalogue_number__iexact=cat)
        if _norm(company):
            f = f.filter(company__name__icontains=_norm(company))
        # This session's own vials first. Two sites can hold the same product
        # against the same gene, and the row on the sheet is about the one this
        # session was run with — picking whichever was created first would file
        # a reading against another bench's vial.
        if session is not None:
            mine = [a for a in f if a.pk in _session_antibody_ids(session)]
            if mine:
                return mine[0]
        return f.first()
    return None


def _session_antibody_ids(session) -> set:
    """The antibodies this session actually tested — one per result row.

    The same set ``planning._session_antibodies`` prints the sheet from, so what
    a row can resolve to is what a row could have been printed for. Cached on
    the session object: ``_collapse`` asks once per row.
    """
    cached = getattr(session, "_oga_ab_ids", None)
    if cached is None:
        from pipeline.services.planning import _session_antibodies
        cached = {a.pk for a in _session_antibodies(session)}
        session._oga_ab_ids = cached
    return cached


def _collapse(target, rows, proc, session=None):
    """Group parsed rows by antibody, keeping only fields valid for the procedure.
    IF (well-based) joins repeated per-antibody values; others take last-wins."""
    allowed = sess._result_field_names(proc)
    by_ab = {}
    order = []
    unmatched = []
    for r in rows:
        if not r["fields"]:
            continue  # nothing filled in on this row
        ab = _resolve_ab(target, r["ab_key"], r["catalogue"], r["company"],
                         session=session)
        if not ab:
            ref = r["ab_key"] or r["catalogue"]
            if ref and ref not in unmatched:
                unmatched.append(ref)
            continue
        if ab.pk not in by_ab:
            by_ab[ab.pk] = {"ab": ab, "fields": {}}
            order.append(ab.pk)
        acc = by_ab[ab.pk]["fields"]
        for field, val in r["fields"].items():
            if field not in allowed:
                continue
            if proc == "IF" and field in _IF_JOIN_FIELDS and acc.get(field):
                if val not in acc[field].split(CONDITION_JOIN):
                    acc[field] = f"{acc[field]}{CONDITION_JOIN}{val}"
            else:
                acc[field] = val
    return [by_ab[pk] for pk in order], unmatched


def wrong_session(session, sheet_session_id) -> str:
    """The refusal for a sheet stamped with a different session's number.

    Not a warning — a refusal. Two bench sheets for one gene differ only in the
    readings written on them, so typing Monday's into Wednesday's session is a
    single wrong click that produces a completely plausible-looking record. The
    stamp is on the sheet precisely so the app can catch it.
    """
    if not sheet_session_id or sheet_session_id == session.pk:
        return ""
    return (f"This sheet was downloaded for session #{sheet_session_id}, "
            f"but you are recording into session #{session.pk}. Nothing has been "
            f"read from it. Open session #{sheet_session_id} and upload it there, "
            f"or download this session's own sheet.")


def _procedure_label(session, proc):
    return dict(session.ProcedureType.choices).get(proc, proc)


def wrong_procedure(session, proc) -> str:
    """The refusal for a sheet that is unmistakably another procedure's.

    The stamp is the precise guard and this is the one that still works without
    it: sheets downloaded before the IF plate map carried a session number have
    nothing for `wrong_session` to read, and a sheet somebody typed themselves
    never will. It is also the guard that catches *what* is wrong rather than
    only *which session* — feeding an IF plate map to a western blot matched all
    three antibodies and read the plate's `Used dilution` (`1in500_T`) as a WB
    dilution, because that one label is shared.

    Silent when the sheet does not clearly name a procedure: this refuses files
    that are obviously something else, never files it merely cannot place.
    """
    if not proc or proc == session.procedure_type:
        return ""
    theirs = _procedure_label(session, proc)
    mine = _procedure_label(session, session.procedure_type)
    return (f"This is a {theirs} sheet — its columns are {theirs} readings — but "
            f"session #{session.pk} is a {mine} session. Nothing has been read "
            f"from it. Open the {theirs} session for this gene and upload it "
            f"there, or download this session's own {mine} bench sheet.")


def unstamped_note(session, sheet_session_id) -> str:
    """What to say about a sheet carrying no session number at all.

    Not a refusal: a sheet typed from scratch, or downloaded before the IF plate
    map carried a stamp, is a legitimate thing to upload and there is nothing
    wrong with it. But silence here is what the panel's own promise — *both files
    carry this session's number* — turns into a false reassurance, so the one
    case where that promise cannot be checked says so.
    """
    if sheet_session_id:
        return ""
    return (f"This sheet carries no session number, so there is no way to check "
            f"it was downloaded for session #{session.pk}. Sheets downloaded from "
            f"this panel carry one — if this is an older download, or two sessions "
            f"of this gene are open, download this session's own sheet and check "
            f"the readings against it before recording.")


def plan(session, parsed):
    """Preview: which antibodies get results, create vs update, unmatched
    references, and the columns the sheet did not ship.

    Accepts the dict `parse` returns, or a bare row list from an older caller.
    """
    rows, unknown, sheet_id, sheet_proc = _unpack(parsed)
    refusal = wrong_session(session, sheet_id) or wrong_procedure(session, sheet_proc)
    if refusal:
        return {"ok": False, "error": refusal}

    proc = session.procedure_type
    ResultModel = sess.RESULT_MODEL_MAP[proc]
    collapsed, unmatched = _collapse(session.target, rows, proc, session=session)
    items = []
    for entry in collapsed:
        ab = entry["ab"]
        exists = ResultModel.objects.using(DB).filter(session=session, antibody=ab).exists()
        items.append({
            "antibody": str(ab),
            "action": "update" if exists else "create",
            "fields": entry["fields"],
        })
    return {
        "ok": True,
        "procedure": proc,
        "items": items,
        "unmatched": unmatched,
        "unknown_columns": unknown,
        # Not an error — the panel draws it above the rows, because it is about
        # whether these readings belong here at all.
        "unstamped": unstamped_note(session, sheet_id),
        "summary": {
            "matched": len(items),
            "create": sum(1 for i in items if i["action"] == "create"),
            "update": sum(1 for i in items if i["action"] == "update"),
            "unmatched": len(unmatched),
            "unknown_columns": len(unknown),
        },
    }


def _unpack(parsed):
    """`(rows, unknown, sheet_session_id, sheet_procedure)` from either shape
    `parse` has had.

    The commit endpoint receives rows back as JSON from the browser, so both
    callers hand this whatever they are holding.
    """
    if isinstance(parsed, dict):
        return (parsed.get("rows") or [],
                parsed.get("unknown") or [],
                parsed.get("sheet_session_id"),
                parsed.get("sheet_procedure"))
    return list(parsed or []), [], None, None


def apply(session, parsed, member=None):
    """Upsert one result row per matched antibody with the filled fields, and
    fold the sheet's own columns into the session's conditions."""
    rows, _unknown, sheet_id, sheet_proc = _unpack(parsed)
    refusal = wrong_session(session, sheet_id) or wrong_procedure(session, sheet_proc)
    if refusal:
        return {"ok": False, "error": refusal}

    proc = session.procedure_type
    ResultModel = sess.RESULT_MODEL_MAP[proc]
    collapsed, unmatched = _collapse(session.target, rows, proc, session=session)
    created, updated = [], []
    with transaction.atomic(using=DB):
        # A column the sheet did not ship is a session condition — the rule the
        # workbook already holds. Only the named keys are touched, so conditions
        # recorded elsewhere survive: rebuilding this dict from scratch is how
        # the full-page save used to wipe a workbook's own columns.
        # Through the same fold the preview used, so the panel's list of what
        # would be kept is what lands. Merged by *key* rather than by header,
        # because two spellings of one column name snake_case to one condition
        # and the second must not silently replace the first.
        headers = []
        for r in rows:
            for h in (r.get("extra") or {}):
                if h not in headers:
                    headers.append(h)
        stored = {}
        for h, values in _fold_condition_values(rows, headers).items():
            if not values:
                continue
            key = _condition_key(h)
            merged = stored[key].split(CONDITION_JOIN) if key in stored else []
            merged += [v for v in values if v not in merged]
            stored[key] = CONDITION_JOIN.join(merged)
        if stored:
            conditions = dict(session.session_conditions or {})
            conditions.update(stored)
            session.session_conditions = conditions
            session.save(using=DB, update_fields=["session_conditions", "updated_at"])

        for entry in collapsed:
            ab, fields = entry["ab"], entry["fields"]
            if not fields:
                continue
            row = ResultModel.objects.using(DB).filter(session=session, antibody=ab).first()
            is_new = row is None
            if is_new:
                row = ResultModel(session=session, antibody_id=ab.pk)
            for field, val in fields.items():
                setattr(row, field, val)
            row.save(using=DB)
            (created if is_new else updated).append(str(ab))
    return {"ok": True, "created": created, "updated": updated,
            "unmatched": unmatched, "conditions_stored": sorted(stored)}
