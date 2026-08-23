"""
Bulk cell-line paste — add/update many cell lines at once from a pasted list,
the cell-line counterpart of services/bulk_antibodies.py.

Cell lines differ from antibodies in three ways that shape this module:
  - a KO line is always associated with a WT parent (parent_line); one WT may
    parent many KOs;
  - a C-number is a freeze-down BATCH with its own storage, not the line's
    identity — so a C-number becomes a CellLineVial hung off the CellLine;
  - WT lines are often consortium-wide (no target); only KO lines need a gene.

Reuses the shared pieces so nothing drifts:
  - services.targets.resolve_or_create_target — inline target creation (enriched)
  - services.cropper.db.resolve_company        — dedup-safe supplier resolution

`plan()` is read-only (preview + dry-run). `apply()` writes, filling only blank
fields so re-pasting never clobbers data.
"""
from __future__ import annotations

import re
from datetime import date

from django.db import transaction
from django.db.models import Q

from pipeline.models import CellLine, CellLineVial, InventoryLocation
from pipeline.services import c_number as c_number_svc
from pipeline.services import lab_numbers
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import example_row
from pipeline.services import sites as site_svc
from pipeline.services import targets as target_svc
from pipeline.services.cropper import db as cdb
from pipeline.services.targets import resolve_or_create_target, resolve_target

DB = "pipeline_db"

# The parental line named in the live data — used in a refusal so the example is
# one of this lab's own rows rather than an invented one.
_PARENT_NAME_EG = "HAP1"

# Header aliases → canonical row keys.
HEADER_ALIASES = {
    "name": "name", "cell line": "name", "cell line name": "name", "line": "name",
    "gene": "gene", "gene name": "gene", "gene symbol": "gene", "target": "gene", "symbol": "gene",
    "genotype": "genotype", "type": "genotype", "geno": "genotype",
    "parent": "parent", "parental": "parent", "parental line": "parent",
    "parent line": "parent", "wt parent": "parent", "wt": "parent",
    "c number": "c_number", "c-number": "c_number", "c#": "c_number", "cnumber": "c_number",
    "c no": "c_number", "lab label": "c_number", "cnum": "c_number",
    "cellosaurus": "cellosaurus_id", "cellosaurus id": "cellosaurus_id",
    "rrid": "cellosaurus_id", "cvcl": "cellosaurus_id",
    "company": "company", "supplier": "company", "vendor": "company",
    "source": "company", "manufacturer": "company",
    "catalogue": "catalogue", "catalog": "catalogue", "catalogue number": "catalogue",
    "catalog number": "catalogue", "cat": "catalogue", "cat no": "catalogue",
    "cat number": "catalogue", "cat#": "catalogue", "product": "catalogue",
    "lot": "lot", "lot number": "lot", "lot no": "lot", "lot #": "lot",
    # Whose line this is. In the export so a downloaded sheet can go back without
    # every row being re-homed to whoever uploads it (services/sites.py).
    "site": "site", "lab": "site", "institution": "site", "centre": "site",
    "center": "site", "node": "site",
    "medium": "medium", "media": "medium", "growth medium": "medium",
    "clone": "clone", "clone id": "clone",
    "species": "species", "origin": "origin",
    # Where a line came from, in a person's own words. The model has carried this
    # since the Access import and nothing exposed it, so tagging a batch of new
    # lines meant creating them and then editing every row one cell at a time.
    "comments": "origin_comments", "comment": "origin_comments",
    "notes": "origin_comments", "note": "origin_comments",
    "origin comments": "origin_comments", "provenance": "origin_comments",
    # the paired partner's C-number (WT/KO shipped together, matched on C-number)
    "paired ko": "pair_c", "paired ko c": "pair_c", "ko c number": "pair_c",
    "ko c#": "pair_c", "ko cnumber": "pair_c", "pair c": "pair_c", "paired c": "pair_c",
    "pair c number": "pair_c", "paired c number": "pair_c", "paired": "pair_c",
    # The heading says who the column is for, because a knockout does not have a
    # paired knockout and the bare name read as though every row needed one. Both
    # spellings are aliases so sheets downloaded before the rename still load.
    "paired ko (wt rows)": "pair_c", "paired ko wt rows": "pair_c",
    "paired ko (wt)": "pair_c", "paired ko (for wt lines)": "pair_c",
    # storage of the vial (freeze-down batch)
    "storage type": "storage_type", "temp": "storage_type", "temperature": "storage_type",
    "storage temp": "storage_type",
    "storage": "location", "storage location": "location", "location": "location",
    "freezer": "freezer", "box": "box", "position": "position", "pos": "position",
    "rack": "rack", "shelf": "shelf", "building": "building", "room": "room",
}

ROW_KEYS = ("name", "gene", "genotype", "parent", "c_number", "cellosaurus_id",
            "company", "catalogue", "lot", "site", "medium", "clone", "species",
            "origin_comments",
            "origin", "pair_c", "storage_type", "location", "freezer", "box",
            "position", "rack", "shelf", "building", "room")

