"""One place that turns a typed cell-line name into a `CellLine`.

There were five of these and they disagreed, which is how the seventh field test
recorded **another institution's knockout as its wild-type control** and never
saw a word about it:

* `bulk_sessions.resolve_or_create_cell_line` matched on name alone. It *takes*
  a `genotype` argument and used it only when **creating**, never when matching,
  and it ignored the member's site — so `SH-SY5Y`, a name five rows share, was
  resolved by `.first()` under `Meta.ordering = ['name']`, i.e. arbitrarily. It
  picked McGill's `SH-SY5Y PRKN KO` as a Leicester STMN2 session's wild type.
  With `HAP1` the odds are far worse: hundreds of rows carry that name, because
  the convention puts the gene in its own column.
* `sessions._resolve_cell_line` matched the bare name or a pk, so the app's own
  rendering — `SH-SY5Y — Leicester`, which is what the board prints and
  therefore what a person retypes — matched nothing, and the mis-resolved value
  above could not be corrected from the surface that displayed it.
* `session_import._resolve_cell_line` looked in `target.cell_lines`. **A wild
  type has no gene**, so that query cannot return one: the per-gene workbook
  ships `cell_line_wt` pre-filled, and the importer dropped it on every row of
  every tab. The visible consequence was three `[WT cell line]` placeholders in
  a generated Data Note.

So this module is `services/sites.py` for cell lines, and it holds the same
line: **resolve, never create; refuse by naming the alternatives; and never pick
one of several silently.** Creating is still `bulk_cell_lines`' job, on a
surface that previews first.

Two rules the callers keep getting wrong are enforced here instead:

* a wild type is recorded once with no gene, so a WT is **never** looked for
  under a target;
* a name that matches more than one row is a **question**, not a coin toss.
"""
from __future__ import annotations

import re

from django.db.models import Q

from pipeline.models import CellLine, CellLineVial
from pipeline.services import c_number as c_number_svc
from pipeline.services import targets as target_svc

DB = "pipeline_db"

# How the sessions board renders a line — `services/session_board.py::_line_label`
# builds it and this reads it back, so the string a person copies off the screen
# is a string they can type in. Kept as one constant because the split is the
# exact inverse of the join.
SITE_SEP = " — "

# A knockout whose stored name doesn't mention its gene is decorated with it:
# `HAP1` displays as `HAP1 ELP3 KO`. Recognised on the way back so the rendered
# label round-trips to the row it was rendered from.
_KO_SUFFIX = re.compile(r"^(?P<name>.+?)\s+(?P<gene>[A-Za-z0-9\-]+)\s+KO$", re.I)

# A trailing bare `KO` on the stored name. Stripped before the gene is appended,
# so `SW620 KO` reads `SW620 SOD1 KO` rather than `SW620 KO SOD1 KO`.
_BARE_KO = re.compile(r"\s+KO\s*$", re.I)

# How many alternatives a refusal lists before it stops. Long enough to be a
# real answer, short enough to read: `HAP1` alone would otherwise print
# hundreds.
_MAX_LISTED = 12

# A trailing clone, appended by `label()`. Two shapes, because the lab writes the
# word both ways: `clone` as a separator before the value (`… KO clone 2.3`,
# split on in `_clone_readings`), and `clone` as the first word *of* the value
# (`… KO Clone2 (C2)`, below). Undone as separate variants rather than one clever
# pattern — `_name_variants` tries them in order and the first that matches a
# real row wins, so a wrong split simply finds nothing instead of the wrong line.
_CLONE_WORD = re.compile(r"^(?P<stem>.+?)\s+(?P<clone>clone\S.*)$", re.I)


def clone_suffix(line) -> str:
    """How a clone is printed after the line's name, or `""`.

    **Printed verbatim.** `clone` is written eleven different ways on live —
    `2.3`, `C11`, `Clone#8`, `Clone 2 (C2)`, `no22`, `Guide B9, 1X, Clone D` —
    and normalising them into one house style is the presentation-rewrites-a-value
    mistake this file records twice already (the µ that CSS upper-cased, the
    received date a refusal put back in the wrong spelling). The only thing added
    is the word `clone`, and only when the value does not already open with it,
    so `Clone2 (C2)` is not drawn as `clone Clone2 (C2)`.
    """
    clone = (getattr(line, "clone", "") or "").strip()
    # `NA` is the Access placeholder for "not recorded", the same shape as `NA`
    # in a gene column — 15 KO rows carry it. It is not a clone, so it is not a
    # name, and a label reading `… KO clone NA` would make an absence look like
    # an identity.
    if not clone or clone.upper() == "NA":
        return ""
    return clone if clone[:5].lower() == "clone" else f"clone {clone}"


def label(line) -> str:
    """What this line is, then whose — the sessions board's own rendering.

    Duplicated from `session_board._line_label` deliberately *not*: that one
    imports from here now, so a change to how a line is named changes it
    everywhere at once, including the refusals below.

    **The clone is part of what the line is**, so it is part of the name. A gene
    and a background define the knockout; the clone says *which* knockout — a
    different single-cell origin, often a different guide. Two clones of one KO
    at one bench rendered the same string, which is `_told_apart`'s problem
    arriving on every screen at once rather than only in a refusal.

    A line with no clone recorded renders exactly as it did before, which is what
    keeps every stored label, bookmark and pasted sheet on the 300-odd clone-less
    knockouts working unchanged.
    """
    if line is None:
        return ""
    text = line.name or ""
    # `NA` is not a gene, so a knockout attached to the placeholder target must
    # not be decorated `HAP1 NA KO` — see services/targets.py::gene_of.
    gene = target_svc.gene_of(getattr(line, "target", None))
    if line.genotype == "KO" and gene and gene.lower() not in text.lower():
        # `SW620 KO` already says it is a knockout but not of what, and two genes'
        # knockouts of one parental are then the same string. Naming the gene is
        # the point; saying KO twice is not.
        text = f"{_BARE_KO.sub('', text)} {gene} KO"
    suffix = clone_suffix(line)
    if suffix:
        text = f"{text} {suffix}"
    where = getattr(getattr(line, "site", None), "name", "")
    return f"{text}{SITE_SEP}{where}" if where else text


