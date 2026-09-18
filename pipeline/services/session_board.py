"""The sessions board — read layer.

Same contract as ``services/target_board.py`` (``board_queryset`` →
``apply_filters`` → ``row_for`` → ``board_rows``), so both boards behave the
same and the JS in ``static/pipeline/board.js`` drives either.

Sessions differ from targets in one structural way and it shapes everything
here: **a target is one row, a session is a header plus N result rows**, and the
result columns depend on the procedure — 16 fields for WB, 24 for IP, 24 for IF,
8 for FC, with almost no overlap between WB and IF. So the board is two levels:
one row per session, and a session's results fetched on demand when a row is
opened.

Result columns are derived from the models themselves rather than written out
again. There were already four hand-maintained lists of "the result fields"
(the detail form, the template download, the round-trip export, the bench
sheet), one of which carries a comment admitting they must be kept in step by
hand. This does not add a fifth.
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Count, Q

from pipeline.models import (ExperimentSession, FcResult, IfResult, IpResult,
                             Member, Site, WbResult)
from pipeline.services import board_page as board_page_svc
from pipeline.services import find
from pipeline.services import members
from pipeline.services import sites as site_svc
from pipeline.services import targets as target_svc

DB = "pipeline_db"

PROCEDURES = ["WB", "IP", "IF", "FC"]

RESULT_MODELS = {"WB": WbResult, "IP": IpResult, "IF": IfResult, "FC": FcResult}

# Never a column on the results grid: identity, plumbing, or edited elsewhere.
_RESULT_SKIP = {"id", "session", "antibody", "access_id", "extra_lanes"}

# Session fields the board may edit in place. procedure_type and target are
# absent on purpose — they are the session's identity. Changing either means
# this was the wrong session, and the fix is to delete it, not retype it.
EDITABLE_SESSION_FIELDS = {
    "date", "status", "comments", "protein_loading_ug", "fc_sub_protocol",
    "site", "experimenter", "cell_line_wt", "cell_line_ko",
}

# A protocol condition arrives as `cond:<key>`. They are not columns — which
# conditions exist depends on the procedure — so they live in the
# session_conditions JSON and are addressed by name.
CONDITION_PREFIX = "cond:"


def condition_fields(procedure_type) -> list[dict]:
    """The protocol conditions for one procedure — label, type, placeholder.

    Read from the one registry the session form already used, rather than a
    second list here. Four hand-written copies of "the result fields" is the
    mistake this codebase has already made once (CLAUDE.md); conditions are not
    going to be the fifth.
    """
    from pipeline.views.session_entry import PROCEDURE_CONDITION_FIELDS
    return [{"key": key, "label": label, "type": input_type,
             "placeholder": placeholder}
            for key, label, input_type, placeholder
            in PROCEDURE_CONDITION_FIELDS.get(procedure_type, [])]


def condition_field_names(procedure_type) -> set[str]:
    return {f["key"] for f in condition_fields(procedure_type)}


def protocol_guidance(session) -> list:
    """The phases of the protocol this session followed, for reference.

    Read-only, and empty when no template is attached — most sessions imported
    from Access have none. It is on the board because it was the last thing
    ``session_detail`` showed that nothing else did, and somebody at the bench
    reading "which buffer" should not need a different page for it.
    """
    template = getattr(session, "protocol_template", None)
    return list(getattr(template, "protocol_guidance", None) or []) if template else []


def result_field_names(procedure_type) -> list[str]:
    """The result columns for one procedure, straight off the model."""
    model = RESULT_MODELS.get(procedure_type)
    if model is None:
        return []
    return [f.name for f in model._meta.fields if f.name not in _RESULT_SKIP]


# ── A result row is not a reading ────────────────────────────────────────────
#
# Planning a session writes one result row per antibody it will test, blank. So
# a session planned and not yet run has twenty-two rows and nothing written on
# any of them — and the board's RESULTS column, which counted rows, read
# **"22 results"** over it. The twelfth field test read that as a finished
# session sitting in the list marked *Planned*, and filed the status as the bug.
# The status was right; the count was the lie. "22 results" on a session nobody
# has run makes an empty session look complete, which is the same sentence this
# app already says in three other places: a workbook tab nobody wrote on is not
# a session (`session_import._has_result`), a session with no readings is not in
# the report (`report_generator._has_readings`), and a bench sheet's rows are
# the session's own antibodies rather than the gene's.
#
# `is_reading` is the single value test behind all of it, and
# `reading_counts` is the same question asked of the database.

# A note is not a measurement. The sessions board's Add panel carries a per-row
# NOTES column that lands in the result row's `comments` at *planning* time, so
# a session nobody had run drew "2 results — every row in this session has
# something recorded" the moment it was created (run 19). Readings are the
# result fields; a comment rides beside them and is still drawn, still
# exported, still searched — it just does not make a row a reading.
NOT_A_READING = frozenset({"comments"})


def reading_fields(procedure_type) -> list[str]:
    """The result columns a reading can be written in — `result_field_names`
    minus the notes column. The one list `reading_q`, the workbook importer and
    the report generator all ask."""
    return [f for f in result_field_names(procedure_type) if f not in NOT_A_READING]


def is_reading(value) -> bool:
    """Whether one result cell counts as something somebody wrote down.

    ``False`` and ``None`` are the absence of a reading, not a reading of
    absence. ``0`` is a reading: somebody measured a zero.
    """
    if value is None or value is False:
        return False
    return bool(str(value).strip())


def reading_q(procedure_type) -> Q:
    """"Any result field on this row carries a reading", as a queryset filter.

    Built from the same ``result_field_names`` list ``is_reading`` is applied
    to, and from the field's own type — a text column is empty as ``''`` and a
    decimal one as ``NULL``, so one test cannot serve both.
    """
    model = RESULT_MODELS[procedure_type]
    q = Q()
    for name in reading_fields(procedure_type):
        internal = model._meta.get_field(name).get_internal_type()
        if internal in ("DecimalField", "IntegerField", "FloatField",
                        "DateField", "DateTimeField"):
            q |= Q(**{f"{name}__isnull": False})
        elif internal == "BooleanField":
            q |= Q(**{name: True})
        else:
            # `__gt=''` excludes both the empty string and NULL, on SQLite and
            # PostgreSQL alike.
            q |= Q(**{f"{name}__gt": ""})
    return q


# ── Two columns, one measurement ─────────────────────────────────────────────
#
# `WbResult` carries **both** `dilution` and `primary_ab_dilution`, and they mean
# the same thing: the Access import wrote one source column (`1AbDilution`) into
# each of them, so every imported row holds the value twice. Nothing said so, and
# both were drawn on the same result card — `dilution` up in the readings and
# `primary_ab_dilution` a click away under Method — which reads as two facts
# about one blot with no hint of how they differ, because they do not.
#
# It was writable twice as well: the printable bench sheet fills `dilution`, the
# full workbook shipped a column for each, and `report_generator` reads
# `r.dilution or r.primary_ab_dilution`. So two people recording one session from
# two artefacts could store two dilutions and the report would silently print the
# first.
#
# `dilution` wins because it is what everything else already prefers. The other
# is **not removed from the model** — a `RemoveField` against live PostgreSQL is
# the one migration a rollback cannot undo (CLAUDE.md), and the rows carry real
# values. It is an alias on the way in (`session_template.RESULT_COL_ALIASES`)
# and hidden on the card *only while it agrees* with the field it copies: a row
# whose two values disagree is exactly the row somebody has to see.
DUPLICATE_RESULT_FIELDS = {"WB": {"primary_ab_dilution": "dilution"}}

# Where the derived name is not the clearest one. `dilution` on a WB card sat
# beside a Method field called "primary ab dilution", so naming it says which
# dilution it is without a second column to say it.
RESULT_FIELD_LABELS = {"WB": {"dilution": "dilution (primary Ab)"}}

# Which result fields offer what is already on file is **derived**, not listed —
# see `result_field_suggestions`. It was `{"WB": ("rating",)}`: one field of the
# twelve on a western blot, and nothing at all for IP, IF and FC. The reasoning
# behind that single entry was right and applies far more widely — `rating` is
# free text, nothing on any screen says whether it wants `Specific`, a 1–5 or a
# sentence, and whatever is typed there is what a generated Data Note prints. So
# is `signal`. So is `lysis_buffer`. Loose, never strict — see
# `board.js::editorHtml` and `services/vocabulary.py`.


def _duplicates_shown(procedure_type, rows) -> dict:
    """`{copy: "conflict" | "only_copy"}` — which duplicate columns to draw, and why.

    A duplicate that agrees everywhere is noise; one that holds something the
    field it copies does not is the whole reason not to simply hide it. Hiding a
    non-empty field is how a value nobody can see becomes a value somebody
    overwrites — the same rule the unrecognised session conditions follow one
    panel down.

    **A blank copy is not a disagreement**, and treating it as one was the whole
    of the eleventh field test's complaint. Filling in `dilution` and leaving the
    Method copy alone is the *normal* way to record a western blot, so the column
    appeared with a note reading "these two disagree" on a session where exactly
    one box had been typed into — momentarily alarming, about nothing, on a
    screen whose job is to flag the one row where two values really do differ.
    An empty copy has nothing to lose by being hidden, so it is hidden.

    The other direction still shows, and needs its own words: a copy that carries
    a value while the canonical field is blank is not two answers disagreeing,
    it is the *only* answer, sitting in the column reports do not read.
    """
    dupes = DUPLICATE_RESULT_FIELDS.get(procedure_type, {})
    out = {}
    for copy, canonical in dupes.items():
        for r in rows:
            mine = str(r["values"].get(copy) or "").strip()
            theirs = str(r["values"].get(canonical) or "").strip()
            if not mine or mine == theirs:
                continue
            # A real conflict outranks "only this one is filled in": if any row
            # has both, the note has to be the stronger of the two.
            out[copy] = "conflict" if theirs else out.get(copy, "only_copy")
    return out


def result_field_suggestions(procedure_type) -> dict:
    """`{field: {"values": [...]}}` — what each result cell may offer.

    Read from the database rather than written down, because the vocabulary is
    whatever the consortium has actually been recording. Ordered by how often
    each value has been used, so the common answer is offered first.

    **Which fields get a list is derived too, and that is the change worth
    noting.** This used to read a hand-typed `SUGGESTED_RESULT_FIELDS` holding
    exactly `{"WB": ("rating",)}` — so one field of the twelve on a western blot
    was offered and IP, IF and FC result rows got nothing at all, on the surface
    where a scientist types the method they followed. A list of "fields worth
    offering" is a second place the truth lives and it goes stale the moment
    somebody records a new kind of value; `vocabulary.offerable` asks every text
    column instead and lets the cap answer.

    Measured against live (1 Sep 2026) that sweep reaches twenty method fields
    it used to miss, every one with a real vocabulary: `wb.signal` 3 distinct
    over 1,927 rows — one of the two axes `AntibodyOutcome` reads, and suggested
    nowhere until now — `ip.enrichment` 3 over 1,631, `if.specific_signal` 3
    over 1,256, `ip.lysis_buffer` 5 over 1,544 (the field a generated Data Note
    leaves as `[lysis buffer]` when nobody filled it in), `wb.ecl` 8 over 1,878,
    `wb.gel` 17, `ip.bead_type` 17, `wb.secondary_ab` 20. `wb.dilution` has 76
    and is correctly left as a plain box.

    Never strict. Every one of these is a convention rather than an enum: a lab
    that starts using a new buffer must be able to type it.
    """
    from pipeline.services import vocabulary

    model = RESULT_MODELS.get(procedure_type)
    if model is None:
        return {}
    return vocabulary.offerable(model, skip=_RESULT_SKIP, db=DB)


def cell_choices(site_id=None) -> dict:
    """What each session cell may hold — see ``antibody_board.cell_choices``.

    Three closed sets and two conventions, and the split is the same one
    everywhere: a `<select>` where the writer refuses anything else, a
    `<datalist>` where an unlisted value is legitimate.

    **The cell-line boxes are the ones where a wrong value costs most**, and
    they are still loose on purpose. A session controlled against the wrong line
    is a silent bad reading, so the list matters — but `cell_lines.session_options`
    is a *preference*, not a gate: another bench's line stays reachable after the
    dash, and a C-number typed off a tube must keep working. Narrowing that to a
    dropdown would take both away, which is the trade `newEntry`'s `suggestions`
    already documents one surface over.

    `fc_sub_protocol` has only two values on live and is **not** here: this
    board draws no cell for it (it is in `EDITABLE_SESSION_FIELDS` and in no
    column), and a picker for a cell nobody draws is dead configuration.
    """
    from pipeline.services import cell_lines as cell_lines_svc
    from pipeline.services import members as members_svc
    from pipeline.services import vocabulary

    lines = lambda genotype: {"values": [
        opt["value"] if isinstance(opt, dict) else opt
        for opt in cell_lines_svc.picker_options(genotype=genotype,
                                                 site_id=site_id)]}
    return {
        "status": {"strict": True, "values": status_choices()},
        "site": vocabulary.sites(),
        # Who ran it. `members.experimenters` is the one list — the step-by-step
        # form's own queryset had a `select_related("user")` inner join that
        # dropped any member with no login row, silently, and this board must
        # not grow a second copy of that mistake.
        "experimenter": {"strict": True, "values": sorted(
            {(m.display_name or "").strip() for m in members_svc.experimenters()}
            - {""})},
        "cell_line_wt": lines("WT"),
        "cell_line_ko": lines("KO"),
    }


def status_choices() -> list[dict]:
    """The six session statuses, for anything that offers them.

    One list behind the filter, the Plan a session header **and** the grid cell.
    The grid was the odd one out: a free-text box on a field whose every other
    surface is a dropdown, so `done` could be typed and refused after the fact.
    """
    return [{"value": v, "label": label}
            for v, label in ExperimentSession.SessionStatus.choices]


def _line_label(line) -> str:
    """A cell line named so two of them can be told apart: what it is, then whose.

    The rendering itself lives in `services/cell_lines.py`, because that module
    has to read this string back: the board prints `HAP1 — Leicester`, so that
    is what a person retypes into the cell, and a resolver that did not know the
    format refused the app's own output.

    Both halves are needed and each was missing on its own. The site, because two
    labs really do both call their parental HAP1. And the gene for a knockout,
    because a KO is very often stored under the parental's bare name — this run's
    was literally `HAP1` — so a WT and the KO made from it printed the same word
    twice, which is what "HAP1 / HAP1" was. Adding only the site turned that into
    `HAP1 — Leicester` twice, which is longer and no clearer.

    The session form's own dropdown has decorated KO rows with their gene since
    it was written; this is that, where the board can use it. Both go through
    `cell_lines.label` now — the form's list is `cell_lines.session_options`.
    """
    from pipeline.services import cell_lines as cl
    return cl.label(line)


def board_queryset():
    return (ExperimentSession.objects.using(DB)
            .select_related("target", "experimenter", "site",
                            "cell_line_wt", "cell_line_ko",
                            "cell_line_wt__site", "cell_line_ko__site",
                            "cell_line_ko__target"))


def apply_filters(qs, *, q="", gene="", procedure="", site="", experimenter="",
                  status="", date_from="", date_to="", show_cancelled=""):
    """Filters mirror how people actually look for a session: whose is it, which
    gene, which application, when.

    Cancelled sessions are hidden unless asked for — they are kept as a record
    that an experiment was abandoned, not as work in progress. Asking for the
    cancelled status explicitly still shows them.
    """
    if q:
        # One list, in `services/find.py`, shared with the nav search box.
        # `.distinct()` because that list reaches result-row comments, which
        # joins four tables — without it a session with three matching readings
        # is drawn three times and the row count disagrees with the grid.
        qs = qs.filter(find.session_q(q)).distinct()
    # The exact gene, matching the other three boards. This board only had the
    # fuzzy `q`, so "STMN2's sessions" was the one question of its kind that
    # could not be asked precisely — and a gene could not be carried here from
    # another board.
    # One gene, or a comma-separated list of them — `services/targets.py::gene_q`.
    if gene:
        gene_filter = target_svc.gene_q("target__gene_name", gene)
        if gene_filter is not None:
            qs = qs.filter(gene_filter)
    if procedure:
        qs = qs.filter(procedure_type=procedure)
    if site:
        qs = site_svc.filter_by(qs, "site_id", site)
    if experimenter:
        qs = qs.filter(experimenter_id=experimenter)
    if status:
        qs = qs.filter(status=status)
    elif not show_cancelled:
        qs = qs.exclude(status=ExperimentSession.SessionStatus.CANCELLED)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    return qs


def result_counts(session_ids) -> dict:
    """{session_id: n} across all four result tables.

    Four queries whatever the board's size — the count must not scale with the
    number of sessions on screen, which is what an annotate-per-relation would
    do via a fan-out join.
    """
    out = defaultdict(int)
    if not session_ids:
        return {}
    for model in RESULT_MODELS.values():
        for row in (model.objects.using(DB).filter(session_id__in=session_ids)
                    .values("session_id").annotate(n=Count("id"))):
            out[row["session_id"]] += row["n"]
    return dict(out)


def reading_counts(session_ids) -> dict:
    """{session_id: n} — result rows somebody has actually written on.

    Beside ``result_counts`` rather than replacing it, because the board needs
    both numbers to say the useful thing: twenty-two rows and no readings is
    *"22 antibodies, none done yet"*, and the difference between the two is what
    is left to do. Four more queries, and like ``result_counts`` the number of
    them does not grow with the board's size.
    """
    out = defaultdict(int)
    if not session_ids:
        return {}
    for procedure, model in RESULT_MODELS.items():
        for row in (model.objects.using(DB)
                    .filter(Q(session_id__in=session_ids) & reading_q(procedure))
                    .values("session_id").annotate(n=Count("id"))):
            out[row["session_id"]] += row["n"]
    return dict(out)


def row_for(session, counts=None, readings=None) -> dict:
    return {
        "id": session.pk,
        "gene": session.target.gene_name if session.target_id else "",
        "protein": session.target.protein_name if session.target_id else "",
        "target_id": session.target_id,
        "procedure": session.procedure_type,
        "procedure_label": session.get_procedure_type_display(),
        "date": session.date.isoformat() if session.date else "",
        "planned_date": session.planned_date.isoformat() if session.planned_date else "",
        "experimenter": str(session.experimenter) if session.experimenter_id else "",
        "experimenter_id": session.experimenter_id,
        "site": session.site.name if session.site_id else "",
        "site_id": session.site_id,
        "status": session.status,
        "status_label": session.get_status_display(),
        # Named with their site. The column read "HAP1 / HAP1" — true, useless,
        # and indistinguishable from a copy-paste bug, because the WT parental
        # and the knockout made from it really are both called HAP1 and the two
        # sites that hold one each call it that too. Selected above, so the
        # query count does not grow with the number of rows.
        "cell_line_wt": _line_label(session.cell_line_wt) if session.cell_line_wt_id else "",
        "cell_line_ko": _line_label(session.cell_line_ko) if session.cell_line_ko_id else "",
        "protein_loading_ug": (float(session.protein_loading_ug)
                               if session.protein_loading_ug is not None else None),
        "fc_sub_protocol": session.fc_sub_protocol,
        "comments": session.comments or "",
        "conditions": session.session_conditions or {},
        "result_count": (counts or {}).get(session.pk, 0),
        # Beside the row count, never instead of it. A planned session carries
        # one blank row per antibody it will test, so the column headed RESULTS
        # said "22 results" about a session nobody had run — see `is_reading`.
        #
        # It is a count of what has been **written down**, and deliberately not
        # a verdict on whether the work happened: gaps in a results section are
        # normal and accepted, and completion is a published report's DOI, not a
        # full set of cells (owner, 5 Aug). So the board reports both numbers
        # and draws no conclusion from the difference.
        "reading_count": (readings or {}).get(session.pk, 0),
    }


def board_rows(**filters) -> list[dict]:
    """Every matching row — for exports and for tests that want the whole set."""
    qs = apply_filters(board_queryset(), **filters).order_by("-date", "-pk")
    sessions = list(qs)
    ids = [s.pk for s in sessions]
    counts, readings = result_counts(ids), reading_counts(ids)
    return [row_for(s, counts, readings) for s in sessions]


def board_page(*, page=1, per_page=board_page_svc.DEFAULT_PER_PAGE, locate=None,
               **filters) -> dict:
    """One page of sessions — see ``services/board_page.py``.

    ``result_counts`` is asked for the page's sessions only, for the same reason
    the target board narrows ``application_coverage``.
    """
    qs = apply_filters(board_queryset(), **filters).order_by("-date", "-pk")
    count = qs.count()
    pages = max(1, -(-count // per_page))
    located = board_page_svc.page_of(qs, locate, per_page) if locate else None
    if located:
        page = located
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    sessions = list(qs[start:start + per_page])
    ids = [s.pk for s in sessions]
    counts, readings = result_counts(ids), reading_counts(ids)
    return {"rows": [row_for(s, counts, readings) for s in sessions], "count": count,
            "page": page, "pages": pages, "per_page": per_page,
            "located": (bool(located) if locate else None)}


# The fields that carry the answer — did this antibody work. Everything else the
# model has is method: how the experiment was run, which is near-constant for a
# lab and is what made an IF result 21 columns wide on a laptop.
#
# The short list is the one written down on purpose. Deriving "method" as
# "everything else" means a field added to a result model still appears, in the
# method group, rather than being silently dropped by a list that went stale —
# the column list itself still comes from the model (``result_field_names``).
_READING_FIELDS = {
    "WB": ("dilution", "exposure_time", "signal", "rating", "comments"),
    "IP": ("enrichment", "amount_of_antibody", "sm_assessment", "ub_assessment",
           "ip_assessment", "comments"),
    "IF": ("specific_signal", "wt_ko_ratio_1", "wt_ko_ratio_2",
           "best_concentration", "comments"),
    # Flow has six fields in total and so no width problem — gating strategy is
    # method, but folding one field away would cost a click and save nothing.
    "FC": ("concentration", "histogram_shift", "median_fluorescence_wt",
           "median_fluorescence_ko", "gating_strategy", "comments"),
}


def result_column_group(procedure_type, field) -> str:
    """'reading' if the field is part of the answer, else 'method'."""
    return ("reading" if field in _READING_FIELDS.get(procedure_type, ())
            else "method")


def results_for(session) -> dict:
    """One session's result rows, with the column list for its procedure.

    The grid is built from ``columns`` rather than a fixed template, because the
    four procedures genuinely have different columns — this is the reason the
    board opens results per row instead of showing one flat sheet.
    """
    fields = result_field_names(session.procedure_type)
    model = RESULT_MODELS.get(session.procedure_type)
    if model is None:
        return {"columns": [], "rows": [], "field_choices": {}}

    rows = []
    qs = (model.objects.using(DB).filter(session_id=session.pk)
          .select_related("antibody", "antibody__company").order_by("pk"))
    for r in qs:
        ab = r.antibody
        values = {}
        for f in fields:
            v = getattr(r, f, "")
            values[f] = "" if v is None else (float(v) if hasattr(v, "quantize") else v)
        rows.append({
            "id": r.pk,
            "antibody": str(ab) if ab else "",
            "antibody_id": r.antibody_id,
            "catalogue": getattr(ab, "catalogue_number", "") if ab else "",
            "company": (ab.company.name if ab and ab.company_id else ""),
            "values": values,
        })
    proc = session.procedure_type
    dupes = DUPLICATE_RESULT_FIELDS.get(proc, {})
    shown = _duplicates_shown(proc, rows)
    labels = RESULT_FIELD_LABELS.get(proc, {})
    columns = []
    for f in fields:
        if f in dupes and f not in shown:
            continue
        col = {"key": f, "label": labels.get(f, f.replace("_", " ")),
               "group": result_column_group(proc, f)}
        if f in dupes:
            # Drawn only because hiding it would hide a value. Say which of the
            # two reasons it is, or it reads as a second measurement again — and
            # "these two disagree" over a blank box is an accusation about
            # nothing.
            other = dupes[f].replace("_", " ")
            col["note"] = (
                f"a second copy of {other} — these two disagree, and reports "
                f"use {other}"
                if shown[f] == "conflict" else
                f"a second copy of {other} — the value is recorded here and "
                f"{other} is blank, but reports read {other}")
        columns.append(col)
    return {
        "columns": columns,
        "rows": rows,
        # What a cell may offer, per procedure — the board merges it into
        # `cfg.cellChoices` when the row is opened.
        "field_choices": result_field_suggestions(proc),
    }


def filter_options() -> dict:
    return {
        "sites": list(Site.objects.using(DB).filter(is_active=True).order_by("name")),
        # One list for both doors to a session — see services/members.py.
        "experimenters": list(members.experimenters(DB)),
        "procedures": [{"value": p, "label": dict(
            ExperimentSession.ProcedureType.choices)[p]} for p in PROCEDURES],
        "statuses": status_choices(),
    }
