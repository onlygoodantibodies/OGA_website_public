"""
Carl's master target list ↔ the pipeline database.

Reads the workbook he actually uses (``Complete target list``, headers on row 5,
columns A–T — the range he asked us to keep) and writes it back out in either
that same shape or a better one. The file stays a supported interchange format
rather than being a one-time migration, because he was clear that offline editing
has to keep working until the online version has earned his trust.

What the file needs tolerating, all of it present in the real sheet:
  * headers on row 5, not row 1, under a merged group-header row;
  * dates as ``2020``, ``2021-05``, ``april 2024``, ``03/22/2023`` and real dates;
  * ``-`` used as "nothing here" in half a dozen columns;
  * one gene appearing twice under different projects (SYNGAP1, ADNP, DYRK1A,
    STXBP1, ARID1B, TSC1) and one appearing as both ``Rab3A`` and ``RAB3A``;
  * an active autofilter that hides 230 of 373 rows — we read the sheet, not the
    filter, so hidden rows import too.

Write rules follow the rest of the pipeline: ``parse → plan → apply``, upsert
only, **fill-only-blank by default**, a blank cell never clears a field, and an
incoming value that would overwrite a populated one is surfaced as a conflict and
only written when the caller passes ``apply_overwrites``. Per Harvinder's data
hierarchy, values we can source better than a typed cell (UniProt identity) are
flagged for review rather than silently taken or silently ignored.
"""
from __future__ import annotations

import datetime as dt
import io
import re

from django.db import transaction

from pipeline.models import (GrantingAgency, Project, Report, Site, Target,
                             TargetNomination)
from pipeline.services import doi as doi_svc
from pipeline.services import target_board

DB = "pipeline_db"

SHEET_TITLE = "Complete target list"

# (key, Carl's exact header, extra aliases). The parser matches on the
# normalised header text, so a renamed or reordered column still lands.
COLUMNS: list[tuple[str, str, tuple[str, ...]]] = [
    ("granting_agency", "Granting agencies", ("granting agency", "agency", "funder")),
    ("project",         "Project",           ("programme", "program")),
    ("funding",         "Funding",           ("funded", "funding available")),
    ("nominated",       "Date of nomination", ("nomination date", "nominated")),
    ("comments",        "Comments",          ("comment", "note", "notes")),
    ("site",            "Which YcharOS site", ("site", "which ycharos site", "ycharos site")),
    ("protein",         "protein name",      ("protein",)),
    ("gene",            "gene name",         ("gene", "gene symbol")),
    ("alternative",     "alternative protein name", ("alternative name", "alt name")),
    ("uniprot",         "Uniprot ID",        ("uniprot", "accession")),
    ("mass",            "Theoretical Molecular Mass (kDa)",
     ("mass", "molecular mass", "mass kda", "theoretical mass")),
    ("essential",       "Essential gene (depmap)?", ("essential gene", "essential")),
    ("ab_requested",    "Antibodies requested?", ("antibodies requested",)),
    ("status_note",     "Status",            ("status note",)),
    ("conclusion",      "Conclusion",        ("conclusion note",)),
    ("zenodo_doi",      "Zenodo DOI",        ("zenodo", "zenodo link")),
    ("zenodo_date",     "Date added on Zenodo", ("zenodo date",)),
    ("f1000_priority",  "To prioritize for F1000", ("prioritise for f1000", "f1000 priority")),
    ("f1000",           "On F1000",          ("f1000", "f1000 doi", "f1000 link")),
    ("f1000_date",      "Published on F1000", ("f1000 published", "f1000 date")),
]
KEYS = [c[0] for c in COLUMNS]
HEADERS = {c[0]: c[1] for c in COLUMNS}

# Extra read-only columns on the "board" layout — things the spreadsheet cannot
# hold because they are computed, not typed.
DERIVED_COLUMNS = [
    ("sites_all", "Sites pursuing"),
    ("classes", "Protein classes"),
    ("family", "Gene family"),
    ("completed", "Completed"),
    ("applications", "Applications done"),
    ("antibody_count", "Antibodies logged"),
]

BOARD_URL = "https://onlygoodantibodies.co.uk/pipeline/targets/board/"