def split_label(value):
    """`'HAP1 ELP3 KO — Leicester'` → `('HAP1 ELP3 KO', 'Leicester')`.

    The site half is what the board adds to tell two labs' HAP1 apart, and it is
    the half that made a retyped value fail: `strict_id`-style exactness against
    a string the app itself composed. Blank site when there is no separator.
    """
    raw = (str(value) if value is not None else "").strip()
    if not raw:
        return "", ""
    # Tolerate the plain hyphen a keyboard produces as well as the em dash the
    # page renders — a person retyping the label will not reach for U+2014.
    for sep in (SITE_SEP, " - ", " -- "):
        if sep in raw:
            head, _, tail = raw.rpartition(sep)
            return head.strip(), tail.strip()
    return raw, ""


def _clone_readings(name: str):
    """`[(stem, implied_clone)]` — the ways a trailing clone could have been added.

    `(name, None)` first and always: **the most literal reading wins**, which is
    what keeps a stored name that contains the word from being taken apart.
    `Jurkat, clone E6-1` is a real parental on file — ATCC's own clone
    designation, part of the name — and splitting it would look for a line called
    `Jurkat,`. It never gets that far while the line carries no clone of its own,
    because the literal reading matches a real row and `candidates` stops at the
    first variant that finds anything.

    **But that row can carry a clone, and then both are true at once.** McGill
    holds `Jurkat, clone E6-1` as a SLIT1 knockout of clone D16, so `label()`
    renders `Jurkat, clone E6-1 SLIT1 KO clone D16` — two `clone`s in one string,
    and a single split takes the *first*, which asks for a line called `Jurkat,`
    and finds nothing. So every separator is tried, **rightmost first**, because
    the rightmost is the one `label()` appended. The leftward readings stay in
    the list underneath: a clone value that itself contains the word (live holds
    `Guide B9, 1X, Clone D`) is only found by splitting further left.
    """
    out = [(name, None)]
    # Rightmost first — `label()` appends the clone at the end, so that reading
    # is the one that is nearly always right, and the rest are fallbacks that
    # cost nothing because a wrong split simply matches no row.
    for m in reversed(list(re.finditer(r"\s+clone\s+", name, re.I))):
        stem, clone = name[:m.start()].strip(), name[m.end():].strip()
        if stem and clone:
            out.append((stem, clone))
    m = _CLONE_WORD.match(name)
    if m:
        out.append((m.group("stem").strip(), m.group("clone").strip()))
    seen, ordered = set(), []
    for stem, clone in out:
        key = (stem.lower(), (clone or "").lower())
        if key not in seen:
            seen.add(key)
            ordered.append((stem, clone))
    return ordered


def _name_variants(name: str):
    """`[(stored_name, implied_genotype, implied_gene, implied_clone)]`, most
    literal first.

    A knockout is very often stored under its parental's bare name with the gene
    in `target`, so the label `HAP1 ELP3 KO` has to find the row called `HAP1`;
    and a name that already ended in `KO` had the gene spliced in, so
    `SW620 SOD1 KO` has to find the row called `SW620 KO`. Both are undone here
    rather than in the caller, because the caller is whoever pasted the label and
    should not have to know it was decorated.

    The implied parts matter as much as the name. Undecorating `HAP1 ELP3 KO`
    to `HAP1` and stopping there hands back the *parental* — and every wild type
    and every other gene's knockout that shares the name — which is the exact
    confusion the decoration exists to remove. So a variant that came from
    stripping ` <GENE> KO` only ever matches knockouts of that gene.

    The clone is stripped **outside** that, because it is appended outside it:
    `label()` builds `<name> <GENE> KO clone <clone>`, so the clone comes off
    first and each reading is then undecorated on its own. A label carrying no
    clone produces exactly the variants it always did.
    """
    out = []
    for stem, clone in _clone_readings(name):
        out.append((stem, None, None, clone))
        m = _KO_SUFFIX.match(stem)
        if m:
            base, gene = m.group("name").strip(), m.group("gene").strip()
            out.append((base, "KO", gene, clone))
            out.append((f"{base} KO", "KO", gene, clone))
    seen, ordered = set(), []
    for v in out:
        key = (v[0].lower(), v[1], (v[2] or "").lower(), (v[3] or "").lower())
        if v[0] and key not in seen:
            seen.add(key)
            ordered.append(v)
    return ordered


def _base_queryset(db: str = DB):
    return CellLine.objects.using(db).select_related("site", "target")