# storage columns that, if any are present, mean "record where this vial lives"
_STORAGE_HIER = ("freezer", "box", "position", "rack", "shelf", "building", "room")


# ── parsing ──────────────────────────────────────────────────────────────────

def _cells(line: str):
    """Split one line into cells, picking a single consistent delimiter: a tab
    (Excel paste), then a comma (CSV), then runs of 2+ spaces."""
    if "\t" in line:
        parts = line.split("\t")
    elif "," in line:
        parts = line.split(",")
    else:
        parts = re.split(r"\s{2,}", line)
    return [p.strip() for p in parts]


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower()).strip(" .:#")


def _header_map(cells):
    """If a line is a header row (≥2 recognised columns), return its column→key
    map as a list aligned to the cells; else None."""
    keys = [HEADER_ALIASES.get(_norm_header(c)) for c in cells]
    if sum(1 for k in keys if k) >= 2:
        return keys
    return None


def parse(text: str, default_gene: str = "", default_genotype: str = ""):
    """Parse a pasted, header-led table into row dicts. Requires a header row so
    columns map unambiguously (cell-line tables vary too much to guess). Blank
    gene/genotype fall back to the page defaults.

    **A wild type never takes the default gene.** A gene's own page pastes with
    ``default_gene`` set to that gene, because every row on that page is about it
    — except the wild-type parent, which is the one row that must have no gene at
    all. One HAP1 WT serves every knockout made from it, so stamping a gene on it
    forks a second, wrong parental line the next time somebody works on a
    different gene, and the two are not obviously duplicates.

    The panel says so above the grid and the identity endpoint refuses the same
    state with a 400. The write path was the one place that did it anyway: the
    fourth field test left the gene column blank on a wild type, exactly as
    instructed, and got ``SH-SY5Y WT`` with ``gene = STMN2``.
    """
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip() and not example_row.is_example(_cells(ln))]
    header = None
    rows = []
    for ln in lines:
        cells = _cells(ln)
        if header is None:
            hm = _header_map(cells)
            if hm:
                header = hm
            continue
        row = {k: "" for k in ROW_KEYS}
        for key, val in zip(header, cells):
            if key:
                row[key] = val.strip()
        if not any(row.values()):
            continue
        # Genotype first: whether this row may take the default gene depends on it.
        if not row["genotype"]:
            row["genotype"] = (default_genotype or "").strip()
        if not row["gene"]:
            genotype = norm_genotype(row["genotype"], row["name"],
                                     bool(row["parent"]))
            if genotype != "WT":
                row["gene"] = (default_gene or "").strip()
        rows.append(row)
    return rows


# ── normalisation helpers ────────────────────────────────────────────────────

def norm_genotype(raw: str, name: str = "", has_parent: bool = False,
                  gene_not_applicable: bool = False) -> str:
    """Map free text to WT / KO / other. If blank, infer: a parent or a name
    containing 'KO' implies a knockout; otherwise wild type.

    ``gene_not_applicable`` is a gene column that said ``NA``. It is a statement
    about the row — this is not a knockout of anything — so it settles a blank
    genotype in favour of WT even when a parent is named, which is the one case
    where the old inference disagreed with what the person wrote.
    """
    r = (raw or "").strip().lower()
    if r in ("wt", "wild type", "wildtype", "wild-type", "parental", "parent"):
        return "WT"
    if r in ("ko", "knockout", "knock-out", "knock out", "ko line", "-/-"):
        return "KO"
    if r:
        return "other"
    if gene_not_applicable:
        return "WT"
    up = (name or "").upper()
    if has_parent or " KO" in f" {up}" or up.endswith("KO") or "-/-" in up:
        return "KO"
    return "WT"


def _cnum(raw, *, field="c number"):
    """``(number, error)`` — ``services/c_number.py`` is the one reader.

    This used to be ``re.search(r"\\d+")``, which does not read a C-number so
    much as go looking for digits in whatever it is given: ``C-RUN11-01`` came
    back as **11**. See ``c_number.parse`` for what that cost.
    """
    return c_number_svc.parse(raw, field=field)


def _norm_cvcl(raw: str) -> str:
    return (raw or "").strip().upper()


def _derived_name(row, genotype):
    name = (row.get("name") or "").strip()
    if name:
        return name
    gene = (row.get("gene") or "").strip()
    if genotype == "KO" and gene:
        return f"{gene} KO"
    if gene:
        return gene
    return ""


# ── dedup ────────────────────────────────────────────────────────────────────

def match_note(row, line) -> str:
    """Why this row matched *that* line, in one clause.

    ``find_cell_line`` matches on Cellosaurus, then supplier + catalogue, then
    name — so a row you have named something new can legitimately land on a line
    already on file, and "already on file — will be updated" then reads as though
    the app has misread you. The fourth field test pasted `HAP1 WT [COWORK RUN4]`,
    saw *already on file*, and could not tell what it had matched or whether
    committing would rename somebody else's row. The antibody preview has named
    the matched vial since run 2; this is the same sentence for lines.
    """
    if line is None:
        return ""
    on_file = line.name or f"line {line.pk}"
    cvcl = _norm_cvcl(row.get("cellosaurus_id"))
    if cvcl and (line.cellosaurus_id or "").upper() == cvcl:
        return f"matches “{on_file}”, which already has {cvcl}"
    cat = (row.get("catalogue") or "").strip()
    if cat and (line.catalogue_number or "").lower() == cat.lower():
        return (f"matches “{on_file}” on supplier and catalogue {cat} — the name "
                f"on file stays as it is")
    if (line.name or "").lower() != (row.get("name") or "").strip().lower():
        return f"matches “{on_file}” — the name on file stays as it is"
    return f"matches “{on_file}”, already on file"