# Shown above the headers in the original layout. Deliberately three lines: what
# this is, how to get a change back in, and the rule that stops people worrying
# about breaking something.
_TIP_LINES = [
    "OGA target board — your usual columns A to T. Edit anything below and upload the file back.",
    f"Upload at {BOARD_URL} → 'Upload a sheet'. You get a preview of every change before anything is saved.",
    "An empty cell never deletes anything. To replace a value that is already filled in, tick "
    "'Update existing values' when you upload. Full notes on the 'Read me' tab.",
]

# Hover notes on the header cells. Only where the answer isn't obvious — a note
# on every column is a note nobody reads.
COLUMN_TIPS = {
    "granting_agency": "Free text. A new funder name is created automatically; "
                       "an existing one is matched, so spelling matters.",
    "project": "Free text, matched the same way as the funder.",
    "funding": "'Funding available' or 'Funding not available'. Leave blank if "
               "you don't know — blank changes nothing.",
    "nominated": "Any format: 2020, 2021-05, april 2024, 03/22/2023. Whatever "
                 "you type is kept exactly as written and comes back the same.",
    "comments": "Free text, kept as written.",
    "site": "Which YCharOS site is doing it. This is what lets the board spot "
            "the same target running at two sites.",
    "gene": "The only column that must be filled in. The same gene can appear "
            "on several rows under different projects — that is expected, and "
            "each row is kept as a separate nomination.",
    "protein": "Filled from UniProt. If yours differs it is flagged on upload "
               "for you to check, never silently overwritten.",
    "uniprot": "Filled from UniProt. Flagged rather than overwritten if it differs.",
    "mass": "Filled from UniProt. Flagged rather than overwritten if it differs.",
    "essential": "YES / NO / slightly. Your reading of DepMap — we don't derive this.",
    "status_note": "Kept as a note. The board works from 'funded' and "
                   "'completed' instead, so nothing depends on what you type here.",
    "conclusion": "Kept as a note, same as Status.",
    "zenodo_doi": "The DOI (10.5281/zenodo.16812915) or the record's address. "
                  "This is what marks a target completed — you don't set "
                  "'completed' anywhere by hand, and clearing the cell on the "
                  "board opens the gene back up.",
    "f1000": "A link, or a note like 'coming' or 'publish elsewhere'. Both are "
             "kept. A note goes in its own field, so it never becomes a link "
             "that points nowhere.",
    "f1000_date": "A date here also marks the target completed.",
    "sites_all": "Computed — every site pursuing this target, across the consortium.",
    "classes": "Computed from UniProt keywords and GO terms. Used for the "
               "portfolio page ('how many GPCRs have we done').",
    "family": "Computed from the gene symbol, e.g. RAB11A → RAB.",
    "completed": "Computed — a Zenodo report or a published F1000 paper exists.",
    "applications": "Computed from real sessions and completed assignments, and "
                    "which site ran them. Not every target gets all four.",
    "antibody_count": "Computed — antibodies logged against this target.",
}

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}

_NULLISH = {"", "-", "--", "n/a", "na", "none", "?", "tbd"}


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------

def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.date().isoformat() if isinstance(v, dt.datetime) else v.isoformat()
    return str(v).strip()


def _clean(v) -> str:
    """Text with Carl's placeholders treated as empty."""
    s = _text(v)
    return "" if s.lower() in _NULLISH else s


def parse_date(v):
    """Return ``(date|None, raw_text)``. Never guesses a day or month it wasn't
    given — ``2020`` becomes 1 Jan 2020 *and* keeps ``2020`` as the raw text, so
    the round-trip can put back what he wrote rather than a date he never typed.
    """
    raw = _text(v)
    if isinstance(v, dt.datetime):
        return v.date(), ""
    if isinstance(v, dt.date):
        return v, ""
    s = _clean(v)
    if not s:
        return None, raw if raw and raw.lower() in _NULLISH else ""
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return dt.date(int(m.group(1)), 1, 1), s
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), 1), s
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return dt.date(*(int(g) for g in m.groups())), ""
        except ValueError:
            return None, s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)   # US order, as typed
    if m:
        mm, dd, yy = (int(g) for g in m.groups())
        try:
            return dt.date(yy, mm, dd), ""
        except ValueError:
            return None, s
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return dt.date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1), s
    return None, s


def parse_funding(v):
    """``Funding available`` → True, ``Funding not available`` → False, blank →
    None (meaning "don't change it")."""
    s = _clean(v).lower()
    if not s:
        return None
    if "not available" in s or s in ("no", "false", "unfunded"):
        return False
    if "available" in s or s in ("yes", "true", "funded"):
        return True
    return None