def by_c_number(value, db: str = DB) -> list:
    """The line a C-number names — the lab's own way of pointing at one.

    **A C-number is how this lab identifies a line, and the app did not read
    one.** Of the 169 parents recorded in the live database, 145 are written as
    C-numbers (`C-124`, `C-48`) and only 24 as names, while the entry surfaces
    said in as many words that the parent is "a name, not a C-number". The sixth
    field test read the column as wanting a number, was told it was wrong, and
    was following the convention of every row already on file.

    Both tables answer, and **the vial table answers more often**: 43 of those
    145 are a `CellLine.c_number`, 102 are a `CellLineVial.c_number` — the
    freeze-down batch, one level down — and nothing joined the two, which is why
    only 2 of 564 rows have a parent actually linked. Looking in one place finds
    a third of them.

    Returns a list for the same reason `candidates` does: a number that names
    more than one row is a question, not a coin toss.
    """
    n, err = c_number_svc.parse(value)
    if err or n is None:
        return []
    hits = list(_base_queryset(db).filter(c_number=n))
    if hits:
        return hits
    line_ids = list(CellLineVial.objects.using(db).filter(c_number=n)
                    .values_list("cell_line_id", flat=True))
    return list(_base_queryset(db).filter(pk__in=line_ids)) if line_ids else []


def batch_numbers(line, db: str = DB) -> list:
    """Every freeze-down batch C-number on a line, ascending.

    The line's own `c_number` is included when it is not already a batch's: it
    is the same number space, and on the Access-era rows the line carries the
    first batch's number because `bulk_cell_lines._ensure_vial` bridged it up
    there while search read only one table.
    """
    # `line.vials.all()` and **not** a fresh queryset: the board prefetches this
    # relation, and a `.filter()` here would ignore the cache and issue a query
    # per drawn row. `tests_cell_line_board.py` pins that as an N+1 — it caught
    # exactly this, at 37 queries for 32 lines against 7 for 2.
    numbers = {v.c_number for v in line.vials.all() if v.c_number is not None}
    if line.c_number is not None:
        numbers.add(line.c_number)
    return sorted(numbers)


def batch_label(line, db: str = DB) -> str:
    """The batches on a line, consecutive runs collapsed and the rest listed.

    **A range over a line's batches is a lie on half the data, and the worst on
    the lines that have the most.** C-numbers are issued per *site* in the order
    things were frozen, not per line, so between two HeLa freeze-downs other
    lines took numbers: HeLa's 17 batches are 15, 16, 79, 113, 422 … 742, and
    printing `C-15–C-742` claims 728 numbers for a line that owns 17. On live,
    60 of the 121 multi-batch lines have gaps like that.

    So a run is collapsed only where it is genuinely consecutive — which is what
    a freshly added batch looks like — and everything else is listed:

        C-23                      one batch
        C-23–C-25                 three consecutive
        C-15–C-16, C-79, C-113    a real line's history
    """
    return format_batches(batch_numbers(line, db=db))


def format_batches(numbers) -> str:
    """`[15, 16, 79]` → ``C-15–C-16, C-79``. Pure, so it is cheap to test."""
    runs, out = [], []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    for run in runs:
        first, last = c_number_svc.label(run[0]), c_number_svc.label(run[-1])
        # Two consecutive numbers are not a range worth collapsing — `C-15–C-16`
        # is longer than `C-15, C-16` says nothing extra. Three or more is.
        out.append(f"{first}–{last}" if len(run) > 2 else
                   ", ".join(c_number_svc.label(n) for n in run))
    return ", ".join(out)


def _clone_group_key(line):
    """What makes two rows clones of the same knockout: the gene, the background
    and the bench. Not the clone itself, which is the thing that tells them
    apart, and not the parent line, which several rows legitimately leave blank.
    """
    return ((line.name or "").strip().lower(), line.target_id, line.site_id)


def clones_of(line, db: str = DB) -> list:
    """Every clone on file of the same knockout, this one included.

    **A gene and a background define the knockout; a clone is one instance of
    it** — a separate single-cell origin, often a separate guide. That is the
    level the database had no room for: `clone` was one text field on one row,
    so a bench with seven clones of `HCT116 ACSL5 KO` could record one of them
    and nothing said the other six existed. Asked for by name (Sara, 3 Sep 2026:
    *"I couldn't see all the clones associated with a KO cell line"*).

    Wild types are excluded on purpose and it is the same rule as everywhere else
    in this file: a WT has no gene, one parental serves every knockout made from
    it, and grouping the 140 of them by name would answer a different question.
    """
    if line is None or (getattr(line, "genotype", "") or "").upper() != "KO":
        return []
    name, target_id, site_id = _clone_group_key(line)
    qs = _base_queryset(db).filter(name__iexact=name, genotype="KO")
    qs = (qs.filter(target_id=target_id) if target_id
          else qs.filter(target__isnull=True))
    qs = qs.filter(site_id=site_id) if site_id else qs.filter(site__isnull=True)
    # By clone, so a picker and a page list them in the same order, and by pk
    # last so the order is total — two rows can share a blank clone while the
    # split that gave them one is still being worked through.
    return list(qs.order_by("clone", "c_number", "pk"))


def clone_counts(lines, db: str = DB) -> dict:
    """`{cell_line_id: clones of that knockout on file}` — one query, not one
    per row.

    The board draws this on every knockout it lists, and `clones_of` per row is
    the N+1 `tests_cell_line_board.py` already caught once on `batch_numbers`
    (37 queries for 32 lines). Only the names actually drawn are asked about, so
    the query is bounded by the page rather than by the table.
    """
    ko = [cl for cl in lines if (getattr(cl, "genotype", "") or "").upper() == "KO"]
    if not ko:
        return {}
    names = {(cl.name or "").strip().lower() for cl in ko}
    tally = {}
    for name, target_id, site_id in (
            CellLine.objects.using(db)
            .filter(genotype="KO", name__in=[cl.name for cl in ko])
            .values_list("name", "target_id", "site_id")):
        key = ((name or "").strip().lower(), target_id, site_id)
        if key[0] in names:
            tally[key] = tally.get(key, 0) + 1
    return {cl.pk: tally.get(_clone_group_key(cl), 1) for cl in ko}


