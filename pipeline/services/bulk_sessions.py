"""Plan an experiment: pick a target, get its antibodies in an editable grid.

When you choose a target the grid is pre-filled with the antibodies already in
the pipeline for it; you can edit rows, remove ones you're not testing, or add
new ones. A new row (a catalogue number not yet on the target) becomes a new
``Antibody`` record when the session is created. Submitting builds the
``results`` payload for the shared ``services.sessions.apply`` write-spine —
one result row per antibody — so recording results afterwards is unchanged.
"""
from __future__ import annotations

import re

from pipeline.models import Antibody, CellLine
from pipeline.services import example_row
from pipeline.services import sessions as sess
from pipeline.services.cropper import db as cdb
from pipeline.services.cropper.commit import _apply_metadata

DB = "pipeline_db"

# Columns for the downloadable template / upload.
SESSION_COLUMNS = ["antibody", "company", "rrid", "notes"]
SESSION_EXAMPLE = ["ab138501", "Abcam", "AB_2537855", "test at 1/1000"]

_ALIASES = {
    "antibody": "antibody", "catalogue": "antibody", "catalog": "antibody",
    "catalogue number": "antibody", "catalogue #": "antibody", "cat": "antibody",
    "cat #": "antibody", "cat#": "antibody", "ab": "antibody",
    "company": "company", "supplier": "company", "vendor": "company",
    "rrid": "rrid",
    "notes": "comments", "note": "comments", "comment": "comments",
    "comments": "comments", "planned": "comments", "dilution": "comments",
}


def _norm(h):
    return (h or "").strip().lower()


def _cells(line):
    if "\t" in line:
        parts = line.split("\t")
    elif "," in line:
        parts = line.split(",")
    else:
        parts = re.split(r"\s{2,}", line)
    return [p.strip() for p in parts]


def antibodies_for_target(target):
    """Existing antibodies for a target, shaped as grid rows (pre-fill)."""
    if not target:
        return []
    qs = (Antibody.objects.using(DB).filter(target=target)
          .select_related("company").order_by("company__name", "catalogue_number"))
    return [{
        "antibody": a.catalogue_number or "",
        "company": (a.company.name if a.company else ""),
        "rrid": a.rrid or "",
        "comments": "",
        "id": a.id,
    } for a in qs]


def parse(text):
    """Pasted antibody list → grid rows. Header row optional; without one,
    columns are antibody, company, rrid, notes in order."""
    rows = []
    lines = [l for l in (text or "").splitlines()
             if l.strip() and not example_row.is_example(_cells(l))]
    if not lines:
        return rows

    first = _cells(lines[0])
    header = None
    if first and _norm(first[0]) in _ALIASES:
        header = [_ALIASES.get(_norm(c)) for c in first]
        body = lines[1:]
    else:
        body = lines

    order = ["antibody", "company", "rrid", "comments"]
    for line in body:
        cs = _cells(line)
        row = {"antibody": "", "company": "", "rrid": "", "comments": ""}
        if header:
            for key, val in zip(header, cs):
                if key:
                    row[key] = (val or "").strip()
        else:
            for key, val in zip(order, cs):
                row[key] = (val or "").strip()
        if row["antibody"]:
            rows.append(row)
    return rows


def preview(target, rows, member=None):
    """Per-row status against ``target``: existing (already a record) vs new
    (a record will be created on submit).

    Takes the same ``member`` ``resolve_or_create`` gets, so the preview looks
    at the same site's vial the submit will write to."""
    items = []
    for r in rows:
        cat = (r.get("antibody") or "").strip()
        company = (r.get("company") or "").strip()
        if not target:
            items.append({"row": r, "status": "no-target", "resolved": "", "error": ""})
            continue
        if not cat:
            items.append({"row": r, "status": "blocked", "resolved": "",
                          "error": "needs a catalogue number"})
            continue
        ab = cdb.find_antibody(target, company, cat,
                               site_id=getattr(member, "site_id", None))
        if ab:
            items.append({"row": r, "status": "existing", "resolved": str(ab), "error": ""})
        else:
            items.append({"row": r, "status": "new", "resolved": "will be added", "error": ""})
    return items