def _decimal(v):
    s = _clean(v).replace(",", ".")
    if not s:
        return None
    try:
        return round(float(re.sub(r"[^0-9.\-]", "", s)), 2)
    except (TypeError, ValueError):
        return None


def _as_link(s: str) -> str:
    """A DOI column's value as an absolute address, or ``""`` if it is not one.

    This was ``_is_url``, a boolean that answered "does this start with http, or
    doi.org, or 10." — and then the raw text was stored. So a bare
    ``10.5281/zenodo.1`` from the sheet went into a URL column unchanged and every
    screen drew it as a **relative** link, which the browser resolves against this
    site and answers with a Not Found page. ``services/doi.py`` is the one reader
    now, and it normalises rather than merely recognising, so the sheet and the
    board's own cell cannot disagree about what a DOI is.
    """
    return doi_svc.parse(s)[0]


def _norm_header(s) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", _text(s).lower()).strip()


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _match_header(cell) -> str:
    """Canonical key for a header cell, or ""."""
    n = _norm_header(cell)
    if not n:
        return ""
    for key, header, aliases in COLUMNS:
        if n == _norm_header(header):
            return key
    for key, header, aliases in COLUMNS:
        if n in {_norm_header(a) for a in aliases}:
            return key
    return ""


def _find_header_row(rows, scan=15):
    """The header row is whichever of the first rows names the gene column. His
    sheet puts it on row 5 under a merged group header; a cleaned-up export puts
    it on row 1. Both are found the same way."""
    best, best_hits = None, 0
    for idx, row in enumerate(rows[:scan]):
        mapping = {}
        for col, cell in enumerate(row):
            key = _match_header(cell)
            if key and key not in mapping:
                mapping[key] = col
        if "gene" in mapping and len(mapping) > best_hits:
            best, best_hits = (idx, mapping), len(mapping)
    return best or (None, {})


# Excel records a sheet's dimension in the file, and it is frequently wrong.
# Carl's workbook declares A1:AV1048539 — every row Excel has ever touched with
# formatting — while holding 373 rows of data. openpyxl believes the declaration,
# so reading the sheet naively materialises a million rows of 48 columns: fifty
# million cells to reach three hundred. That is what made the upload hang.
# Stopping after a run of blank rows bounds the work by the real data instead.
_MAX_TRAILING_BLANKS = 50
_MAX_ROWS = 100_000


def _read_grid(ws) -> list[list]:
    """Rows up to the end of the real data, not the end of the declared range."""
    grid: list[list] = []
    blanks = 0
    for row in ws.iter_rows(values_only=True):
        if any(c is not None and str(c).strip() for c in row):
            blanks = 0
        else:
            blanks += 1
            # Only stop once something has been seen — a sheet may open with a
            # run of blank rows above its header, as this one does.
            if blanks > _MAX_TRAILING_BLANKS and grid:
                break
        grid.append(list(row))
        if len(grid) >= _MAX_ROWS:
            break
    # Drop the trailing run of blanks so downstream row numbers stay honest.
    while grid and not any(c is not None and str(c).strip() for c in grid[-1]):
        grid.pop()
    return grid


def parse_workbook(f) -> dict:
    """Read an uploaded .xlsx/.csv into row dicts keyed by ``COLUMNS`` keys."""
    name = (getattr(f, "name", "") or "").lower()
    if name.endswith((".csv", ".tsv", ".txt")):
        import csv
        raw = f.read().decode("utf-8-sig", errors="replace")
        delim = "\t" if name.endswith((".tsv", ".txt")) else ","
        grid = [list(r) for r in csv.reader(io.StringIO(raw), delimiter=delim)]
        sheet_name = name
    else:
        import openpyxl
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = next((s for s in wb.worksheets
                   if _norm_header(s.title) == _norm_header(SHEET_TITLE)), wb.worksheets[0])
        sheet_name = ws.title
        grid = _read_grid(ws)
        wb.close()

    header_idx, mapping = _find_header_row(grid)
    if header_idx is None:
        return {"ok": False, "rows": [],
                "error": "No header row with a 'gene name' column was found — "
                         "is this the target list?"}

    rows = []
    for n, raw_row in enumerate(grid[header_idx + 1:], start=header_idx + 2):
        rec = {k: (raw_row[c] if c < len(raw_row) else None)
               for k, c in mapping.items()}
        if not _clean(rec.get("gene")):
            continue
        rec["_row"] = n
        rows.append(rec)

    return {"ok": True, "rows": rows, "sheet": sheet_name,
            "header_row": header_idx + 1,
            "columns_found": sorted(mapping), "error": None,
            "columns_missing": sorted(set(KEYS) - set(mapping))}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