def clone_note(line, total=None) -> str:
    """What a row says about its siblings: `clone 2.3 · 1 of 7 clones`.

    Two facts that are easy to run together and are not the same. A row with a
    clone and no siblings says only what it is; a row with siblings says how many
    there are, because **a cell showing one of several looks like an answer** —
    the rule `services/storage.py` holds for an antibody kept in two boxes. A
    knockout with siblings and *no* clone recorded is the state the Access merge
    left behind, and it says so rather than drawing an empty cell.
    """
    if line is None:
        return ""
    suffix = clone_suffix(line)
    if total is None:
        total = len(clones_of(line)) or 1
    if total <= 1:
        return suffix
    if not suffix:
        return f"not recorded · one of {total} clones"
    return f"{suffix} · 1 of {total} clones"


def parent_label(line) -> str:
    """What the PARENT cell says: the linked line, and the batch it was recorded as.

    **The link and the reference are two facts and the cell was drawing one.**
    `parent_line.name if parent_line_id else parental_line_name` showed the
    C-number until a row was linked and the bare name afterwards — so
    `backfill_cell_line_parents` linking 156 rows replaced `C-16` with `HeLa` on
    every one of them, losing the freeze-down batch off the screen at the exact
    moment the row got better. Which HeLa is the whole question: the name covers
    17 parental stocks from five sources, and the C-number is the only thing that
    says which.

    So both, and neither twice — a row recorded as `HAP1` and linked to HAP1 says
    it once. A row carrying text that resolved to nothing says so rather than
    reading like a link, because those 17 are a worklist and an unlinked
    reference drawn plainly is indistinguishable from a working one.
    """
    if line is None:
        return ""
    recorded = (getattr(line, "parental_line_name", "") or "").strip()
    parent = getattr(line, "parent_line", None) if line.parent_line_id else None
    if parent is None:
        return f"{recorded} — not linked" if recorded else ""
    name = (parent.name or "").strip()
    if not recorded or recorded.lower() == name.lower():
        return name
    return f"{name} · {recorded}"


# ── A parent has to be a wild type *of that background* ─────────────────────
#
# `HeLa FUS KO → HAP1`, `U2OSn UBQLN2 KO → HCT116`, `HCT116 CSNK2A1 KO →
# HEK293T`: fourteen links the first dry run of `backfill_cell_line_parents`
# would have written, every one resolving to *a* wild type and none to a wild
# type of the knockout's own cell line. That check lived in the command, so the
# nineteenth field test pasted `HAP1 | STMN2 | KO | parent HeLa` on the cell
# lines board and read *parent "HeLa" is your site's line — new*: the paste
# door asked whether the parent was a wild type and never whether it was the
# right one, and a mismatched control is read as the matched one by every
# session planned afterwards. One reader now, asked by the command, the paste
# preview and the identity dialog.

def background(name: str, gene: str = "") -> str:
    """The cell line a name is a knockout *of*: `HAP1 ELP3 KO` → `HAP1`,
    `SW620 KO` → `SW620`, `HAP1` → `HAP1`.

    A knockout is usually stored under its parental's bare name with the gene in
    `target`, but the Access-era rows and a pasted label both carry the
    decoration `label()` adds, so it is undone here — trailing `KO` first, then
    the trailing gene symbol when the caller knows it.
    """
    # `label()` appends ` <GENE> KO` and then ` clone <x>`, so the clone comes
    # off only where it follows the KO — a wild type genuinely named
    # `U2OSn clone FM109` keeps its name, and stays distinct from `U2OSn`.
    text = re.sub(r"\s+KO(\s+clone\s+.*)?$", "", (name or "").strip(), flags=re.I)
    g = (gene or "").strip()
    if g:
        text = re.sub(rf"\s+{re.escape(g)}$", "", text, flags=re.I)
    return text.strip()


def parental_hint(name: str, site_id=None, db: str = DB) -> str:
    """`The HeLa wild type on file is C-15 — may be that.` — what to name instead.

    "Not a HeLa" describes the check; it does not tell a reader what to do, and
    the thing they need next is almost always on file already: the wild type of
    that background, at that bench. Its numbers come from `batch_numbers`, which
    folds a line's own C-number together with its freeze-down batches; two or
    three are listed and the rest counted, because this is a hint and not an
    inventory. A parental that carries no C-number at all — every one of
    Leicester's eight, on 5 Sep 2026 — is named by name, since "no wild type on
    file" would be false and would send somebody to add a second one. Where the
    background really has no wild type, it says so, so a suggestion never names
    a row that is not there.
    """
    bg = (name or "").strip()
    if not bg:
        return ""
    wts = list(CellLine.objects.using(db)
               .prefetch_related("vials")
               .filter(genotype=CellLine.Genotype.WILD_TYPE, name__iexact=bg,
                       site_id=site_id))
    if not wts:
        return (f"No {bg} wild type is on file at this bench, so there is "
                f"nothing to point it at.")
    numbers = sorted({n for wt in wts for n in batch_numbers(wt, db=db)})
    if not numbers:
        return (f"The {bg} wild type on file at this bench carries no C-number "
                f"— name it as {wts[0].name}.")
    shown = ", ".join(f"C-{n}" for n in numbers[:3])
    rest = len(numbers) - 3
    more = f" (and {rest} more of its batches)" if rest > 0 else ""
    one = len(numbers) == 1
    return (f"The {bg} wild type on file is {shown}{more} — "
            f"may be {'that' if one else 'one of those'}.")


