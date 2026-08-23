"""
Session template upload (write-back for the per-gene sessions tool).

Reverse of ``session_template``: read a filled workbook (a tab per application),
and for each tab create **one shared ExperimentSession** for the gene plus one
per-antibody result row (WbResult / IpResult / IfResult / FcResult). Columns that
are neither context nor result fields are folded into ``session_conditions``.

Guarantees, mirroring the rest of the tool:
  * **Create only, and only on explicit commit** — ``plan_import`` is read-only
    and lists exactly what would be created; ``apply_import`` writes in one
    transaction. Re-uploading creates new sessions (it is additive by nature), so
    the preview makes the count obvious before anything is written.
  * **Nothing is edited or deleted** here — this path only *adds* sessions and
    results. Editing existing sessions is the separate round-trip export/upload.
  * A row whose antibody can't be matched in the gene is **skipped and reported**,
    never guessed.

Members only. Writes ``pipeline_db``.
"""
from __future__ import annotations

import re
from datetime import date as _date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from pipeline.models import (Target, Member, ExperimentSession,
                             WbResult, IpResult, IfResult, FcResult)
from pipeline.services import session_template as tmpl
from pipeline.services.cropper import db as cdb

DB = "pipeline_db"

PROCEDURES = tmpl.PROCEDURES
_CONTEXT_COLS = set(tmpl._CONTEXT_COLS)
_RESULT_COLS = tmpl._RESULT_COLS
CONDITION_PREFIX = tmpl.CONDITION_PREFIX
_RESULT_MODEL = {"WB": WbResult, "IP": IpResult, "IF": IfResult, "FC": FcResult}

# How several rows' worth of one invented column are written into the single
# session condition they have to share. Imported by ``bench_results`` so the two
# importers spell it one way — the same reason ``_condition_key`` is the same
# function twice. See ``_group`` for why joining rather than taking the first.
CONDITION_JOIN = " | "

# Result fields that are DecimalFields on their model (everything else is text).
_DECIMAL_RESULT = {
    "IF": {"wt_ko_ratio_1", "wt_ko_ratio_2"},
    "FC": {"median_fluorescence_wt", "median_fluorescence_ko"},
}


def _norm(h) -> str:
    return str(h or "").strip().lower()


def parse_template(f) -> dict:
    """Read a filled template into {procedure: {"header": [...], "rows": [ {header: cell} ]}}.
    Only the WB/IP/IF/FC tabs are read; blank rows are dropped. Raises ValueError on
    an unreadable workbook."""
    import openpyxl
    try:
        f.seek(0)
    except Exception:
        pass
    try:
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"could not read the workbook ({e})")

    out = {}
    for proc in PROCEDURES:
        if proc not in wb.sheetnames:
            continue
        ws = wb[proc]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if len(rows) < 2:
            continue
        header = ["" if c is None else str(c).strip() for c in rows[0]]
        data = []
        for raw in rows[1:]:
            cells = ["" if c is None else str(c).strip() for c in raw]
            if not any(cells):
                continue
            data.append({header[i]: (cells[i] if i < len(cells) else "")
                         for i in range(len(header))})
        if data:
            out[proc] = {"header": header, "rows": data}
    return out


def _get(row, key):
    """Case-insensitive column read from a row dict."""
    for k, v in row.items():
        if _norm(k) == key:
            return (v or "").strip()
    return ""


def _result_cols(proc) -> set:
    """Every header this sheet reads as a reading — the current columns and the
    ones they replaced.

    A retired column has to stay in this set or the rule below promotes it to a
    session condition: `primary_ab_dilution` is `dilution` on a WB tab
    (``session_template.RESULT_COL_ALIASES``), and every workbook downloaded
    before that merge still carries the old header.
    """
    return set(_RESULT_COLS[proc]) | set(tmpl.RESULT_COL_ALIASES.get(proc, {}))


def _get_result(row, proc, col) -> str:
    """One reading, taken from its column or from the column it replaced."""
    for name in [col, *tmpl.aliases_for(proc, col)]:
        val = _get(row, name)
        if val:
            return val
    return ""