def find_cell_line(row, target, db: str = DB, member=None, site_id=None):
    """Find the existing CellLine a row refers to: by Cellosaurus ID first
    (universal), then (supplier, catalogue, target), then (name, target),
    scoped to the site the row belongs to where one is known.

    ``site_id`` is the row's own site — its `site` column if the sheet has one,
    otherwise the member's. Passing it explicitly is what lets a downloaded sheet
    go back and match the rows it came from, rather than matching nothing and
    creating a copy of each under the uploader's site.
    """
    if site_id is None:
        site_id = getattr(member, "site_id", None)
    qs = CellLine.objects.using(db)

    # **A knockout row must never match a wild-type row.**
    #
    # The three lookups below separate the two by `target`, which works only
    # while the gene is known: a KO naming a gene that is not in the pipeline
    # yet resolves to `target=None`, so the name branch asked for a line called
    # HAP1 with no target — and found the *parental*. The preview then read
    # "already on file — will be updated", about the wild type, and a commit
    # would have filled that row's blanks from the knockout's cells.
    #
    # It was survivable while the example row taught `HAP1 SNCA KO`, because
    # those names are nearly unique. The real convention is the bare parental
    # name, and **437 of the 562 lines on file carry a name that some other row
    # of the opposite genotype also carries** — HAP1, HeLa, HCT116, SH-SY5Y,
    # U2OS. Teaching the true convention without this makes the collision the
    # normal case rather than a corner.
    #
    # A stored genotype that is blank still matches, so nothing from the Access
    # import falls out of its own sheet's round trip.
    want_genotype = norm_genotype(row.get("genotype"), row.get("name"),
                                  bool(row.get("parent")))
    same_kind = Q(genotype=want_genotype) | Q(genotype="") | Q(genotype__isnull=True)
    qs = qs.filter(same_kind)

    cvcl = _norm_cvcl(row.get("cellosaurus_id"))
    if cvcl:
        hit = qs.filter(cellosaurus_id__iexact=cvcl).first()
        if hit:
            return hit

    company = (cdb.resolve_company(row.get("company", ""), row.get("catalogue", ""),
                                   create=False, db=db) if row.get("company") else None)
    cat = (row.get("catalogue") or "").strip()
    if cat and company:
        f = qs.filter(catalogue_number__iexact=cat, company=company)
        f = f.filter(target=target) if target else f.filter(target__isnull=True)
        if site_id:
            f = f.filter(site_id=site_id)
        hit = f.first()
        if hit:
            return hit

    name = _derived_name(row, norm_genotype(row.get("genotype"), row.get("name"),
                                            bool(row.get("parent"))))
    if name:
        f = qs.filter(name__iexact=name)
        f = f.filter(target=target) if target else f.filter(target__isnull=True)
        if site_id:
            f = f.filter(site_id=site_id)
        hit = f.first()
        if hit:
            return hit
    return None


# ── plan (read-only preview) ─────────────────────────────────────────────────

def _row_site(row, member):
    """`(site_id, site_name, error)` — the row's own `site` column, else the
    member's. See the equivalent in ``bulk_antibodies``: an upload that ignores
    the column re-homes every row to whoever uploaded the file."""
    site_id, err = site_svc.for_row(row.get("site"), member=member)
    if err:
        return None, (row.get("site") or "").strip(), err
    if (row.get("site") or "").strip():
        site = site_svc.resolve(row["site"])
        return site_id, (site.name if site else ""), None
    return site_id, (getattr(getattr(member, "site", None), "name", "") or ""), None


def _parent_refs(pname):
    """A parent cell split into the references it holds, in written order.

    A line made from more than one parental is recorded as
    ``C-153/C-420/C-421`` — nineteen rows in the live database are written that
    way. ``parent_line`` is a single ForeignKey, so the first one that resolves
    is linked and the cell is kept verbatim in ``parental_line_name``; the note
    says both things happened rather than letting a value disappear into a
    field nobody reads.
    """
    raw = (pname or "").strip()
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[/;,]| and ", raw) if p.strip()]