def wrong_background(name: str, parent, *, gene: str = "", site_id=None,
                     db: str = DB) -> str:
    """`""` when `parent` is a wild type of the same cell line as a knockout
    called `name`; otherwise the sentence to refuse the link with.

    **The name match is exact.** `HEK293` and `HEK293T` are different cell
    lines and no prefix rule separates that pair from `U2OSn` and `U2OSn clone
    FM109`, which are one — so both go to a person. Refusing a link that is
    right costs one edit in a dialog; writing one that is wrong costs an
    experiment. Only a wild-type parent is judged here: a parent that is not a
    wild type at all is a different refusal, and its caller already has it.
    """
    if parent is None or parent.genotype != CellLine.Genotype.WILD_TYPE:
        return ""
    mine = background(name, gene)
    theirs = background(parent.name or "")
    if not mine or mine.casefold() == theirs.casefold():
        return ""
    where = site_id if site_id is not None else getattr(parent, "site_id", None)
    return (f"Looks wrong: {parent.name} is a wild type, but a {mine} knockout "
            f"does not come from a {parent.name}. "
            f"{parental_hint(mine, site_id=where, db=db)}")


def candidates(value, *, genotype=None, db: str = DB) -> list:
    """Every line a typed value could mean, before site preference is applied.

    Matches the label as rendered, then the bare stored name, then a pk. Never
    filters on `target`: a wild type has no gene, and the one caller that did
    that could not find a single one.
    """
    name, site_name = split_label(value)
    if not name:
        return []

    # **The first variant that matches anything wins**, and the search stops
    # there. Pooling every variant's hits instead meant a label naming one exact
    # row — `SH-SY5Y PRKN KO — McGill` — also dragged in the parental it was
    # undecorated from and every wild type sharing that name, and came back
    # "matches 2 cell lines" about a string that names exactly one. The most
    # literal reading of what somebody typed is the reading they meant.
    found = []
    for stored, want_genotype, want_gene, want_clone in _name_variants(name):
        qs = _base_queryset(db).filter(name__iexact=stored)
        if want_genotype:
            qs = qs.filter(genotype=want_genotype)
        if want_gene:
            qs = qs.filter(target__gene_name__iexact=want_gene)
        if want_clone:
            # Named the clone, so it is asking for that clone and not for its
            # siblings. A label with **no** clone deliberately does not filter:
            # `HCT116 ACSL5 KO` now answers to seven rows, and seven is the
            # honest answer — `ambiguous_message` lists them and asks, which is
            # this module's whole rule about a name that matches more than one.
            qs = qs.filter(clone__iexact=want_clone)
        hits = list(qs)
        if hits:
            found = hits
            break
    if not found and name.isdigit():
        found = list(_base_queryset(db).filter(pk=int(name)))
    if not found:
        # A C-number, last — so a bare number is still a pk (which is what the
        # sessions board's own labels use) and `C-48` is unambiguous either way.
        found = by_c_number(name, db=db)

    if genotype:
        want = str(genotype).strip().upper()
        found = [cl for cl in found if (cl.genotype or "").strip().upper() == want]

    if site_name:
        # A label that names a site means *that* site's line. Honoured strictly:
        # falling back to another site's row is the re-homing this module exists
        # to stop.
        narrowed = [cl for cl in found
                    if (getattr(getattr(cl, "site", None), "name", "") or "").lower()
                    == site_name.lower()]
        if narrowed:
            found = narrowed
    return found


def known_labels(*, genotype=None, site_id=None, db: str = DB) -> list:
    """The lines a person could have meant, for a refusal to name.

    Narrowed to the asker's own site when there is one, because "the parentals
    on file at your site" is a list somebody can act on and the consortium-wide
    list is not.
    """
    qs = _base_queryset(db)
    if genotype:
        qs = qs.filter(genotype=str(genotype).strip().upper())
    if site_id:
        own = list(qs.filter(site_id=site_id).order_by("name")[:_MAX_LISTED])
        if own:
            return [label(cl) for cl in own]
    return [label(cl) for cl in qs.order_by("name")[:_MAX_LISTED]]


def wild_type_options(*, site_id=None, limit=_MAX_LISTED * 2, db: str = DB) -> list:
    """`[{"name", "c_number", "value", "hint"}]` — a site's wild types, pickable.

    The gene page printed these **in prose** — *"Wild-type parentals already on
    file at your site: A549, HAP1, HCT116, …"* — under a grid where you then
    retyped one into a knockout's `parent` cell. A list you read and a box you
    type into are two halves of a job the page can do, on the one column where
    getting it wrong parents a knockout onto another institution's line.

    ``value`` is what goes in the cell, so it is **what the parser accepts** and
    nothing else: `bulk_cell_lines.resolve_parent` takes a bare name or a
    C-number, and it does not take `label()`'s `HAP1 — Leicester`. Offering the
    app's own display string in a picker is the mistake this file already
    records the sessions board making — the resolver refused output the app had
    composed itself.

    A **name shared by two of the site's own wild types is a question, not a
    coin toss**, so those entries fall back to their C-number, which is exact.
    The name stays in the hint, because that is what a person recognises.
    """
    if not site_id:
        return []
    lines = list(_base_queryset(db).filter(genotype="WT", site_id=site_id)
                 .order_by("name")[:limit])
    if not lines:
        return []

    # The freeze-down batch answers more often than the line does — 102 of the
    # 145 C-numbered parents on file are a vial's number. One query, not one per
    # line: this is drawn on every gene page.
    vial_numbers = _vial_numbers(lines, db=db)

    shared = {}
    for cl in lines:
        shared[(cl.name or "").lower()] = shared.get((cl.name or "").lower(), 0) + 1

    out = []
    for cl in lines:
        name = cl.name or ""
        number = cl.c_number or vial_numbers.get(cl.pk)
        c_text = f"C-{number}" if number else ""
        ambiguous = shared.get(name.lower(), 0) > 1 and c_text
        out.append({
            "name": name,
            "c_number": number,
            "value": c_text if ambiguous else name,
            "hint": (f"{name} — one of {shared[name.lower()]} called {name}"
                     if ambiguous else c_text),
        })
    return out