def _condition_cols(proc, header):
    """Header columns that carry session conditions.

    A ``cond:`` prefix says so outright, and is what the template writes — a
    condition named after the result field it describes (IP's ``bead_type``)
    otherwise appeared twice in one header row and lost its value to the blank
    result column beside it.

    Anything else that is neither a context nor a result column is still read as
    a condition, so sheets downloaded before the prefix existed — and the
    invented columns scientists add — keep working.
    """
    res = _result_cols(proc)
    return [h for h in header
            if _norm(h) and (_norm(h).startswith(CONDITION_PREFIX)
                             or (_norm(h) not in _CONTEXT_COLS and _norm(h) not in res))]


def _unrecognised_cols(proc, header):
    """The condition columns the workbook did not ship — a scientist's own.

    They are still kept (that is the rule above), but they are kept as *session*
    conditions — one value for the whole tab. A column added per antibody used to
    lose every value but the first, silently: the sixth field test added
    ``Owner notes`` with a different page reference on each of three rows and got
    one of them, with the preview saying nothing and the save message saying
    nothing. ``_group`` joins them now, so nothing typed disappears.

    Naming them is still what lets the preview say so before anything is written:
    a column read as a condition is a column that was not read as a reading.
    """
    res = _result_cols(proc)
    return [h for h in header
            if _norm(h) and not _norm(h).startswith(CONDITION_PREFIX)
            and _norm(h) not in _CONTEXT_COLS and _norm(h) not in res]


def _has_result(row, proc) -> bool:
    """Did anybody write a reading on this row?

    The workbook ships **all four tabs** pre-filled with the gene's antibodies,
    because you cannot know in advance which procedure the week will bring. So a
    scientist who runs a western blot, fills the WB tab and uploads the file is
    handing back three other tabs still carrying their context rows — and every
    one of those used to become an ``ExperimentSession`` marked *complete*, with a
    blank result row against a real antibody.

    That is the bench-sheet rule one surface over: a row on a sheet is an
    invitation to write a reading on it, and an extra row becomes a result for an
    experiment nobody ran. A tab with nothing written on it is not a session.
    """
    return any(_get_result(row, proc, col) != "" for col in _RESULT_COLS[proc])


def _condition_key(header):
    """The name a condition column is stored under: its prefix is spelling, not
    part of the key, so ``cond:bead_type`` and a legacy ``bead_type`` land in the
    same place in ``session_conditions``.

    Spaces become underscores, because every key the registry knows is
    snake_case and a promoted column arrived as ``"owner notes"`` — the one key
    in the dict that nothing round-tripping by key would survive. No effect on a
    ``cond:`` column, which is snake_case already.
    """
    h = _norm(header)
    h = h[len(CONDITION_PREFIX):] if h.startswith(CONDITION_PREFIX) else h
    return "_".join(h.split())


def _resolve_member(name, uploader):
    """Match an experimenter by display name, else fall back to the uploader."""
    n = (name or "").strip()
    if n:
        m = (Member.objects.using(DB).filter(display_name__iexact=n, is_active=True).first()
             or Member.objects.using(DB).filter(display_name__icontains=n, is_active=True).first())
        if m:
            return m
    return uploader


def _resolve_cell_line(target, name, *, genotype=None, site_id=None):
    """`(line, error)` for a cell-line name off a workbook row.

    This looked in ``target.cell_lines``, and **a wild type has no gene**, so
    that query cannot return one — ever. The workbook ships ``cell_line_wt``
    pre-filled with the parental's name on every row of every tab
    (``session_template._tab_plan`` resolves it through the knockout's
    ``parent_line``), and this threw it away on the way back in: three sessions
    created from one upload, all with a blank wild type, on a board whose whole
    subject is the WT-versus-KO comparison. Two paragraphs later it surfaced as
    three literal ``[WT cell line]`` placeholders in a generated Data Note.

    The fourth reader to assume a gene filter can find a parental. It goes
    through ``services/cell_lines.py`` now, like every other one.
    """
    from pipeline.services import cell_lines as clines
    n = (name or "").strip()
    if not n:
        return None, None
    return clines.resolve(n, genotype=genotype, site_id=site_id)


def _line_label(line):
    from pipeline.services import cell_lines as clines
    return clines.label(line)