def _pending_parent(refs, site_id, pending, any_on_file=True):
    """The wild type **this same paste is about to create**, if one answers to the
    parent cell — the pending entry (``name``/``site_id``/``row``) or ``None``.

    ``apply`` sorts wild types ahead of knockouts (see ``order`` there) precisely
    so a WT and the knockouts made from it can go in together, which is what both
    workbooks teach and what a gene's page asks for. The preview did not know
    that: it asked the database as it stands *now*, where the new WT does not
    exist yet, and answered from whatever else shares the name.

    So adding a Leicester ``U2OS`` WT and a Leicester ``TRPA1`` KO in one paste
    previewed as *parent "U2OS" is McGill's line — check that is the one you
    mean*, and then saved — correctly — onto the Leicester line, because by then
    ``_pick_parent`` had an own-site wild type to prefer. A warning that is true
    of the database and false of the write is the supplier-preview bug one
    surface over: a preview is only true if it asks what the write will ask.

    Own site only, and a ``create`` only. A pending row at another site does not
    beat an existing own-site WT, and a row that is blocked or already on file is
    not pending at all — ``plan`` passes only the rows that will be written.
    """
    for ref in refs:
        low = ref.strip().lower()
        for p in pending:
            if p["name"].strip().lower() != low:
                continue
            # Own site, or nothing else on file to compete with. `_pick_parent`
            # only prefers a site when there is one to prefer, so claiming the
            # pending row for a site-less paste would be guessing at an ordering
            # the database decides.
            if site_id and p["site_id"] == site_id:
                return p
            if not site_id and not any_on_file:
                return p
    return None


def resolve_parent(pname, site_id=None, pending=()):
    """``(parent_line, note)`` for a KO row's PARENT cell — one lookup, shared.

    ``pending`` is the wild types the *same paste* is about to create, and only
    ``plan`` passes it: by the time ``apply`` runs this, those rows are in the
    database and are found by the ordinary lookup below. It is what keeps the two
    answers the same — see ``_pending_parent``.

    **A parent may be named or numbered, because the lab does both.** This said
    "matched by name, not by C-number" and meant it: of the 169 parents in the
    live database 145 are C-numbers and 24 are names, so the rule the app stated
    was true of one row in seven. The sixth field test read the column as wanting
    a number, was corrected, and had been right. A C-number goes through
    ``services/cell_lines.py::by_c_number``, which looks in the vial table as
    well as the line table — 102 of those 145 are a vial's number, which is why
    only 2 of 564 rows had a parent linked at all.

    Two things this fixes beyond reading both forms:

    * **The preview said nothing.** `plan` reported a bare "new" whether the
      parent had resolved or not, so the one thing that makes a knockout mean
      anything was the one thing you could not check before saving. The write
      already knew — it appended a note to `apply`'s output, which no preview
      reads. A preview is only true if it asks the same question as the write,
      which is why both call this.
    * **It was not scoped to a site.** Three lines above it in `apply`, the row's
      own identity is matched with `find_cell_line(..., site_id=…)`, deliberately.
      The parent was matched across every site, so "HAP1" at Leicester could
      quietly parent onto McGill's HAP1 — the two really are both called HAP1,
      which is the whole reason the sessions board showed `HAP1 / HAP1`. Own site
      first, and when only another site's line matches, say whose it is.
    """
    refs = _parent_refs(pname)
    if not refs:
        return None, ""

    extra = (f" (also recorded: {', '.join(refs[1:])} — kept as written, "
             f"one parent can be linked)" if len(refs) > 1 else "")

    def _pending_note(p):
        # Nothing is returned as the parent — the line does not exist yet — so
        # the caller records the intent and the note says which row supplies it.
        return None, (f'parent "{p["name"]}" is the wild type on row {p["row"]} of '
                      f"this paste — it is saved first, so this knockout links to "
                      f"it{extra}")

    # Before the database, because the write reaches this row *after* creating
    # that one — but only where the answer is not in doubt (see `_pending_parent`).
    p = _pending_parent(refs, site_id, pending)
    if p:
        return _pending_note(p)

    for ref in refs:
        # A C-number first when the cell is written as one: `C-48` cannot be a
        # name, and reading it as one is what left 145 parents unlinked. A bare
        # `48` is tried as a name first and as a number after, because it could
        # honestly be either and a name is the more literal reading.
        by = "C-number"
        matches = cell_line_svc.by_c_number(ref) if _looks_like_c_number(ref) else []
        if not matches:
            matches = list(CellLine.objects.using(DB).filter(name__iexact=ref))
            by = "name"
        if not matches and ref.strip().isdigit():
            matches = cell_line_svc.by_c_number(ref)
            by = "C-number"
        if not matches:
            continue
        line = _pick_parent(matches, site_id)
        where = getattr(getattr(line, "site", None), "name", "")
        said = f'{ref} → "{line.name}"' if by == "C-number" else f'"{line.name}"'
        if line.genotype != "WT":
            return line, (f"parent {said} is a "
                          f"{line.get_genotype_display().lower()} line, not a wild "
                          f"type — check that is the one you mean{extra}")
        if site_id and line.site_id == site_id:
            return line, f"parent {said} is your site's line{extra}"
        return line, ((f"parent {said} is {where}'s line — check that is the one "
                       f"you mean{extra}") if where
                      else f"parent {said} matched, with no site on file{extra}")

    # Nothing on file answers to it — so a wild type this paste is creating is
    # the only candidate there is, whatever site it carries. Asked here as well
    # as above because "no cell line called 'U2OS'" would otherwise be printed
    # over a row that is about to create exactly that.
    p = _pending_parent(refs, site_id, pending, any_on_file=False)
    if p:
        return _pending_note(p)

    known = ", ".join(
        CellLine.objects.using(DB).filter(genotype="WT", site_id=site_id)
        .order_by("name").values_list("name", flat=True)[:12]) if site_id else ""
    typed = refs[0] if len(refs) == 1 else pname.strip()
    return None, (f"no cell line called '{typed}' — a parent is the wild type this "
                  f"knockout was made from, named ({_PARENT_NAME_EG}) or numbered "
                  f"(C-48). This knockout will be saved with no parent linked"
                  + (f". Parental lines on file at your site: {known}" if known else ""))