def _vial_numbers_all(lines, db: str = DB) -> dict:
    """`{cell_line_id: [c_number, …]}` — every freeze-down batch on file.

    One query for the whole list, not one per line: every caller draws this on a
    page load. 102 of the 145 C-numbered parents on file are a vial's number
    rather than the line's, which is why this is asked at all.
    """
    out = {}
    for line_id, c in (CellLineVial.objects.using(db)
                       .filter(cell_line_id__in=[cl.pk for cl in lines],
                               c_number__isnull=False)
                       .order_by("cell_line_id", "c_number")
                       .values_list("cell_line_id", "c_number")):
        out.setdefault(line_id, []).append(c)
    return out


def _vial_numbers(lines, db: str = DB) -> dict:
    """`{cell_line_id: c_number}` — the first batch, for callers naming one.

    A picker offers one number because it needs a value that names exactly one
    row, not an inventory. `session_options` wants them all, so the query lives
    one function up and this narrows it — rather than the two asking the vial
    table the same question in two shapes.
    """
    return {k: v[0] for k, v in _vial_numbers_all(lines, db=db).items()}


def picker_options(*, genotype, site_id, limit=_MAX_LISTED * 5, db: str = DB) -> list:
    """`[{"value", "hint"}]` — a site's lines of one kind, offered as a list.

    The quick **Plan a session** panel's WT and KO boxes are free text, on a
    surface whose stated rule is that anything new is created along with the
    session — so a typo does not fail, it quietly mints a near-duplicate cell
    line and controls the session against it. The step-by-step wizard has had
    dropdowns naming each line's site since it was written; this is that list,
    where the fast path can use it.

    A `<datalist>` and not a `<select>`, deliberately: creating a line with the
    session is a real feature (a bench that has just made a knockout should not
    have to leave and come back), and the panel's own check already reports a
    name nothing answers to as *will be created*. The list removes the typo, not
    the door.

    ``value`` is **what the parser accepts**: `resolve` reads `label()`'s
    `HAP1 — Leicester` back through `split_label`, so the app's own rendering
    round-trips. That is the rule `wild_type_options` states one function up, and
    it points the other way here — there the parser was `bulk_cell_lines`, which
    does *not* know the label.

    A label two of the site's own lines share is a **question, not a coin toss**,
    so those fall back to a C-number, which names exactly one row. The label
    stays as the hint, because that is what a person recognises.
    """
    if not site_id:
        return []
    lines = list(_base_queryset(db)
                 .filter(genotype=str(genotype).strip().upper(), site_id=site_id)
                 .order_by("name")[:limit])
    if not lines:
        return []

    numbers = _vial_numbers(lines, db=db)
    labels = [label(cl) for cl in lines]
    shared = {}
    for text in labels:
        shared[text] = shared.get(text, 0) + 1

    out = []
    for cl, text in zip(lines, labels):
        number = cl.c_number or numbers.get(cl.pk)
        if shared[text] > 1 and number:
            out.append({"value": f"C-{number}", "hint": text})
        else:
            out.append({"value": text, "hint": f"C-{number}" if number else ""})
    return out


def session_options(target, *, db: str = DB) -> dict:
    """The lines a session on one gene may be controlled against, box by box.

    `{"wt": [option], "ko": [option], "wt_note": str, "ko_note": str}`, where an
    option is `{"id", "label"}` — the id is what the form posts and the label is
    `label()`'s, with the freeze-down numbers spliced in.

    **Two boxes ask two questions and the form asked one for both.** It fetched
    one list and the page put anything of genotype `other` in *both* dropdowns,
    over a query that admitted `other` lines with **no gene at all**. So the
    twelfth field test opened a fresh TRPA1 with no cell lines on file, and the
    box labelled KO Cell Line offered exactly one option — a McGill line that is
    not a knockout of TRPA1 or of anything else. A dropdown with one option
    reads as the answer, and the WT box beside it had 43 sensible entries, which
    made the single KO entry look authoritative rather than wrong.

    Three rules, and each was broken by the one query:

    * **A KO box takes knockouts of this gene.** Never another gene's, which the
      old query had right, and never a line that is not a knockout, which it did
      not.
    * **A WT box takes wild types**, which carry no gene — so `target__isnull`
      is the normal case here rather than a leak, and a WT naming this target is
      kept for the paired WT/KO a supplier ships together. It is the one place
      in this file where filtering on a target is right, and only because the
      other half of the `Q` is what actually finds them.
    * **`other` is offered nowhere.** Not a narrowing: every other door already
      refuses it. `sessions._resolve_cell_line` resolves both slots with an
      explicit `genotype=`, so picking that McGill line and pressing Plan
      Session came back *"'134' is on file, but not as a knockout line"* — a
      refusal quoting a primary key the reader never typed, about a row the app
      had just offered them. Offering it cost a save and bought nothing.

    **An empty box is a sentence, not an empty picker.** A gene whose knockout
    has not been made yet is the ordinary state of a fresh target, and a picker
    holding only its own placeholder says nothing about which of "none exist"
    and "this is broken" is true. The wording is the gene page's own, which
    already says *no knockout line on file* a few centimetres from where you
    would go to add one.
    """
    gene = target_svc.gene_of(target)
    wt = list(_base_queryset(db)
              .filter(Q(genotype="WT", target__isnull=True)
                      | Q(genotype="WT", target=target))
              .order_by("name"))
    ko = list(_base_queryset(db)
              .filter(genotype="KO", target=target)
              .order_by("name"))

    numbers = _vial_numbers_all(wt + ko, db=db)
    for_gene = f" for {gene}" if gene else ""
    return {
        "wt": [_session_option(cl, numbers) for cl in wt],
        "ko": [_session_option(cl, numbers) for cl in ko],
        "wt_note": ("" if wt else
                    "No wild-type parental on file — add one on the cell lines "
                    "board."),
        "ko_note": ("" if ko else
                    f"No knockout line on file{for_gene} — add one on the cell "
                    f"lines board."),
    }