def _resolve_antibody(target, company, catalogue):
    cat = (catalogue or "").strip()
    if not cat:
        return None
    ab = cdb.find_antibody(target, company or "", cat)
    if ab:
        return ab
    return target.antibodies.using(DB).filter(catalogue_number__iexact=cat).first()


def _parse_date(raw):
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return _date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None


_SESSION_REF_ID = re.compile(r"#(\d+)\s*$")


def _parse_session_ref(value):
    """The session id a ``session_ref`` names, or None for a ``(new)`` tab.

    ``WB · ELP3 #480`` → 480. This one number is the whole create-versus-fill-in
    decision, which is why it is written where a person can read it before
    uploading a file they filled in three days ago.
    """
    m = _SESSION_REF_ID.search(str(value or "").strip())
    return int(m.group(1)) if m else None


def _target_session(session_id, proc, gene):
    """``(session, error)`` for a ``#<id>`` ref. Never falls back to creating.

    A ref that names nothing, or names a session of the wrong procedure or the
    wrong gene, is **refused**. Falling back to "create one instead" is how a
    typo in a cell quietly becomes a duplicate session with a complete set of
    results attached to it — the same silent-wrong-write shape as resolving a
    cell line by name alone.
    """
    session = (ExperimentSession.objects.using(DB)
               .select_related("target", "experimenter", "site")
               .filter(pk=session_id).first())
    if session is None:
        return None, (f"session #{session_id} is not in the database — "
                      f"the session_ref column names a session that does not exist. "
                      f"Nothing was recorded for this tab.")
    if session.procedure_type != proc:
        return None, (f"session #{session_id} is a "
                      f"{session.get_procedure_type_display()} session, but this tab is {proc}. "
                      f"Nothing was recorded for it.")
    on_file = (session.target.gene_name or session.target.protein_name or "").strip()
    if gene and on_file and on_file.lower() != gene.strip().lower():
        return None, (f"session #{session_id} is a {on_file} session, but this tab says "
                      f"{gene}. Nothing was recorded for it.")
    return session, None


def _group(parsed):
    """One entry per (procedure, gene, session_ref) — a tab's worth of rows.

    ``unknown`` describes the columns the workbook did not ship, so the preview
    can name them: ``[{"header", "key", "value", "values", "rows"}]``, where
    ``value`` is what the one session condition will hold — every distinct value
    the column carried, joined. It used to hold the *first* row's value and count
    the rest as dropped, which is a silent choice of an arbitrary reading: a
    condition belongs to the session, but that is a reason to keep them all in
    one place, not a reason to keep one and lose eight.

    ``session_id`` is the id in the ``session_ref`` column, or None when the tab
    is stamped ``(new)``. It is part of the grouping key so a hand-assembled
    file holding both cannot merge them.
    """
    groups = []
    for proc, block in parsed.items():
        header = block["header"]
        cond_cols = _condition_cols(proc, header)
        unknown_cols = _unrecognised_cols(proc, header)
        by_key = {}
        for row in block["rows"]:
            key = (_get(row, "gene"), _parse_session_ref(_get(row, "session_ref")))
            by_key.setdefault(key, []).append(row)
        for (gene, session_id), rows in by_key.items():
            target = (Target.objects.using(DB).filter(gene_name__iexact=(gene or "").strip()).first()
                      if gene else None)
            # A `cond:` column is the workbook's own, and genuinely one value per
            # session — row 1 is where the template writes it. A column somebody
            # invented is not: it is filled in per row, and keeping the first row
            # keeps an arbitrary one. Those are folded below instead.
            unknown_set = {_norm(c) for c in unknown_cols}
            conditions = {}
            if rows:
                for c in cond_cols:
                    if _norm(c) in unknown_set:
                        continue
                    val = _get(rows[0], _norm(c))
                    if val:
                        conditions[_condition_key(c)] = val
            unknown = []
            for c in unknown_cols:
                values, filled = [], 0
                for r in rows:
                    v = _get(r, _norm(c))
                    if v:
                        filled += 1
                        if v not in values:
                            values.append(v)
                if not values:
                    continue
                key, joined = _condition_key(c), CONDITION_JOIN.join(values)
                conditions[key] = joined
                unknown.append({"header": c, "key": key, "value": joined,
                                "values": len(values), "rows": filled})
            groups.append({"proc": proc, "gene": gene, "target": target,
                           "conditions": conditions, "rows": rows,
                           "unknown": unknown, "session_id": session_id})
    return groups