def _looks_like_c_number(ref: str) -> bool:
    """`C-48`, `c48`, `#48` — but not a bare `48`, which could be a pk or a name."""
    n, err = c_number_svc.parse(ref)
    return n is not None and not err and not ref.strip().isdigit()


def _pick_parent(matches, site_id):
    """Your own bench's wild type when the name is shared — the same preference
    ``services/cell_lines.py::resolve`` applies, and for the same reason: two
    labs really do both have a HAP1.

    **A wild type anywhere beats a knockout here**, including your own site's.
    A parent is by definition the wild type a knockout was made from, so
    returning a KO because it happens to be yours would be preferring the
    convenient answer to the correct one — and the caller only says "check that
    is the one you mean" about it, which is a weaker warning than it deserves.
    """
    wt = [m for m in matches if m.genotype == "WT"]
    if site_id:
        own_wt = [m for m in wt if m.site_id == site_id]
        if own_wt:
            return own_wt[0]
    if wt:
        return wt[0]
    if site_id:
        own = [m for m in matches if m.site_id == site_id]
        if own:
            return own[0]
    return matches[0]


def plan(rows, create_targets: bool = False, member=None):
    """Per-row preview. **Two passes**, because a row's parent may be a row.

    The first pass settles every row's own identity and status; only then is it
    known which wild types this paste will actually create, which is what the
    parent lookup in the second pass needs to match ``apply``'s ordering. See
    ``_pending_parent``.
    """
    items = []
    for r in rows:
        gene = (r.get("gene") or "").strip()
        # `NA` in the gene column means "there isn't one", which is the honest
        # answer for a wild type and what this lab has always written. It is read
        # as a blank gene here rather than matched against the placeholder target
        # it used to create — see services/targets.py::NOT_APPLICABLE.
        said_na = target_svc.is_not_applicable(gene)
        if said_na:
            gene = ""
        genotype = norm_genotype(r.get("genotype"), r.get("name"), bool(r.get("parent")),
                                 gene_not_applicable=said_na)
        target = resolve_target(gene) if gene else None
        name = _derived_name(r, genotype)
        site_id, site_name, site_err = _row_site(r, member)

        note = ""
        status = "create"
        # Set by the `else` branch below when the row matches a line already on
        # file. Named up here because the C-number clash check further down
        # needs it, and a row that matched is allowed to keep its own number.
        existing = None
        if said_na and genotype != "KO":
            note = "gene NA — read as a wild type, with no gene"
        if site_err:
            status, note = "blocked", site_err
        elif not name:
            status, note = "blocked", (
                "needs a name — the cell line's own name, as it is written on the "
                f"flask ({_PARENT_NAME_EG}, HeLa, U2OS). The gene goes in the gene "
                "column, not the name")
        elif genotype == "KO" and not gene:
            status, note = "blocked", (
                "gene NA on a KO row: a knockout is a knockout *of* something, so "
                "this needs the gene it knocks out — or set the genotype to WT"
                if said_na else
                "a KO needs a gene (KO of what?) — put the gene symbol in the gene "
                "column, or set the genotype to WT if this is a parental line")
        else:
            existing = find_cell_line(r, target, site_id=site_id)
            if existing:
                status, note = "update", match_note(r, existing)
            elif genotype == "WT" and gene:
                # Refused here for the same reason the identity dialog refuses it
                # — and in the same words, because a rule enforced in one place
                # and not the other is a rule nobody can rely on. Only on a
                # *create*: a wild type already on file with a gene is legacy data
                # and re-uploading its own sheet must not be blocked.
                status, note = "blocked", (
                    "a wild-type parental line is recorded once, with no gene: "
                    "one HAP1 WT serves every knockout made from it — leave the "
                    "gene blank, or set the genotype to KO")
            elif gene and target is None:
                status = "create-target" if create_targets else "blocked"
                if not create_targets:
                    # **Name the control that is on this page.** This said
                    # tick "create targets", and no control anywhere is called
                    # that: the checkbox reads "Add the gene as a new target if
                    # it isn't one yet". A refusal that names a control nobody
                    # can find is the same failure as one that names a retired
                    # page — the reader follows it, finds nothing, and concludes
                    # the feature is broken. `add_target_url` is the other half:
                    # a gene belongs on the target board, so the preview offers
                    # the way there rather than describing it.
                    note = (f"gene '{gene}' is not in the pipeline yet — tick "
                            f"“Add the gene as a new target if it isn't one yet” "
                            f"above, or add it on the target board first")

        # A C-number that cannot be read is refused *per cell*, and the row is
        # still written — the same bargain the concentration column strikes, for
        # the same reason: a batch label nobody can parse is no reason to throw
        # away a good cell line. What must not happen is the row going in with a
        # number this made up, which is what a bare `re.search(r"\d+")` did.
        cnum, cnum_err = _cnum(r.get("c_number"))
        pair_c, pair_err = _cnum(r.get("pair_c"), field="paired ko")
        # A number **another** line at this bench already carries is a different
        # matter from one that cannot be read, and it does not take the same
        # bargain. Unreadable means the cell says nothing usable and the line is
        # still worth keeping; a collision means the cell says something that
        # belongs to somebody else's tube, and writing the row anyway would
        # either put two lines under one number or — since a number is issued
        # when the column is blank — quietly file this one under a number
        # nobody typed. So it blocks, and names the line in the way.
        if cnum is not None and status != "blocked":
            clash = lab_numbers.clash(
                lab_numbers.CELL_LINE, site_id, cnum, site_name=site_name,
                exclude_pk=existing.pk if existing else None)
            if clash and not (existing and existing.c_number == cnum):
                status, note = "blocked", clash
        # Held back rather than joined here, so the second pass can put the
        # parent note where it has always read — after the row's own verdict and
        # before the per-cell refusals.
        cnum_notes = [err for err in (cnum_err, pair_err) if err]

        comp = (cdb.resolve_company(r.get("company", ""), r.get("catalogue", ""),
                                    create=False) if r.get("company") else None)
        items.append({
            "row": r, "name": name, "gene": gene, "genotype": genotype,
            "parent": (r.get("parent") or "").strip(),
            "c_number": cnum,
            "pair_c": pair_c,
            # See `bulk_antibodies.plan`. A row whose C-number cell could not be
            # read is *not* counted: that one is deliberately left blank
            # (`lab_numbers.withhold`), and `no_c_number` is what names it.
            "will_be_numbered": (status == "create" and bool(site_id)
                                 and cnum is None and not cnum_err),
            # Named separately from `note` so a summary can count them without
            # matching on the wording of a message.
            "c_number_dropped": bool(cnum_err),
            "pair_c_dropped": bool(pair_err),
            "target_id": target.id if target else None,
            "company_status": ("existing" if comp else ("new" if r.get("company") else "none")),
            # What the row will be saved under, not the display spelling — the
            # same rule the antibodies preview follows. See
            # `cropper/db.py::resolved_company_name`.
            "company_resolved": cdb.resolved_company_name(r.get("company", ""),
                                                          r.get("catalogue", ""),
                                                          existing=comp),
            "site": site_name, "site_id": site_id,
            # Where to go when the answer is "that gene isn't a target yet".
            # Rendered as a link by `OGABoard.previewRows`, so every surface that
            # previews a paste offers it — a preview rule fixed in one template
            # is a rule for one page.
            "add_target_url": (_add_target_url(gene)
                               if status == "blocked" and gene and target is None
                               else ""),
            "status": status, "note": note, "_cnum_notes": cnum_notes,
        })

    # ── second pass: the parent, now that it is known which rows get written ──
    #
    # A wild type this paste creates is a parent the database cannot answer for
    # yet. `apply` writes wild types first, so by the time it resolves a KO's
    # parent the row exists and wins on site preference — the preview has to
    # simulate that or it warns about a line the save will not use.
    pending = [{"name": it["name"], "site_id": it["site_id"], "row": n}
               for n, it in enumerate(items, start=1)
               if it["genotype"] == "WT" and it["name"]
               and it["status"] in ("create", "create-target")]

    for it in items:
        parts = [it["note"]] if it["note"] else []
        # Asked for a row that is going to be written, and with the same lookup
        # `apply` will do, so the answer is on screen before anything is saved
        # rather than in a note nobody reads.
        if it["status"] in ("create", "update", "create-target") and it["genotype"] == "KO":
            _parent, pnote = resolve_parent(it["parent"], site_id=it["site_id"],
                                            pending=pending)
            if pnote:
                parts.append(pnote)
        parts.extend(it.pop("_cnum_notes"))
        it["note"] = " · ".join(parts)

    return items


