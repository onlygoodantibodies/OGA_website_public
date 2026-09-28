"""What a draft Data Note needs from the bench — the one declaration.

`report_generator` prints a **named gap** (`[lysis buffer]`) wherever a value it
needs was never recorded, rather than inventing one (`_recorded`'s docstring has
the history). Until 26 Sep 2026 the list of those gaps lived only inside the
paragraph builders, so nothing at the bench could say "the report will need the
antigen retrieval" — you found out when the draft came back full of brackets,
weeks after the session, when nobody remembered the buffer.

This module is that list, and the report reads it too: every `_recorded` call
in a Method paragraph or figure legend asks `value()` for a `Need` declared
here, and the bench-sheet and workbook upload previews ask `missing()` of the
same declarations. So the preview and the report cannot disagree about what is
missing — `pipeline/services/tests/test_ihc_bench.py::test_the_preview_and_the_report_
name_the_same_gaps` generates the
paragraphs from a blank session and asserts the brackets they print are exactly
the gaps `missing()` names, in both directions.

Two rules it holds:

* **A need names a field somebody can enter.** Every condition key the report
  reads has an entry in `views/session_entry.PROCEDURE_CONDITION_FIELDS` — the
  form, the sessions board's `cond:` cells and the bench sheet's label map all
  read that registry — so a warning never names a field with no control. Pinned
  by test; several keys the report read had no writer before this module
  existed (WB's membrane, IP's antibody amount, FC's two tracker dyes, blocking,
  cell count and default concentration), and those were added to the registry.
* **A warning, never a refusal.** The owner's decision (PLATFORM_ROADMAP #102):
  most fields are optional, and a save is never blocked for a missing one. The
  preview names each gap so it can be filled while the notebook is still open.

Not declared here, deliberately: the gaps that are not bench fields — the wild
type and knockout names (the session's cell lines), the predicted mass
(UniProt), DepMap expression — and values whose absence only drops a clause
(an objective, a secondary dilution), because leaving words out invents nothing
and so is not a gap.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Need:
    """One session-level value a report paragraph prints or leaves as `[gap]`.

    ``keys`` are the `session_conditions` keys read, first filled wins (older
    spellings after the current one). ``result_fields`` are read off the
    session's result rows *before* the conditions — IP's detection antibody is
    recorded per row on older sessions, and the workbook's WB, IP and IF tabs
    offer gel, membrane, ECL, fixative, blocking and so on as per-row result
    columns, so a value typed there is a recorded value and never a gap. Every
    report builder passes the first session's rows for the same reason. ``only_if`` makes the need conditional:
    IHC's tissue fields are needed only when one of them says tissue was on the
    slide.
    """
    name: str
    keys: tuple
    gap: str
    where: str
    result_fields: tuple = ()
    only_if: tuple = ()

    def applies(self, conditions) -> bool:
        if not self.only_if:
            return True
        return any(_filled((conditions or {}).get(k)) for k in self.only_if)


@dataclass(frozen=True)
class ResultNeed:
    """One per-antibody value a figure legend or Method paragraph prints.

    ``label`` is the bench sheet's own column heading, so a warning names the
    column the reader will look for. ``gap`` is what the draft prints when no
    antibody carries it (WB/IF print a dash for one missing antibody and the gap
    only when all are); IHC prints the gap per antibody.
    """
    name: str
    fields: tuple
    label: str
    gap: str
    where: str
    per_antibody_gap: bool = False


# ── The declarations ─────────────────────────────────────────────────────────
#
# Order is the order the report prints them in, which is the order a warning
# lists them in.

_SECONDARY = ("secondary_antibody", "secondary_ab")
_GEL = ("gel_chemistry", "gel")
_FIX = ("fixation", "fixative")

SESSION_NEEDS = {
    "WB": (
        Need("lysis_buffer", ("lysis_buffer",), "lysis buffer", "methods"),
        Need("protein_loading", ("protein_loading_ug",), "protein loading",
             "methods and Figure 1 legend"),
        Need("gel", _GEL, "gel chemistry", "methods and Figure 1 legend",
             result_fields=("gel",)),
        Need("membrane", ("membrane",), "membrane",
             "results, methods and Figure 1 legend", result_fields=("membrane",)),
        Need("blocking", ("blocking",), "blocking buffer", "methods"),
        Need("ecl", ("ecl_type", "ecl"), "ECL substrate", "methods",
             result_fields=("ecl",)),
        Need("imaging", ("imaging_system", "detection_system"), "imaging system",
             "methods", result_fields=("detection_system",)),
        Need("secondary", _SECONDARY, "secondary antibody", "methods",
             result_fields=("secondary_ab",)),
    ),
    "IP": (
        Need("antibody_amount", ("antibody_amount_ug",), "antibody amount",
             "methods and Figure 2 legend"),
        Need("bead_type", ("bead_type",), "bead type", "methods and Figure 2 legend",
             result_fields=("bead_type",)),
        Need("gel", _GEL, "gel chemistry", "methods and Figure 2 legend",
             result_fields=("gel",)),
        Need("detection_antibody", ("detection_antibody",), "detection antibody",
             "methods and Figure 2 legend", result_fields=("detection_ab",)),
    ),
    "IF": (
        Need("fixation", _FIX, "fixative", "methods", result_fields=("fixative",)),
        Need("permeabilisation", ("permeabilisation",), "permeabilisation", "methods",
             result_fields=("permeabilisation",)),
        Need("blocking", ("blocking",), "blocking buffer", "methods",
             result_fields=("blocking",)),
        Need("secondary", _SECONDARY, "secondary antibody",
             "methods and Figure 3 legend", result_fields=("secondary_ab",)),
    ),
    "FC": (
        Need("tracker_wt", ("tracker_dye_wt",), "WT tracker dye", "methods"),
        Need("tracker_ko", ("tracker_dye_ko",), "KO tracker dye", "methods"),
        Need("fixation", _FIX, "fixative", "methods and Figure 4 legend"),
        Need("permeabilisation", ("permeabilisation",), "permeabilisation",
             "methods and Figure 4 legend"),
        Need("blocking", ("blocking",), "blocking buffer", "methods"),
        Need("secondary", _SECONDARY, "secondary antibody",
             "methods and Figure 4 legend"),
        # The form has always written `secondary_dilution`; the report read only
        # `secondary_concentration`, so a recorded value never reached it.
        Need("secondary_concentration",
             ("secondary_concentration", "secondary_dilution"),
             "secondary concentration", "methods"),
        Need("cytometer", ("flow_cytometer",), "flow cytometer",
             "methods and Figure 4 legend"),
        Need("analysis_software", ("analysis_software",), "analysis software",
             "methods"),
        Need("cell_count", ("cell_count",), "cell count", "methods"),
        Need("primary_concentration", ("primary_concentration",),
             "primary antibody concentration", "Figure 4 legend"),
    ),
    # Read off the PPP2R5D report's "Antibody screening by immunohistochemistry".
    "IHC": (
        Need("fixation", ("fixation",), "fixation", "methods"),
        Need("embedding", ("embedding",), "embedding",
             "results, methods and Figure 5 legend"),
        Need("section_format", ("section_format",), "section format",
             "methods and Figure 5 legend"),
        Need("section_thickness", ("section_thickness_um",), "section thickness",
             "methods"),
        Need("stainer", ("stainer",), "stainer", "methods"),
        Need("antigen_retrieval", ("antigen_retrieval",), "antigen retrieval",
             "methods"),
        Need("chromogen", ("chromogen",), "chromogen", "methods"),
        Need("counterstain", ("counterstain",), "counterstain", "methods"),
        Need("scanner", ("scanner",), "scanner", "methods"),
        Need("objective", ("objective",), "objective", "methods"),
        Need("image_export_software", ("image_export_software",),
             "image export software", "methods"),
        # Only for a run that had tissue on the slide — a HAP1-pellet run has
        # none, and asking for "tissue species" there would be a warning about
        # something that did not happen.
        Need("tissue_species", ("tissue_species",), "tissue species", "methods",
             only_if=("tissue_species", "tissue_organs", "tissue_fixation")),
        Need("tissue_organs", ("tissue_organs",), "tissue organs", "methods",
             only_if=("tissue_species", "tissue_organs", "tissue_fixation")),
        Need("tissue_fixation", ("tissue_fixation",), "tissue fixation", "methods",
             only_if=("tissue_species", "tissue_organs", "tissue_fixation")),
    ),
}

RESULT_NEEDS = {
    "WB": (ResultNeed("dilution", ("dilution", "primary_ab_dilution"),
                      "Used dilution", "dilutions to be added", "Figure 1 legend"),),
    "IP": (),
    "IF": (ResultNeed("dilution", ("primary_ab_dilution", "best_concentration"),
                      "Used dilution", "dilutions to be added", "Figure 3 legend"),),
    # The FC legend states one default concentration (`primary_concentration`)
    # rather than listing antibodies, so flow has no per-antibody need.
    "FC": (),
    "IHC": (
        ResultNeed("dilution", ("primary_ab_dilution",), "Used dilution",
                   "dilution", "Figure 5 legend", per_antibody_gap=True),
        ResultNeed("dilution_source", ("dilution_source",), "Dilution source",
                   "dilution source", "Figure 5 legend", per_antibody_gap=True),
        ResultNeed("secondary", ("secondary_ab",), "Secondary / detection",
                   "secondary / detection", "methods and Figure 5 legend",
                   per_antibody_gap=True),
    ),
}


def need(proc, name) -> Need:
    for n in SESSION_NEEDS.get(proc, ()):
        if n.name == name:
            return n
    raise KeyError(f"no report need {name!r} declared for {proc}")


def result_need(proc, name) -> ResultNeed:
    for n in RESULT_NEEDS.get(proc, ()):
        if n.name == name:
            return n
    raise KeyError(f"no per-antibody report need {name!r} declared for {proc}")


def _filled(value) -> bool:
    return value is not None and value is not False and str(value).strip() != ""


def recorded(n: Need, conditions, rows=()) -> str:
    """The value the report prints for ``n``, or ``""`` when nothing is on file."""
    for r in rows or ():
        for f in n.result_fields:
            v = getattr(r, f, None) if not isinstance(r, dict) else r.get(f)
            if _filled(v):
                return str(v)
    for k in n.keys:
        v = (conditions or {}).get(k)
        if _filled(v):
            return str(v)
    return ""


def value(proc, name, conditions, rows=()) -> str:
    """What the report prints: the recorded value, or ``[gap]``."""
    n = need(proc, name)
    return recorded(n, conditions, rows) or f"[{n.gap}]"


def result_value(n: ResultNeed, row) -> str:
    """One antibody's value for a per-antibody need, or ``""``."""
    for f in n.fields:
        v = row.get(f) if isinstance(row, dict) else getattr(row, f, None)
        if _filled(v):
            return str(v)
    return ""