def _existing_sessions(target, proc) -> dict:
    """``{"total": n, "planned": [{"id", "date"}]}`` for this gene and procedure.

    Uploading is additive by design, so a session already on file is not an
    error — but a *planned* one is very likely the one the person thought they
    were filling in, and that is worth saying before the second is created.
    """
    qs = (ExperimentSession.objects.using(DB)
          .filter(target=target, procedure_type=proc))
    planned = [{"id": s.pk, "date": str(s.date or "")}
               for s in qs.filter(status=ExperimentSession.SessionStatus.PLANNED)
               .order_by("date")[:5]]
    return {"total": qs.count(), "planned": planned}


def _preview_lines(target, rows, proc, site_id):
    """The resolved WT/KO a commit would store, and any refusal, for the preview.

    The preview printed the **typed** cell, which is the run-5 supplier bug one
    surface over: a preview is only true if it is compared against the write.
    The workbook's `cell_line_wt` was being dropped entirely, and the preview
    said `HAP1` about a session that was going to be saved with nothing.
    """
    out = {}
    for field, genotype in (("cell_line_wt", "WT"), ("cell_line_ko", "KO")):
        typed = _get(rows[0], field)
        line, err = _resolve_cell_line(target, typed, genotype=genotype, site_id=site_id)
        out[field] = _line_label(line) if line is not None else typed
        out[f"{field}_error"] = err if typed else None
    return out