def _session_option(cl, numbers) -> dict:
    """One entry in a session's cell-line dropdown.

    The label is `label()`'s and not a second rendering of it — the board prints
    `HAP1 TRPA1 KO — Leicester` and that is therefore the string a person
    recognises. The freeze-down numbers go *before* the site rather than after,
    because the site is the last thing said about a line everywhere else in the
    app and a number after it reads as belonging to the site.

    **The line's own number is one of them.** This read the vial table alone, so
    a line was labelled with the batches frozen *from* it and never with the
    number on its own record — and a line with nothing frozen down yet was
    labelled with no number at all, however it was numbered. That is backwards
    for what the picker is for: you pick a tube out of the freezer and come back
    to plan a session against it, and run 17 found the board could find that
    number while the picker could not say it. `batch_numbers` folds the two
    together for the board's own cell and this is the same fold, done off the
    one batched query rather than per row.
    """
    text = label(cl)
    nums = sorted({*numbers.get(cl.pk, []),
                   *([cl.c_number] if cl.c_number is not None else [])})
    if nums:
        vials = " [" + ", ".join(f"C-{n}" for n in nums[:5]) + "]"
        head, sep, tail = text.partition(SITE_SEP)
        text = f"{head}{vials}{sep}{tail}"
    return {"id": cl.pk, "label": text}


def _genotype_word(genotype) -> str:
    g = (str(genotype or "").strip().upper())
    return {"WT": "wild-type", "KO": "knockout"}.get(g, "")


def refusal(value, *, genotype=None, site_id=None, db: str = DB) -> str:
    """Why that name was refused, and what could be typed instead.

    A refusal that says only "not found" is half a message — the same rule
    `services/sites.py` holds. It also has to distinguish *no such line* from
    *a line of the wrong kind*, because `SH-SY5Y` matching three knockouts and
    no wild type is a different problem with a different fix.
    """
    name, _site = split_label(value)
    word = _genotype_word(genotype)

    if genotype:
        others = candidates(value, genotype=None, db=db)
        if others:
            kinds = sorted({(cl.get_genotype_display() or cl.genotype or "?")
                            for cl in others})
            listed = ", ".join(label(cl) for cl in others[:_MAX_LISTED])
            return (f"'{name}' is on file, but not as a {word} line — "
                    f"it matches {len(others)} {'/'.join(kinds)} line"
                    f"{'' if len(others) == 1 else 's'}: {listed}. "
                    f"Name the {word} line itself.")

    known = known_labels(genotype=genotype, site_id=site_id, db=db)
    kind = f"{word} " if word else ""
    if known:
        return (f"There is no {kind}cell line called '{name}'. "
                f"On file: {', '.join(known)}.")
    return f"There is no {kind}cell line called '{name}'."


def _told_apart(lines, db: str = DB) -> tuple[list, bool]:
    """`([text], any_needed_a_number)` — a label per line, unique among them.

    **Two rows really can render the same label**, and when they do the label is
    no longer an answer to "name the one you mean": it is the question again.
    The eleventh field test hit exactly that and reported it as impossible to
    follow, correctly — *"'U2OS TRPA1 KO A1' matches 2 cell lines: U2OS TRPA1 KO
    A1 — Leicester, U2OS TRPA1 KO A1 — Leicester"*, with the closing sentence
    telling them the site after the dash was the distinguishing part.

    So a shared label falls back to a C-number, which names exactly one row —
    the rule `wild_type_options` and `picker_options` already hold one function
    up, arriving where it is load-bearing rather than merely tidy. The C-number
    is a **typable** answer and not decoration: `candidates` reads one through
    `by_c_number`, so the value this message offers is one the resolver accepts.
    A row with no number anywhere falls back to its id, which `candidates` also
    takes (a bare number is a pk).
    """
    labels = [label(cl) for cl in lines]
    shared = {}
    for text in labels:
        shared[text] = shared.get(text, 0) + 1
    if all(n == 1 for n in shared.values()):
        return labels, False

    # Only asked for when a label is actually shared — this message is on a
    # refusal path, but it is on the refusal path of a paste of a hundred rows.
    numbers = _vial_numbers(lines, db=db)
    out = []
    for cl, text in zip(lines, labels):
        if shared[text] == 1:
            out.append(text)
            continue
        number = cl.c_number or numbers.get(cl.pk)
        out.append(f"{text} — type {f'C-{number}' if number else cl.pk}")
    return out, True