def summarize(items):
    return {
        "rows": len(items),
        "existing": sum(1 for i in items if i["status"] == "existing"),
        "new": sum(1 for i in items if i["status"] == "new"),
        "blocked": sum(1 for i in items if i["status"] == "blocked"),
    }


def resolve_or_create(target, row, member):
    """Return (antibody, created) for a grid row, creating a new Antibody on the
    target if the catalogue number isn't already there (dedup-safe, reuses the
    cropper write engine)."""
    cat = (row.get("antibody") or "").strip()
    company = (row.get("company") or "").strip()
    # A session names a product, not a lot, so this stays a product lookup — but
    # one product can hold a vial per site, and the vial you tested is your own.
    # Without the site preference a Leicester session would attach to whichever
    # site's row happened to be created first.
    ab = cdb.find_antibody(target, company, cat,
                           site_id=getattr(member, "site_id", None))
    if ab:
        return ab, False
    ab = Antibody(target=target, catalogue_number=cat)
    if member is not None and getattr(member, "site_id", None):
        ab.site_id = member.site_id
    _apply_metadata(ab, {"company": company, "rrid": (row.get("rrid") or "").strip()},
                    overwrite=False)
    ab.save(using=DB)
    return ab, True


def cell_lines_for_target(target):
    """Existing cell-line names for the WT/KO autocomplete + 'in pipeline' hint,
    plus the target's KO lines with their WT parent so the planner can auto-fill.
    WT lines are shared across targets; KO lines are specific to this target."""
    wt = list(CellLine.objects.using(DB).filter(genotype="WT")
              .exclude(name="").order_by("name")
              .values_list("name", flat=True).distinct())
    ko, ko_lines = [], []
    if target:
        ko_qs = (CellLine.objects.using(DB).filter(genotype="KO", target=target)
                 .exclude(name="").select_related("parent_line").order_by("name"))
        for cl in ko_qs:
            ko.append(cl.name)
            parent = (cl.parent_line.name if cl.parent_line
                      else (cl.parental_line_name or ""))
            ko_lines.append({"name": cl.name, "parent": parent})
    return {"wt": wt, "ko": ko, "ko_lines": ko_lines}


def resolve_or_create_cell_line(name, *, genotype, target=None, parent=None, member=None):
    """Return (cell_line, created) for a cell-line name, matching an existing
    line or creating a minimal record. A KO gets the session's target and (if
    given) its WT parent; a WT is a bare parental line. Fuller details
    (C-numbers, storage, supplier) are added on the cell-line board.

    Matching goes through `services/cell_lines.py`, which is the whole point of
    that module. This function **took a `genotype` argument and used it only
    when creating**: the match was `filter(name__iexact=name).first()`, so the
    seventh field test typed `SH-SY5Y` into the quick panel's *wild type* box
    and got McGill's `SH-SY5Y PRKN KO` — a knockout, of another gene, at another
    institution — silently attached as the biological control of a Leicester
    STMN2 session. Five rows are literally named `SH-SY5Y` and `Meta.ordering`
    is `['name']`, so which one won was arbitrary. It then reached the generated
    Data Note, relabelled as a STMN2 knockout.

    Two refusals now, both raised so `session_plan_commit` renders them and
    nothing is written:

    * the name matches lines of another kind — you meant one of those, not a new
      one;
    * the name matches several — creating a duplicate is not the answer either.

    Only a name nothing answers to is created, which is the case this was for.
    """
    from pipeline.services import cell_lines as clines

    name = (name or "").strip()
    if not name:
        return None, False

    site_id = getattr(member, "site_id", None)
    cl, err = clines.resolve(name, genotype=genotype, site_id=site_id)
    if cl is not None:
        return cl, False
    # `resolve` refuses both "nothing of this kind" and "several of this kind".
    # Only the first is a new line; anything already answering to the name means
    # the typed value is wrong rather than new.
    if clines.candidates(name):
        raise ValueError(err)

    cl = CellLine(name=name, genotype=genotype)
    if genotype == "KO" and target is not None:
        cl.target_id = target.pk
    if parent is not None:
        cl.parent_line_id = parent.pk
        cl.parental_line_name = parent.name
    if member is not None and getattr(member, "site_id", None):
        cl.site_id = member.site_id
    cl.save(using=DB)
    return cl, True