def plan_import(parsed, uploader=None) -> dict:
    """Read-only preview: what a commit would create *and* what it would fill in.

    Two modes, decided per tab by the ``session_ref`` column: ``(new)`` creates a
    session, ``#480`` fills session 480 in. A ref naming a session that is not on
    file, or one of another procedure or gene, is blocked rather than quietly
    turned into a new session.
    """
    if not parsed:
        return {"ok": False, "error": "no WB/IP/IF/FC tabs with data were found in the file."}

    sessions, blocked = [], []
    for g in _group(parsed):
        proc, gene, target = g["proc"], g["gene"], g["target"]
        rows, conditions, unknown = g["rows"], g["conditions"], g["unknown"]
        if not any(_has_result(r, proc) for r in rows):
            # Before the gene check, not after: an untouched tab is the normal
            # state of three quarters of every workbook, so it is neither an
            # error nor worth a line in "not recorded" — whatever else is wrong
            # with it.
            continue
        if target is None:
            blocked.append({"label": f"{proc}: {gene or '(no gene)'}",
                            "reason": "gene is not a target in the pipeline database"})
            continue

        existing_session = None
        if g["session_id"] is not None:
            existing_session, err = _target_session(g["session_id"], proc, gene)
            if err:
                blocked.append({"label": f"{proc}: {gene} — session #{g['session_id']}",
                                "reason": err})
                continue

        matched, unmatched, will_update = 0, [], 0
        model = _RESULT_MODEL[proc]
        for row in rows:
            if not _has_result(row, proc):
                continue
            ab = _resolve_antibody(target, _get(row, "company"), _get(row, "antibody"))
            if ab:
                matched += 1
                if existing_session is not None and model.objects.using(DB).filter(
                        session=existing_session, antibody=ab).exists():
                    will_update += 1
            else:
                unmatched.append(_get(row, "antibody") or "(blank)")
        experimenter = _resolve_member(_get(rows[0], "experimenter"), uploader)
        site_id = (existing_session.site_id if existing_session is not None
                   else getattr(experimenter, "site_id", None))
        entry = {
            "procedure": proc, "gene": gene,
            "mode": "update" if existing_session is not None else "create",
            "session_id": existing_session.pk if existing_session is not None else None,
            "results": matched,
            "results_updated": will_update,
            "results_new": matched - will_update,
            "unmatched": unmatched,
            "experimenter": (experimenter.display_name if experimenter else "(none)"),
            "conditions": len(conditions),
            "unknown_columns": unknown,
            **_preview_lines(target, rows, proc, site_id),
        }
        # **What the save will complain about, said before the save.**
        # `_preview_lines` has resolved the WT and KO since it was written and
        # reported a refusal in `cell_line_wt_error`/`cell_line_ko_error` — which
        # no panel drew. So a workbook naming a cell line this database cannot
        # place previewed as clean, and then the receipt said "2 rows skipped"
        # about two values on two tabs, with nothing to say which rows or why.
        # A preview that is silent about a refusal is a refusal deferred — the
        # rule the quick session panel already holds one surface over.
        entry["dropped"] = [
            {"label": f"{proc}: {gene}", "reason": entry[f"{field}_error"]}
            for field in ("cell_line_wt", "cell_line_ko")
            if entry.get(f"{field}_error")]
        if existing_session is not None:
            entry["status_change"] = (
                f"{existing_session.get_status_display()} → Complete"
                if existing_session.status in (ExperimentSession.SessionStatus.PLANNED,
                                               ExperimentSession.SessionStatus.IN_PROGRESS)
                else "")
        else:
            # What this gene already has for this procedure.
            #
            # A tab stamped "(new)" creates, and the sixth field test planned a WB
            # session through the wizard, downloaded the workbook from the same
            # page, filled it in, and ended up with two Western Blots: one Planned
            # with no results and one Complete with three, and no explanation on
            # either screen. Naming the planned one here is what points at the
            # session workbook instead. One query per tab, not per row.
            entry["existing"] = _existing_sessions(target, proc)
        sessions.append(entry)
        for u in unmatched:
            blocked.append({"label": f"{proc}: {gene} — {u}",
                            "reason": "antibody not found for this gene; row skipped"})

    ok_sessions = [s for s in sessions if s["results"] > 0]
    dropped = [d for s in ok_sessions for d in s["dropped"]]
    return {
        "ok": True, "error": None,
        "sessions": sessions,
        "counts": {
            "sessions": len([s for s in ok_sessions if s["mode"] == "create"]),
            "updated": len([s for s in ok_sessions if s["mode"] == "update"]),
            "results": sum(s["results"] for s in sessions),
            "blocked": len(blocked),
            "unknown_columns": sum(len(s["unknown_columns"]) for s in ok_sessions),
            # Values a commit will drop while still recording the session — the
            # same bargain `bulk_antibodies` strikes with a concentration whose
            # unit it cannot convert, and it has to be counted at the check as
            # well as named, or a reader agrees to a save they were not warned
            # about.
            "dropped": len(dropped),
        },
        "blocked": blocked,
        "dropped": dropped,
    }