def ambiguous_message(value, matches, db: str = DB) -> str:
    """More than one row answers to that name — say which, and say nothing was
    chosen. Picking one for somebody is how a session ends up controlled against
    another institution's knockout."""
    name, _site = split_label(value)
    listed, by_number = _told_apart(matches[:_MAX_LISTED], db=db)
    return (f"'{name}' matches {len(matches)} cell lines: {', '.join(listed)}. "
            + ("More than one is written the same way, so the site is not enough "
               "to tell them apart: type the C-number or id shown against the "
               "one you mean, on its own."
               if by_number else
               "Name the one you mean — the site after the dash is part of the "
               "name."))


def resolve(value, *, genotype=None, site_id=None, db: str = DB):
    """`(line, error)` for a typed cell-line name. Never creates, never guesses.

    `site_id` is a **preference**, not a filter: your own bench's line wins when
    the name is shared, which is almost always what somebody at a bench means,
    and another site's is still reachable by naming it in the label. What it
    never does is choose between two strangers — that comes back as an error
    listing both.
    """
    name, _site = split_label(value)
    if not name:
        return None, None

    matches = candidates(value, genotype=genotype, db=db)
    if not matches:
        return None, refusal(value, genotype=genotype, site_id=site_id, db=db)
    if len(matches) == 1:
        return matches[0], None

    if site_id:
        own = [cl for cl in matches if cl.site_id == site_id]
        if len(own) == 1:
            return own[0], None
        if len(own) > 1:
            return None, ambiguous_message(value, own, db=db)
    return None, ambiguous_message(value, matches, db=db)


def preview(value, *, genotype=None, site_id=None, db: str = DB) -> dict:
    """What `resolve` is about to do with this typed name, said out loud.

    `resolve` is right and silent, which is the wrong half of a rule this module
    otherwise holds. Site preference means a bare `HAP1` at Leicester lands on
    Leicester's HAP1 — correct, and the fix for the seventh field test's
    cross-institution control — but the eighth typed exactly that, got a session
    controlled against one of two lines called HAP1, and had **no way to tell
    which from the screen**. On a board whose subject is the WT/KO comparison, a
    choice made between two real rows has to be a line of text.

    `status` is one of `empty`, `resolved`, `new` (nothing answers to the name,
    so the commit would create it) or `error` (the commit will refuse). `shared`
    is how many rows answer to the name before site preference is applied, so a
    caller can say *why* there was a choice to make.
    """
    typed = (str(value) if value is not None else "").strip()
    out = {"typed": typed, "status": "empty", "label": "", "shared": 0,
           "shared_any": 0, "mine": False, "note": "", "error": ""}
    if not typed:
        return out

    matches = candidates(typed, genotype=genotype, db=db)
    # How many rows answer to the bare name at all, ignoring the genotype the
    # slot wants. `matches` is already narrowed to the slot's kind, and the
    # narrowing is what hid the U2OS case: 17 rows are called U2OS, 16 of them
    # knockouts, so asking for a *wild type* leaves exactly one and the value
    # looked unambiguous. It is — but it is somebody else's, which is the fact a
    # reader needed.
    all_named = candidates(typed, genotype=None, db=db)
    out["shared"] = len(matches)
    out["shared_any"] = len(all_named)
    line, err = resolve(typed, genotype=genotype, site_id=site_id, db=db)
    if line is not None:
        out["status"] = "resolved"
        out["label"] = label(line)
        out["mine"] = bool(site_id) and line.site_id == site_id
        name = split_label(typed)[0]
        whose = getattr(getattr(line, "site", None), "name", "")
        if not out["mine"] and whose:
            # **Not yours is worth saying even when it was not a choice.** Site
            # preference only speaks when two rows compete; a name only one lab
            # holds resolved silently to that lab's reagent, and the ninth field
            # test typed a name that is not Leicester's and got McGill's line
            # attached with a one-line, warning-free receipt.
            out["note"] = (
                f'{len(all_named)} cell line{"" if len(all_named) == 1 else "s"} '
                f'{"is" if len(all_named) == 1 else "are"} called "{name}" — '
                f"this one is {whose}'s, not yours.")
        elif len(matches) > 1:
            # Name the row and say how many it was chosen from, so "did it
            # understand me?" is answered on the page rather than by reopening
            # the record afterwards.
            out["note"] = (f'{len(matches)} cell lines are called '
                           f'"{name}" — this is your site\'s.')
        return out
    if matches:
        # `resolve` refuses a genuine tie, and refuses a name that is on file as
        # another kind of line. Either way the commit will not write; say so here
        # instead of at the end of a save.
        out["status"] = "error"
        out["error"] = err or ""
        return out
    out["status"] = "new"
    out["note"] = "not on file — it will be created with the session."
    return out


def resolve_wt(value, *, site_id=None, db: str = DB):
    """The wild type a session was run against.

    Its own function because the mistake is specific and has been made four
    times: a wild type carries **no gene**, so it is never found under a target
    and never filtered by one. One HAP1 parental serves every knockout made from
    it.
    """
    return resolve(value, genotype=CellLine.Genotype.WILD_TYPE, site_id=site_id, db=db)


def wt_for_session(session=None, *, knockout=None, db: str = DB):
    """The wild type a session's records imply, when no name was typed.

    Two sources, in the order the report generator learned to trust them: the
    knockout's own `parent_line`, and the WT a session recorded. Used to fill a
    blank rather than to override a stated value.
    """
    if knockout is not None and getattr(knockout, "parent_line_id", None):
        return _base_queryset(db).filter(pk=knockout.parent_line_id).first()
    if session is not None and getattr(session, "cell_line_wt_id", None):
        return _base_queryset(db).filter(pk=session.cell_line_wt_id).first()
    return None