# Identity fields we can source better than a typed cell (UniProt). A difference
# here is reported as a conflict for review, never silently applied.
_IDENTITY = {
    "protein": "protein_name",
    "alternative": "alternative_name",
    "uniprot": "uniprot_id",
    "mass": "theoretical_mass_kda",
}


class _Index:
    """Everything the import needs, loaded in a handful of queries.

    A file this size (373 rows) against a remote database cannot afford a lookup
    per row: doing it that way meant roughly 750 round-trips for a preview, which
    on Render's Postgres is tens of seconds of latency and nothing else. Loading
    the four tables once and matching in memory makes it a fixed cost regardless
    of how many rows the file has.
    """

    def __init__(self):
        self.targets = {}
        self.by_accession = {}
        for t in Target.objects.using(DB).only(
                "id", "gene_name", "protein_name", "alternative_name",
                "uniprot_id", "theoretical_mass_kda", "essential_gene"):
            if t.gene_name:
                self.targets[t.gene_name.strip().lower()] = t
            if t.uniprot_id:
                self.by_accession[t.uniprot_id.strip().upper()] = t

        self.nominations = {}
        for n in (TargetNomination.objects.using(DB)
                  .select_related("project", "granting_agency", "site")):
            self.nominations.setdefault(n.target_id, []).append(n)

        self.sites = {s.name.strip().lower(): s
                      for s in Site.objects.using(DB).only("id", "name", "short_code")}
        self.agencies = {a.name.strip().lower(): a
                         for a in GrantingAgency.objects.using(DB).only("id", "name")}
        self.projects = {p.name.strip().lower(): p
                         for p in Project.objects.using(DB).only("id", "name")}

    def target_for(self, gene: str, accession: str = ""):
        t = self.targets.get((gene or "").strip().lower())
        if t is None and accession:
            t = self.by_accession.get(accession.strip().upper())
        return t

    def nomination_for(self, target, agency_name, project_name, site_name):
        """Match a row to an existing nomination on (project, agency, site) so
        re-uploading the same file updates rather than duplicates."""
        if target is None:
            return None
        for n in self.nominations.get(target.pk, ()):
            if ((n.project.name if n.project else "") == (project_name or "")
                    and (n.granting_agency.name if n.granting_agency else "") == (agency_name or "")
                    and (n.site.name if n.site else "") == (site_name or "")):
                return n
        return None

    def remember(self, target, nomination=None):
        if target.gene_name:
            self.targets[target.gene_name.strip().lower()] = target
        if target.uniprot_id:
            self.by_accession[target.uniprot_id.strip().upper()] = target
        if nomination is not None:
            self.nominations.setdefault(target.pk, []).append(nomination)