def _add_target_url(gene: str) -> str:
    """The target board, with this gene filled in and the Add panel open."""
    try:
        from django.urls import reverse
        from urllib.parse import urlencode
        return f"{reverse('pipeline:target_board')}?{urlencode({'gene': gene, 'add': '1'})}"
    except Exception:  # never break a preview over a URL
        return ""


def summarize(items) -> dict:
    return {
        "rows": len(items),
        "update": sum(1 for i in items if i["status"] == "update"),
        "create": sum(1 for i in items if i["status"] == "create"),
        "create_target": sum(1 for i in items if i["status"] == "create-target"),
        "blocked": sum(1 for i in items if i["status"] == "blocked"),
        # **A number the app gives a record is a thing the app did.** The first
        # field test on A-numbers left the number column blank, got A-1, and
        # said nothing in the preview or the save message mentioned that a lab
        # reference had been minted. The check says one *will* be given and does
        # not promise which — the value depends on what else lands in the same
        # batch — and `apply` reports what was actually issued.
        "will_be_numbered": sum(1 for i in items if i.get("will_be_numbered")),
        "new_companies": sorted({i["row"].get("company", "") for i in items
                                 if i["company_status"] == "new" and i["row"].get("company")}),
        # Say what is about to be dropped, and count it — at the save as well as
        # at the check. Per-row notes alone are how the eleventh field test read
        # a refusal, pressed the button anyway and got two lines saved with no
        # C-number where it had typed one.
        "no_c_number": sum(1 for i in items
                           if i.get("c_number_dropped") or i.get("pair_c_dropped")),
        # Which benches this paste would write to — see bulk_antibodies.summarize.
        "sites": sorted({i["site"] for i in items if i.get("site")}),
    }