def _registry(proc) -> dict:
    from pipeline.views.session_entry import PROCEDURE_CONDITION_FIELDS
    return {k: label for k, label, *_ in PROCEDURE_CONDITION_FIELDS.get(proc, [])}


def writer_key(proc, n: Need):
    """The key of ``n`` that a form field writes for this procedure, or None.

    A need's keys carry older spellings (`gel_chemistry` then `gel`), and which
    one the form writes differs by procedure — IP's form writes `gel`, WB's
    `gel_chemistry` — so the warning names the one a person can actually fill.
    """
    registry = _registry(proc)
    return next((k for k in n.keys if k in registry), None)


def condition_label(proc, key) -> str:
    """The form's own label for a condition key — what the warning calls it."""
    return _registry(proc).get(key) or key.replace("_", " ").capitalize()


def missing(proc, conditions, rows=()) -> list[dict]:
    """Every gap a draft would print for this session, as warnings.

    ``conditions`` is what the session will hold **after** the save being
    previewed — template, stored and the sheet's own values merged, blanks
    filling only — never what exists now. ``rows`` are the session's result rows
    as they will be stored: objects or dicts, each with an ``antibody`` label
    under the key ``"antibody"`` (dicts) or ``str(row.antibody)``.

    Each entry: ``{"kind": "session"|"antibody", "field", "key", "gap",
    "where", "antibodies", "message"}``.
    """
    out = []
    conditions = conditions or {}
    for n in SESSION_NEEDS.get(proc, ()):
        if not n.applies(conditions) or recorded(n, conditions, rows):
            continue
        key = writer_key(proc, n) or n.keys[0]
        label = condition_label(proc, key)
        out.append({
            "kind": "session", "field": label, "key": key,
            "gap": f"[{n.gap}]", "where": n.where, "antibodies": [],
            "message": (f"{label} is blank — the report's {n.where} "
                        f"need{'s' if n.where == 'methods' else ''} it; a draft "
                        f"would print [{n.gap}]."),
        })
    for n in RESULT_NEEDS.get(proc, ()):
        lacking = []
        for r in rows or ():
            if not result_value(n, r):
                name = (r.get("antibody") if isinstance(r, dict)
                        else str(getattr(r, "antibody", "") or ""))
                if name and name not in lacking:
                    lacking.append(name)
        if not lacking:
            continue
        everyone = len(lacking) == len({(r.get("antibody") if isinstance(r, dict)
                                         else str(getattr(r, "antibody", "")))
                                        for r in rows})
        shown = "every antibody" if everyone else (
            f"{len(lacking)} antibod{'y' if len(lacking) == 1 else 'ies'} "
            f"({', '.join(lacking)})")
        if n.per_antibody_gap or everyone:
            prints, gap = f"a draft would print [{n.gap}]", f"[{n.gap}]"
        else:
            # WB and IF print a dash for one antibody's missing dilution and the
            # gap only when none has one — so no bracket to name here.
            prints, gap = "a draft would print a dash for those", ""
        out.append({
            "kind": "antibody", "field": n.label, "key": n.fields[0],
            "gap": gap, "where": n.where, "antibodies": lacking,
            "message": (f"{n.label} is blank for {shown} — the report's "
                        f"{n.where} needs it; {prints}."),
        })
    return out


def session_conditions(session, extra=None) -> dict:
    """The conditions a report reads for ``session``, as `report_generator`
    merges them: template first, then the session's own, then ``extra`` (what
    the upload being previewed will add — blank values never clear).

    `ExperimentSession.protein_loading_ug` is folded in when the conditions do
    not carry it: the sessions board edits that column, not the condition, and
    the report used to read the condition alone — so a loading recorded on the
    board printed as `[protein loading]`.
    """
    conditions = {}
    tmpl = getattr(session, "protocol_template", None) if session is not None else None
    if tmpl is not None and isinstance(getattr(tmpl, "conditions", None), dict):
        conditions.update(tmpl.conditions)
    if session is not None and isinstance(session.session_conditions, dict):
        conditions.update(session.session_conditions)
    for k, v in (extra or {}).items():
        if _filled(v):
            conditions[k] = v
    loading = getattr(session, "protein_loading_ug", None) if session is not None else None
    if not _filled(conditions.get("protein_loading_ug")) and loading is not None:
        conditions["protein_loading_ug"] = _plain_number(loading)
    return conditions


def _plain_number(value) -> str:
    """`20.00` → `20`, `2.50` → `2.5`: a Decimal column as a person writes it."""
    text = str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text