def _coerce_decimal(raw):
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def apply_import(parsed, uploader=None) -> dict:
    """Write the workbook back in one transaction.

    A tab stamped ``(new)`` **creates** a session; a tab whose ``session_ref``
    carries ``#<id>`` **fills that session in**. A ref that names nothing, or
    something of another procedure or gene, is skipped and reported — never
    turned into a new session, because a mistyped number would then become a
    duplicate week of work that nobody previewed.
    """
    if not parsed:
        return {"ok": False, "error": "no WB/IP/IF/FC tabs with data were found in the file."}
    if uploader is None:
        return {"ok": False, "error": "no member profile for the uploader; cannot set the experimenter."}

    out = {"ok": True, "sessions_created": 0, "sessions_updated": 0,
           "results_created": 0, "results_updated": 0, "skipped": []}

    with transaction.atomic(using=DB):
        for g in _group(parsed):
            proc, gene, target = g["proc"], g["gene"], g["target"]
            rows, conditions = g["rows"], g["conditions"]
            # First, and for the same reason `plan_import` checks it first: a tab
            # nobody wrote on is the normal state of most of a workbook, and it
            # must not be reported as skipped work either.
            if not any(_has_result(r, proc) for r in rows):
                continue
            if target is None:
                out["skipped"].append(f"{proc}: {gene or '(no gene)'} (not a target)")
                continue

            existing = None
            if g["session_id"] is not None:
                existing, err = _target_session(g["session_id"], proc, gene)
                if err:
                    out["skipped"].append(err)
                    continue

            experimenter = _resolve_member(_get(rows[0], "experimenter"), uploader)
            if experimenter is None:
                out["skipped"].append(f"{proc}: {gene} (no experimenter)")
                continue

            site_id = (existing.site_id if existing is not None
                       else getattr(experimenter, "site_id", None))
            wt, wt_err = _resolve_cell_line(target, _get(rows[0], "cell_line_wt"),
                                            genotype="WT", site_id=site_id)
            ko, ko_err = _resolve_cell_line(target, _get(rows[0], "cell_line_ko"),
                                            genotype="KO", site_id=site_id)
            # A cell line that will not resolve is reported rather than silently
            # dropped. It used to be dropped on every row of every tab, because
            # the lookup went through `target.cell_lines` and a wild type has no
            # gene — the session came out blank and nothing said so until the
            # placeholder reached a Data Note.
            for err in (wt_err, ko_err):
                if err:
                    out["skipped"].append(f"{proc}: {gene} — {err}")
            when = _parse_date(_get(rows[0], "date")) or _date.today()

            # Resolve antibodies first — a session with zero matched rows isn't
            # created. Rows nobody wrote a reading on are not rows: see
            # `_has_result`. A tab left untouched must not become an experiment.
            resolved = []
            for row in rows:
                if not _has_result(row, proc):
                    continue
                ab = _resolve_antibody(target, _get(row, "company"), _get(row, "antibody"))
                if ab is None:
                    out["skipped"].append(f"{proc}: {gene} — {_get(row, 'antibody') or '(blank)'} (no antibody)")
                    continue
                resolved.append((row, ab))
            if not resolved:
                continue

            if existing is not None:
                session = existing
                # Fill-only-blank on the header, the rule every other write path
                # holds: a sheet that says nothing about the wild type must not
                # clear one that is recorded.
                if wt is not None and session.cell_line_wt_id is None:
                    session.cell_line_wt = wt
                if ko is not None and session.cell_line_ko_id is None:
                    session.cell_line_ko = ko
                # Touch only the keys the sheet carries. Sessions built from a
                # workbook hold arbitrary spreadsheet columns in that dict, and
                # rebuilding it from scratch wiped them on every write.
                merged = dict(session.session_conditions or {})
                merged.update(conditions)
                session.session_conditions = merged
                conditions = merged
                if session.status in (ExperimentSession.SessionStatus.PLANNED,
                                      ExperimentSession.SessionStatus.IN_PROGRESS):
                    session.status = ExperimentSession.SessionStatus.COMPLETE
                    session.date = session.date or when
                out["sessions_updated"] += 1
            else:
                session = ExperimentSession(
                    procedure_type=proc, target=target, experimenter=experimenter,
                    date=when, site_id=experimenter.site_id,
                    session_conditions=conditions,
                    cell_line_wt=wt, cell_line_ko=ko,
                    status=ExperimentSession.SessionStatus.COMPLETE,
                )
                out["sessions_created"] += 1
            plo = _coerce_decimal(conditions.get("protein_loading_ug", "")) if conditions else None
            if plo is not None:
                session.protein_loading_ug = plo
            session.save(using=DB)

            Model = _RESULT_MODEL[proc]
            decimals = _DECIMAL_RESULT.get(proc, set())
            for row, ab in resolved:
                # Upsert, not insert. On a `(new)` tab nothing exists yet so this
                # is a create either way; on a session tab it is what makes the
                # round trip a round trip — download what is recorded, change a
                # rating, put it back, and get one row with the new rating rather
                # than two rows disagreeing.
                obj = (Model.objects.using(DB)
                       .filter(session=session, antibody=ab).first()
                       if existing is not None else None)
                is_new = obj is None
                if is_new:
                    obj = Model(session=session, antibody=ab)
                for col in _RESULT_COLS[proc]:
                    val = _get_result(row, proc, col)
                    # A blank cell means "not written down", not "clear this" —
                    # the same fill-only-blank rule the header fields hold above.
                    if val == "":
                        continue
                    if col in decimals:
                        d = _coerce_decimal(val)
                        if d is not None:
                            setattr(obj, col, d)
                    else:
                        setattr(obj, col, val)
                obj.save(using=DB)
                out["results_created" if is_new else "results_updated"] += 1

    return out