# ── apply (writes) ───────────────────────────────────────────────────────────

def _apply_metadata(cl, row, *, creating, member, overwrite=False, site_id=None):
    """Fill only blank fields by default, so re-pasting never clobbers existing
    data; overwrite=True lets a non-empty value replace the existing one (the
    edited-export round-trip). A blank cell never wipes data either way."""
    if row.get("company") and (overwrite or not cl.company_id):
        company = cdb.resolve_company(row["company"], row.get("catalogue", ""), create=True)
        if company:
            cl.company = company
    for src, field in (("catalogue", "catalogue_number"), ("lot", "lot_number"),
                       ("cellosaurus_id", "cellosaurus_id"), ("medium", "medium"),
                       ("clone", "clone"), ("origin", "origin"),
                       ("origin_comments", "origin_comments"),
                       ("parent", "parental_line_name")):
        val = (row.get(src) or "").strip()
        if src == "cellosaurus_id":
            val = _norm_cvcl(val)
        if val and (overwrite or not getattr(cl, field)):
            setattr(cl, field, val)
    if row.get("species") and (overwrite or cl.species in ("", "Human")):
        cl.species = row["species"].strip()
    if site_id is None:
        site_id = getattr(member, "site_id", None)
    if creating and site_id:
        cl.site_id = site_id


def _ensure_vial(cl, c_number, member, db=DB, site_id=None):
    """A C-number is a freeze-down batch: ensure a CellLineVial carries it."""
    if c_number is None:
        return None
    vial = CellLineVial.objects.using(db).filter(cell_line=cl, c_number=c_number).first()
    if vial:
        return vial
    vial = CellLineVial(cell_line=cl, c_number=c_number, received=True,
                        received_date=date.today())
    if site_id is None:
        site_id = getattr(member, "site_id", None)
    if site_id:
        vial.site_id = site_id
    vial.save(using=db)
    # bridge: surface the batch on the identity's legacy c_number so the existing
    # "search by C number" finds it, without overwriting an earlier batch.
    if cl.c_number is None:
        cl.c_number = c_number
        cl.save(using=db, update_fields=["c_number"])
    return vial


def _storage_type(row) -> str:
    """Map free text to an InventoryLocation.StorageType code."""
    explicit = (row.get("storage_type") or "").strip().lower()
    src = explicit or " ".join([(row.get("location") or ""), (row.get("freezer") or "")]).lower()
    if "ln2" in src or "nitrogen" in src or "liquid n" in src:
        return "ln2"
    if "-80" in src or "−80" in src:
        return "-80"
    if "-20" in src or "−20" in src:
        return "-20"
    if "4" in src and ("c" in src or "fridge" in src):
        return "4c"
    if "rt" in src or "room" in src:
        return "rt"
    return "other"


def _ensure_location(vial, cl, row, member, db=DB, site_id=None):
    """If the row carries any storage info, record where the vial lives as an
    InventoryLocation. Skips silently if there's no site (the FK is required) and
    dedups so re-pasting the same location doesn't stack duplicates."""
    has = any((row.get(k) or "").strip() for k in _STORAGE_HIER) \
        or (row.get("location") or "").strip() or (row.get("storage_type") or "").strip()
    if not (has and vial):
        return None
    site_id = (site_id or getattr(member, "site_id", None)
               or getattr(cl, "site_id", None))
    if not site_id:
        return None
    st = _storage_type(row)
    fields = {k: (row.get(k) or "").strip() for k in _STORAGE_HIER}
    notes = (row.get("location") or "").strip()
    dup = InventoryLocation.objects.using(db).filter(vial=vial, storage_type=st, notes=notes, **fields)
    if dup.exists():
        return dup.first()
    return InventoryLocation.objects.using(db).create(
        vial=vial, cell_line=cl, site_id=site_id, storage_type=st, notes=notes, **fields)