def plan(parsed: dict, *, default_site: str = "") -> dict:
    """Read-only: what an upload would do, row by row, with conflicts surfaced."""
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error"), "items": []}

    index = _Index()
    items, seen_genes = [], {}
    for rec in parsed["rows"]:
        gene = _clean(rec.get("gene"))
        agency = _clean(rec.get("granting_agency"))
        project = _clean(rec.get("project"))
        site = _clean(rec.get("site")) or default_site
        target = index.target_for(gene, _clean(rec.get("uniprot")))
        nomination = index.nomination_for(target, agency, project, site)

        conflicts = []
        if target is not None:
            for key, field in _IDENTITY.items():
                incoming = (_decimal(rec.get(key)) if key == "mass"
                            else _clean(rec.get(key)))
                if incoming in ("", None):
                    continue
                current = getattr(target, field, None)
                if current in ("", None):
                    continue
                same = (abs(float(current) - float(incoming)) < 0.51
                        if key == "mass" else
                        str(current).strip().lower() == str(incoming).strip().lower())
                if not same:
                    conflicts.append({"field": HEADERS[key],
                                      "in_file": str(incoming),
                                      "in_database": str(current)})

        dup_key = gene.upper()
        duplicate_of = seen_genes.get(dup_key)
        seen_genes.setdefault(dup_key, rec["_row"])

        items.append({
            "row": rec["_row"],
            "gene": gene,
            "target_action": "update" if target else "create",
            "target_id": target.pk if target else None,
            "nomination_action": "update" if nomination else "create",
            "site": site,
            "project": project,
            "agency": agency,
            "funded": parse_funding(rec.get("funding")),
            "report_action": ("update-or-create"
                              if (_clean(rec.get("zenodo_doi")) or _clean(rec.get("f1000")))
                              else "none"),
            "conflicts": conflicts,
            # Two rows, same gene: expected and supported — the second becomes a
            # second nomination on the same target rather than a rejected row.
            "second_nomination_for": duplicate_of,
        })

    return {
        "ok": True,
        "items": items,
        "summary": {
            "rows": len(items),
            "new_targets": sum(1 for i in items if i["target_action"] == "create"),
            "existing_targets": sum(1 for i in items if i["target_action"] == "update"),
            "new_nominations": sum(1 for i in items if i["nomination_action"] == "create"),
            "conflicts": sum(len(i["conflicts"]) for i in items),
            "repeat_genes": sum(1 for i in items if i["second_nomination_for"]),
            "sites": sorted({i["site"] for i in items if i["site"]}),
        },
        "columns_missing": [HEADERS[k] for k in parsed.get("columns_missing", [])],
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _resolve_site(name: str, index=None):
    """Sites, funders and projects repeat on almost every row, so they are
    resolved against the preloaded maps and only hit the database when the name
    is genuinely new."""
    name = (name or "").strip()
    if not name:
        return None
    cache = index.sites if index else {}
    site = cache.get(name.lower())
    if site:
        return site
    site = Site.objects.using(DB).filter(name__iexact=name).first()
    if site is None:
        code = re.sub(r"[^A-Za-z]", "", name).upper()[:3] or "SIT"
        base, n = code, 1
        while Site.objects.using(DB).filter(short_code=code).exists():
            n += 1
            code = f"{base}{n}"[:10]
        site = Site.objects.using(DB).create(name=name, short_code=code)
    cache[name.lower()] = site
    return site


def _resolve_named(model, name: str, cache=None):
    name = (name or "").strip()
    if not name:
        return None
    cache = cache if cache is not None else {}
    obj = cache.get(name.lower())
    if obj:
        return obj
    obj = (model.objects.using(DB).filter(name__iexact=name).first()
           or model.objects.using(DB).create(name=name))
    cache[name.lower()] = obj
    return obj


def _fill(obj, field, value, *, overwrite: bool) -> bool:
    """Fill-only-blank unless overwrite. A blank incoming value never clears."""
    if value in ("", None):
        return False
    current = getattr(obj, field, None)
    if current not in ("", None) and not overwrite:
        return False
    if current == value:
        return False
    setattr(obj, field, value)
    return True


def apply(parsed: dict, *, member=None, apply_overwrites: bool = False,
          default_site: str = "", create_targets: bool = True) -> dict:
    """Upsert the parsed rows. One transaction; nothing is ever deleted."""
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error")}

    out = {"ok": True, "targets_created": [], "targets_updated": 0,
           "nominations_created": 0, "nominations_updated": 0,
           "reports_touched": 0, "skipped": [], "overwritten": 0}

    index = _Index()
    with transaction.atomic(using=DB):
        for rec in parsed["rows"]:
            gene = _clean(rec.get("gene"))
            if not gene:
                continue

            up = _clean(rec.get("uniprot"))
            target = index.target_for(gene, up)
            if target is None:
                if not create_targets:
                    out["skipped"].append(gene)
                    continue
                # Deliberately NOT enriched from UniProt here. A file this size
                # would make one network call per new gene inside the request —
                # minutes of latency and a near-certain gateway timeout — and it
                # is redundant anyway, because the file already carries the
                # protein name, accession and mass. Anything still missing is
                # filled afterwards by ``enrich_targets_from_uniprot``.
                target = Target(gene_name=gene, status=Target.Status.NOT_STARTED)
                if member is not None and getattr(member, "pk", None):
                    target.created_by_id = member.pk
                out["targets_created"].append(gene)

            changed = target.pk is None
            changed |= _fill(target, "protein_name", _clean(rec.get("protein")),
                             overwrite=apply_overwrites)
            changed |= _fill(target, "alternative_name", _clean(rec.get("alternative")),
                             overwrite=apply_overwrites)
            # uniprot_id is unique; only claim it if no other target holds it.
            if up and index.by_accession.get(up.upper(), target) is target:
                changed |= _fill(target, "uniprot_id", up, overwrite=apply_overwrites)
            changed |= _fill(target, "theoretical_mass_kda", _decimal(rec.get("mass")),
                             overwrite=apply_overwrites)
            changed |= _fill(target, "essential_gene", _clean(rec.get("essential")),
                             overwrite=apply_overwrites)
            if changed:
                was_new = target.pk is None
                target.save(using=DB)
                if not was_new:
                    out["targets_updated"] += 1
                index.remember(target)

            # --- nomination (the repeatable half: one gene, many nominations) ---
            agency_name = _clean(rec.get("granting_agency"))
            project_name = _clean(rec.get("project"))
            site_name = _clean(rec.get("site")) or default_site
            nomination = index.nomination_for(target, agency_name, project_name, site_name)
            if nomination is None:
                nomination = TargetNomination(target_id=target.pk)
                if member is not None and getattr(member, "pk", None):
                    nomination.created_by_id = member.pk
                is_new = True
            else:
                is_new = False

            site = _resolve_site(site_name, index)
            if site and (nomination.site_id is None or apply_overwrites):
                nomination.site_id = site.pk
                nomination.site = site
            agency = _resolve_named(GrantingAgency, agency_name, index.agencies)
            if agency and (nomination.granting_agency_id is None or apply_overwrites):
                nomination.granting_agency_id = agency.pk
                nomination.granting_agency = agency
            project = _resolve_named(Project, project_name, index.projects)
            if project and (nomination.project_id is None or apply_overwrites):
                nomination.project_id = project.pk
                nomination.project = project

            funded = parse_funding(rec.get("funding"))
            if funded is not None and (is_new or apply_overwrites or not nomination.funded):
                nomination.funded = funded
            nom_date, nom_raw = parse_date(rec.get("nominated"))
            if nom_date and (nomination.date_of_nomination is None or apply_overwrites):
                nomination.date_of_nomination = nom_date
            _fill(nomination, "date_text", nom_raw, overwrite=apply_overwrites)
            _fill(nomination, "comments", _clean(rec.get("comments")),
                  overwrite=apply_overwrites)
            _fill(nomination, "status_note", _clean(rec.get("status_note"))[:120],
                  overwrite=apply_overwrites)
            _fill(nomination, "conclusion_note", _clean(rec.get("conclusion"))[:120],
                  overwrite=apply_overwrites)
            _fill(nomination, "antibodies_requested",
                  _clean(rec.get("ab_requested"))[:40], overwrite=apply_overwrites)
            nomination.save(using=DB)
            if is_new:
                index.nominations.setdefault(target.pk, []).append(nomination)
            out["nominations_created" if is_new else "nominations_updated"] += 1

            # --- dissemination ---
            zen = _clean(rec.get("zenodo_doi"))
            f1000 = _clean(rec.get("f1000"))
            f1000_pri = _clean(rec.get("f1000_priority"))
            if zen or f1000 or f1000_pri:
                report = (Report.objects.using(DB)
                          .filter(target_id=target.pk).order_by("pk").first())
                if report is None:
                    report = Report(target_id=target.pk)
                touched = False
                # The sheet is the record of what has been published, so a Zenodo
                # cell this cannot read is **kept**, not dropped — his file is the
                # only copy of some of it, and a value nobody can see is a value
                # nobody can fix. It is normalised where we understand it, and the
                # board draws the rest as text rather than as a link that goes
                # nowhere. A *typed* cell is refused outright (views/target_board.
                # py::_save_report_field): there, the person is present to correct
                # it, and the whole point of an import is that nobody is.
                touched |= _fill(report, "zenodo_doi", _as_link(zen) or zen,
                                 overwrite=apply_overwrites)
                zdate, _ = parse_date(rec.get("zenodo_date"))
                if zdate and (report.zenodo_date is None or apply_overwrites):
                    report.zenodo_date = zdate
                    touched = True
                if f1000:
                    # 'coming', 'publish elsewhere' — a note, kept in the field
                    # that is for notes rather than in a URL column.
                    link = _as_link(f1000)
                    touched |= _fill(report, "f1000_doi" if link else "f1000_note",
                                     (link or f1000)[:120], overwrite=apply_overwrites)
                touched |= _fill(report, "f1000_priority", f1000_pri[:120],
                                 overwrite=apply_overwrites)
                fdate, _ = parse_date(rec.get("f1000_date"))
                if fdate and (report.f1000_date is None or apply_overwrites):
                    report.f1000_date = fdate
                    touched = True
                if touched or report.pk is None:
                    if report.zenodo_doi or report.f1000_date:
                        report.status = Report.ReportStatus.PUBLISHED
                    report.save(using=DB)
                    out["reports_touched"] += 1

    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _export_rows(rows) -> list[dict]:
    """Board rows → one output row per nomination (his file's grain), so a gene
    nominated twice comes back out as two rows exactly as it went in."""
    out = []
    for r in rows:
        noms = r["nominations"] or [{}]
        for n in noms:
            out.append({
                "granting_agency": n.get("agency", ""),
                "project": n.get("project", ""),
                "funding": ("Funding available" if n.get("funded")
                            else "Funding not available" if n else ""),
                "nominated": n.get("date_text") or n.get("date", ""),
                "comments": n.get("comments", ""),
                "site": n.get("site", ""),
                "protein": r["protein"],
                "gene": r["gene"],
                "alternative": r["alternative"],
                "uniprot": r["uniprot"],
                "mass": r["mass_kda"],
                "essential": r.get("essential_gene", ""),
                "ab_requested": n.get("antibodies_requested", ""),
                "status_note": n.get("status_note", ""),
                "conclusion": n.get("conclusion_note", ""),
                "zenodo_doi": r["zenodo_doi"],
                "zenodo_date": r["zenodo_date"],
                "f1000_priority": r["f1000_priority"],
                "f1000": r["f1000_doi"],
                "f1000_date": r["f1000_date"],
                # derived
                "sites_all": ", ".join(r["sites"]),
                "classes": ", ".join(r["classes"]),
                "family": r["family"],
                "completed": "YES" if r["completed"] else "NO",
                "applications": ", ".join(
                    f"{app} ({'/'.join(sites)})"
                    for app, sites in sorted(r["coverage"].items())),
                "antibody_count": r["antibody_count"],
            })
    return out


def build_workbook(rows, layout: str = "board") -> bytes:
    """``layout='carl'`` reproduces his sheet (A–T, headers on row 5) so his own
    filters and habits survive. ``layout='board'`` is the same data plus the
    computed columns, headers on row 1. Both re-import through ``parse_workbook``.
    """
    import openpyxl
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    carl = layout == "carl"
    cols = list(COLUMNS) if carl else (
        list(COLUMNS) + [(k, h, ()) for k, h in DERIVED_COLUMNS])
    data = _export_rows(rows)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE[:31]

    header_row = 5 if carl else 1
    if carl:
        # His layout keeps four rows above the headers (the old file used them
        # for a colour legend). They cost nothing and are the one place a tip is
        # guaranteed to be seen, so the short version of the instructions goes
        # here and the long version on the "Read me" tab.
        for row, text in enumerate(_TIP_LINES, start=1):
            cell = ws.cell(row, 1, text)
            cell.font = Font(bold=(row == 1), color="01599F" if row == 1 else "555555",
                             size=11 if row == 1 else 10)
        # The group header he reads across the identity block.
        ws.cell(4, 7, "Target information").font = Font(bold=True)
        ws.merge_cells(start_row=4, start_column=7, end_row=4, end_column=12)
        ws.cell(4, 7).alignment = Alignment(horizontal="center")

    for i, (key, header, _aliases) in enumerate(cols, start=1):
        c = ws.cell(header_row, i, header)
        c.font = Font(bold=True, color="FFFFFF")
        # Computed columns get a different header colour, so it is obvious at a
        # glance which cells are worth typing in and which are filled for you.
        c.fill = PatternFill("solid",
                             fgColor="064C83" if key in KEYS else "6B7A8C")
        c.alignment = Alignment(vertical="center", wrap_text=True)
        note = COLUMN_TIPS.get(key)
        if note:
            c.comment = Comment(note, "OGA")
            c.comment.width = 320
            c.comment.height = 110
        ws.column_dimensions[get_column_letter(i)].width = min(
            46, max(12, len(header) + 4))

    for r, rec in enumerate(data, start=header_row + 1):
        for i, (key, _h, _a) in enumerate(cols, start=1):
            ws.cell(r, i, rec.get(key, ""))

    last = header_row + len(data)
    ws.freeze_panes = f"A{header_row + 1}"
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(cols))}{max(last, header_row)}"

    funding_col = get_column_letter(KEYS.index("funding") + 1)
    dv = DataValidation(
        type="list", formula1='"Funding available,Funding not available"',
        allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"{funding_col}{header_row + 1}:{funding_col}{max(last, header_row + 1)}")

    _add_readme_sheet(wb, carl)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _add_readme_sheet(wb, carl: bool):
    """A short guide travelling with the file, so the person who opens it in six
    months doesn't have to find an email to know how it works."""
    from openpyxl.styles import Alignment, Font

    ws = wb.create_sheet("Read me")
    head = Font(bold=True, size=13, color="01599F")
    sub = Font(bold=True, size=11)

    def line(text="", font=None, indent=0):
        ws.append([("    " * indent) + text])
        if font:
            ws.cell(ws.max_row, 1).font = font

    line("The OGA target board", head)
    line()
    line("This file is a snapshot of the target list. Editing it and uploading it "
         "back is fully supported — you never have to work in the browser if you "
         "would rather not.")
    line()

    line("To change something", sub)
    line("1. Edit any cell on the 'Complete target list' tab and save the file.", indent=1)
    line(f"2. Go to {BOARD_URL} and choose 'Upload a sheet'.", indent=1)
    line("3. Press 'Preview changes'. Nothing is saved yet — you see exactly what "
         "would be created or changed, and anything that disagrees with the "
         "database.", indent=1)
    line("4. Press 'Save these changes' when you are happy.", indent=1)
    line()

    line("Things that are safe", sub)
    line("• An empty cell never erases anything. Deleting a value in the "
         "spreadsheet does not delete it in the database.", indent=1)
    line("• Uploading the same file twice changes nothing the second time.", indent=1)
    line("• Nothing is ever deleted by an upload. Rows you remove from the file "
         "are simply left alone.", indent=1)
    line("• To replace a value that is already filled in, tick 'Update existing "
         "values'. Without that tick, only blanks are filled.", indent=1)
    line()

    line("Two things worth knowing", sub)
    line("• The same gene can appear on more than one row — funded under one "
         "project and on a wish list under another. Both rows are kept, as two "
         "nominations of one target. You do not need to tidy them up.", indent=1)
    line("• 'Completed' is worked out from the Zenodo DOI or a published F1000 "
         "date. There is no completed column to fill in: add the DOI and it "
         "follows.", indent=1)
    line()

    line("Where each column comes from", sub)
    ws.append(["Column", "Source"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    typed_origin = {
        "granting_agency": "You type it", "project": "You type it",
        "funding": "You type it", "nominated": "You type it",
        "comments": "You type it", "site": "You type it",
        "protein": "UniProt — flagged, never overwritten, if yours differs",
        "alternative": "UniProt — flagged if yours differs",
        "uniprot": "UniProt — flagged if yours differs",
        "mass": "UniProt — flagged if yours differs",
        "essential": "You type it (your reading of DepMap)",
        "ab_requested": "You type it", "status_note": "You type it (kept as a note)",
        "conclusion": "You type it (kept as a note)",
        "zenodo_doi": "You type it — this is what marks a target completed",
        "zenodo_date": "You type it", "f1000_priority": "You type it",
        "f1000": "You type it", "f1000_date": "You type it — also marks completed",
    }
    for key, header, _aliases in COLUMNS:
        ws.append([header, typed_origin.get(key, "You type it")])
    if not carl:
        for key, header in DERIVED_COLUMNS:
            ws.append([header, "Computed — editing it here has no effect"])

    line()
    line("If something looks wrong", sub)
    line("Nothing you do in this file can damage the database — every upload is "
         "previewed first, and nothing is ever deleted. If a preview shows "
         "something you did not expect, close it without saving and ask "
         "Harvinder.", indent=1)

    ws.column_dimensions["A"].width = 96
    ws.column_dimensions["B"].width = 58
    for row in ws.iter_rows(min_col=1, max_col=1):
        row[0].alignment = Alignment(vertical="top", wrap_text=True)


def export_bytes(layout: str = "board", **filters) -> bytes:
    rows = target_board.board_rows(**filters)
    # essential_gene isn't on the board row (it isn't shown); fetch for export.
    essentials = dict(Target.objects.using(DB)
                      .filter(pk__in=[r["id"] for r in rows])
                      .values_list("pk", "essential_gene"))
    for r in rows:
        r["essential_gene"] = essentials.get(r["id"], "")
    return build_workbook(rows, layout=layout)