def apply(rows, create_targets: bool, member=None, overwrite=False):
    """Create/update cell lines (and, if asked, missing targets) in one
    transaction. Fills only blank fields by default; overwrite=True lets non-empty
    values replace existing ones (the edited-export round-trip). Processes WT/other
    rows before KO rows so a KO's parent — if it is in the same paste — exists in
    time to be linked."""
    items = plan(rows, create_targets, member=member)
    out = {"created": [], "updated": [], "created_targets": [],
           "skipped": [], "notes": [],
           # Rows written *without* the C-number they named, so the save box can
           # say so rather than leaving it to a per-row note on the check.
           "no_c_number": [],
           # The C-numbers this press minted. Same key as `bulk_antibodies`,
           # because one writer in `board.js` renders both.
           "numbers_issued": []}

    order = sorted(range(len(items)), key=lambda i: items[i]["genotype"] == "KO")
    processed = []          # [(item, cell_line)] for the pairing pass
    cnum_map = {}           # this paste's C-number → cell line
    with transaction.atomic(using=DB):
        for idx in order:
            it = items[idx]
            r, name, gene, genotype = it["row"], it["name"], it["gene"], it["genotype"]
            if it["status"] == "blocked":
                out["skipped"].append(name or gene or "(row)")
                if it["note"]:
                    out["notes"].append(f"{name or gene or 'row'}: {it['note']}")
                continue

            target = resolve_target(gene) if gene else None
            if gene and target is None and create_targets:
                target, made = resolve_or_create_target(gene, member=member)
                if made and target:
                    out["created_targets"].append(target.gene_name)

            cl = find_cell_line(r, target, site_id=it["site_id"])
            creating = cl is None
            if creating:
                cl = CellLine(name=name, target=target, genotype=genotype)
                # The typed C-number goes on the line **before** it is saved.
                # `_ensure_vial` below has always bridged the batch number up to
                # the line afterwards, and that was enough while the column was
                # only ever filled by hand — but a number is now issued on save
                # when the column is blank (`pipeline/signals.py`), so a line
                # saved without it would be given one and the bridge, which only
                # fills a blank, would never run. The tube would say C-42 and the
                # record C-745.
                cl.c_number = it["c_number"]
                if it.get("c_number_dropped"):
                    # The cell said something this could not read. The row is
                    # still written — an odd batch label is no reason to discard
                    # a good cell line — and the number stays **blank** rather
                    # than becoming the next free one, because the person was
                    # telling us what the tube says and filing it under a
                    # different number papers over exactly that. The save's own
                    # `no_c_number` list is what says so on screen.
                    lab_numbers.withhold(cl)
            elif overwrite and genotype and cl.genotype != genotype:
                cl.genotype = genotype
            _apply_metadata(cl, r, creating=creating, member=member,
                            overwrite=overwrite, site_id=it["site_id"])

            # KO → WT parent link (only fill if not already linked). Same lookup
            # the preview ran, so what was previewed is what is written.
            if genotype == "KO" and not cl.parent_line_id:
                pname = (r.get("parent") or "").strip()
                if pname:
                    parent, pnote = resolve_parent(pname, site_id=it["site_id"])
                    if parent:
                        cl.parent_line = parent
                    if pnote:
                        out["notes"].append(f"{name}: {pnote}")
                else:
                    out["notes"].append(f"{name}: knockout with no WT parent named")

            cl.save(using=DB)
            vial = _ensure_vial(cl, it["c_number"], member, site_id=it["site_id"])
            _ensure_location(vial, cl, r, member, site_id=it["site_id"])
            if it["c_number"] is not None:
                cnum_map[it["c_number"]] = cl
            processed.append((it, cl))

            if creating and it["c_number"] is None and cl.c_number is not None:
                out["numbers_issued"].append(
                    {"number": c_number_svc.label(cl.c_number), "name": cl.name})
            rec = {"name": cl.name, "id": cl.pk, "genotype": cl.genotype,
                   "c_number": cl.c_number}
            (out["created"] if creating else out["updated"]).append(rec)
            if it.get("c_number_dropped"):
                out["no_c_number"].append(
                    {"name": cl.name, "typed": str(r.get("c_number") or "").strip()})
            if it.get("pair_c_dropped"):
                out["no_c_number"].append(
                    {"name": cl.name, "typed": str(r.get("pair_c") or "").strip()})

        # ── pairing pass: a paired-KO C-number links the WT↔KO shipped together ──
        for it, cl in processed:
            pc = it.get("pair_c")
            if pc is None:
                continue
            partner = cnum_map.get(pc)
            if partner is None:
                v = CellLineVial.objects.using(DB).filter(c_number=pc).first()
                partner = v.cell_line if v else None
            if partner is None or partner.pk == cl.pk:
                out["notes"].append(f"{cl.name}: paired C-{pc} not found")
                continue
            wt = cl if cl.genotype == "WT" else (partner if partner.genotype == "WT" else None)
            ko = cl if cl.genotype == "KO" else (partner if partner.genotype == "KO" else None)
            if wt and ko:
                if wt.arrived_with_ko_id != ko.pk:
                    wt.arrived_with_ko = ko
                    wt.save(using=DB, update_fields=["arrived_with_ko"])
            else:
                out["notes"].append(f"{cl.name}: pairing needs one WT and one KO")
    return out
