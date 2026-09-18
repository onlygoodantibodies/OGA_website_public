"""Site-scoped lab numbers — the ``A-`` on an antibody, the ``C-`` on a cell line.

The lab numbered its reagents long before this app existed: an antibody is
``A-118`` on a freezer box and a cell line is ``C-631`` on a tube. Both came
across in the Access import and both are still columns —
``Antibody.ab_number``, ``CellLine.c_number`` and ``CellLineVial.c_number``.
(`access_csvs/Antibodies.csv` carries ``AbNumber`` 1–3084 on 3,137 of 3,142
rows; `CellLines.csv` carries ``LabLabel`` 1–744, all distinct. Read 4 Aug
2026.)

What did not survive the import is the **numbering**. Nothing has issued one
since, so every record added through this app carries a blank where the lab's
own reference goes, and the antibodies board never drew the column at all — so
there was no way to notice that, let alone fix it.

Three facts shape this, and each one rules something out:

* **A number belongs to a site, not to the consortium.** McGill's ``A-1`` and
  Leicester's ``A-1`` are different antibodies and always were: each bench keeps
  its own freezer and its own run of numbers. So the scope of "the next number"
  is ``site_id``, and a record with no site gets none — "next" is not a question
  with an answer until you know whose bench is asking.
* **Nothing is unique across the table**, so no migration adds a constraint.
  The Access rows themselves use 54 antibody numbers twice, and a constraint
  that the live data violates is a deploy that fails. A collision a *person*
  types is refused here instead, by name, which is the case that matters.
* **Blank stays blank.** The rows with no number are Leicester's (owner, 4 Aug
  2026) — that bench never used the convention, so its old records are not
  missing a number, they simply do not have one. Nothing here backfills, and
  assignment is on **creation only**: editing a 2019 record must not mint it a
  number it never had.

One module for all three jobs, so the grid and the paste box cannot disagree the
way they did over ``C-RUN11-01`` — see ``services/c_number.py``, which is this
module's cell-line face and keeps its name because six callers and a rule in
CLAUDE.md use it:

* **read** a number a person typed — ``631``, ``C-631``, ``C631`` — and refuse
  what is not one, by name and with what is accepted;
* **issue** the next one at a site when nobody typed one;
* **refuse a collision**, naming the record that already holds the number.

Assignment happens in ``pipeline/signals.py``, on ``pre_save``, rather than in
each of the five write paths that create these records. That is deliberate and
it is the lesson this repo keeps re-learning: a rule enforced in one door and
not another is the shape of nearly every defect in CLAUDE.md. A number is issued
wherever a record is born — the boards, a paste, an upload, the cropper, a
session, the Django admin, a shell — because there is one place that issues it.
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from django.db.models import Max

DB = "pipeline_db"

ANTIBODY = "antibody"
CELL_LINE = "cell_line"


@dataclass(frozen=True)
class Kind:
    key: str
    prefix: str      # the letter written on the box or the tube
    attr: str        # the model field
    field: str       # what a refusal calls the column
    noun: str        # what a refusal calls the record
    example: str     # a number of the right order of magnitude for this kind

    @property
    def accepted(self) -> str:
        return f"a plain number, optionally written {self.prefix}-{self.example}"


KINDS: dict[str, Kind] = {
    ANTIBODY: Kind(ANTIBODY, "A", "ab_number", "ab number", "antibody", "118"),
    CELL_LINE: Kind(CELL_LINE, "C", "c_number", "c number", "cell line", "631"),
}


def spec(kind: str) -> Kind:
    return KINDS[kind]


# ``631``, ``C-631``, ``C631``, ``c 631``, ``#631`` — the label as it is written
# on the tube, and nothing else. Anchored at both ends on purpose: an unanchored
# search is the bug this whole module's cell-line half was written for, and
# ``C-RUN11-01`` is what it cost.
def _pattern(prefix: str) -> re.Pattern:
    return re.compile(rf"^[{prefix}#]?[\s\-–—]*(\d+)$", re.IGNORECASE)


_PATTERNS = {k: _pattern(v.prefix) for k, v in KINDS.items()}


def parse(raw, *, kind: str, field: str = "") -> tuple[int | None, str]:
    """``(number, error)``.

    ``(None, "")`` for a blank cell — blank means "not written down", which is
    never a reason to refuse and is exactly what makes room for a number to be
    issued instead. ``(None, message)`` when the cell says something this cannot
    turn into a number; the message quotes what was typed and says what is
    accepted, because "not a number" is wrong about the case that matters —
    ``C-RUN11-01`` *contains* a number. Otherwise ``(int, "")``.
    """
    k = spec(kind)
    name = field or k.field
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None, ""
    m = _PATTERNS[kind].match(text)
    if not m:
        return None, (f"'{text}' is not a {name} — {name} is {k.accepted}. "
                      f"A label that is not a plain number belongs in the "
                      f"comments column, where it is kept as written.")
    return int(m.group(1)), ""


def label(value, *, kind: str) -> str:
    """``C-631`` for 631 — the way it is written on the tube — and ``""`` for
    nothing recorded.

    The prefix is not decoration. A bench sheet's ``ab #`` column has always
    printed a bare record id, so once A-numbers are real a bare integer under
    that heading is ambiguous between the two; the prefix is what tells a reader
    — and ``bench_results._resolve_ab`` — which of them a cell holds.
    """
    return f"{spec(kind).prefix}-{value}" if value not in (None, "") else ""


# ── the `ab #` column, and the thing it must not do ──────────────────────────
#
# Every sheet that names an antibody has an `ab #` column, and it used to print
# ``ab_number or access_id or pk`` — falling back to a *record* id so that a row
# created since the Access import still had something in the cell. The first
# field test on A-numbers found what that costs, and it is worse than a blank:
#
#   antibodies board   NB110-40763   not numbered
#   the same row, exported                 4550
#   session 501's bench sheet              4550
#
# 4550 looks exactly like an answer. It is written on a tube or quoted in a
# message as that vial's number, the board says the vial has no number at all,
# and on the way back in it is ignored — so the column exported a value that is
# not the vial's identifier and does not survive the round trip. Three surfaces,
# three answers, and the one a person would act on is the wrong one.
#
# So the cell is the A-number or it is **empty**, which is the true thing and
# already what an upload does with it. Reading still accepts a bare integer as
# a record id (``read_reference``) because sheets printed before this are on
# people's disks — nothing *writes* one any more.

def sheet_number(ab) -> str:
    """What a sheet prints in its ``ab #`` column: ``A-118``, or nothing."""
    return label(getattr(ab, "ab_number", None), kind=ANTIBODY)


_BARE = re.compile(r"^\d+$")


def read_reference(raw) -> tuple[str, int | None]:
    """``("number", 118)``, ``("record", 42)`` or ``("", None)``.

    ``"number"`` only when the cell carries the ``A`` — a reader must not guess
    that a bare integer is an A-number, because for every sheet downloaded
    before this it is not.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return "", None
    if _BARE.match(text):
        return "record", int(text)
    m = _PATTERNS[ANTIBODY].match(text)
    if m and not text.lstrip().startswith("#"):
        return "number", int(m.group(1))
    return "", None


# ── issuing ──────────────────────────────────────────────────────────────────

def highest(kind: str, site_id, *, db: str = DB) -> int | None:
    """The largest number in use at this site, or ``None`` if the site has none.

    For cell lines this reads **both** tables. 145 of the parents recorded in
    the Access data are a ``CellLine.c_number`` and 102 a ``CellLineVial``'s
    (CLAUDE.md), ``services/cell_lines.py::by_c_number`` looks in both, and a
    number issued here that already labels somebody's freeze-down batch would be
    exactly the ambiguity that reader exists to resolve.
    """
    from pipeline.models import Antibody, CellLine, CellLineVial

    if not site_id:
        return None
    if kind == ANTIBODY:
        return (Antibody.objects.using(db).filter(site_id=site_id)
                .aggregate(m=Max("ab_number"))["m"])
    line = (CellLine.objects.using(db).filter(site_id=site_id)
            .aggregate(m=Max("c_number"))["m"])
    vial = (CellLineVial.objects.using(db).filter(cell_line__site_id=site_id)
            .aggregate(m=Max("c_number"))["m"])
    return max([v for v in (line, vial) if v is not None], default=None)


def next_number(kind: str, site_id, *, db: str = DB) -> int | None:
    """The number this site's next record would get, or ``None`` with no site.

    One past the highest in use, so McGill continues from its Access run
    (A-3085 next, C-745 next) and a bench that never numbered anything starts at
    1. A gap in the middle of the run is **not** filled — the box it was written
    on may still be in the freezer, and the point of the number is that it says
    one thing. Deleting the highest record does free its number again, which is
    the one case this does not protect against; closing that would mean keeping
    a counter per site, and a counter is a second place the truth lives.
    """
    if not site_id:
        return None
    return (highest(kind, site_id, db=db) or 0) + 1


def holder(kind: str, site_id, number, *, db: str = DB, exclude_pk=None):
    """The record already carrying this number at this site, or ``None``.

    Returns the model instance so the caller can name it — a refusal that says
    only "already taken" leaves you guessing which record you would be
    colliding with.
    """
    from pipeline.models import Antibody, CellLine, CellLineVial

    if not site_id or number is None:
        return None
    if kind == ANTIBODY:
        qs = (Antibody.objects.using(db)
              .filter(site_id=site_id, ab_number=number)
              .select_related("company", "target"))
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs.first()
    qs = (CellLine.objects.using(db)
          .filter(site_id=site_id, c_number=number).select_related("target"))
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    line = qs.first()
    if line is not None:
        return line
    vials = (CellLineVial.objects.using(db)
             .filter(cell_line__site_id=site_id, c_number=number)
             .select_related("cell_line"))
    if exclude_pk:
        vials = vials.exclude(cell_line_id=exclude_pk)
    return vials.first()


def holder_label(obj) -> str:
    """What a refusal calls the record standing in the way."""
    from pipeline.models import Antibody, CellLine, CellLineVial

    if isinstance(obj, Antibody):
        bits = [obj.company.name if obj.company_id else "",
                obj.catalogue_number or f"#{obj.pk}",
                f"({obj.target.gene_name})" if obj.target_id and obj.target.gene_name else ""]
        return " ".join(b for b in bits if b)
    if isinstance(obj, CellLine):
        bits = [obj.name or f"#{obj.pk}", obj.genotype or "",
                obj.target.gene_name if obj.target_id and obj.target.gene_name else ""]
        return " ".join(b for b in bits if b)
    if isinstance(obj, CellLineVial):
        return f"a freeze-down batch of {obj.cell_line.name or f'#{obj.cell_line_id}'}"
    return str(obj)


def clash(kind: str, site_id, number, *, db: str = DB, exclude_pk=None,
          site_name: str = "") -> str:
    """``""`` when the number is free at this site, or the refusal to show.

    Named, with the record that holds it and with the way out — the shape
    ``identity.vial_clash_message`` uses, and for the same reason: "could not
    save that" tells you it failed and nothing you can act on.
    """
    other = holder(kind, site_id, number, db=db, exclude_pk=exclude_pk)
    if other is None:
        return ""
    k = spec(kind)
    where = f" at {site_name}" if site_name else ""
    free = next_number(kind, site_id, db=db)
    return (f"{label(number, kind=kind)} is already {holder_label(other)}{where}. "
            f"Two records with one {k.field} cannot be told apart on a freezer "
            f"box. Leave the cell empty and the next free number "
            f"({label(free, kind=kind)}) is given to this {k.noun}, or type one "
            f"nobody is using.")


# ── assignment ───────────────────────────────────────────────────────────────

_state = threading.local()


@contextmanager
def suspended():
    """Issue no numbers inside this block.

    For the historical importers only. ``import_access_data`` and
    ``import_leicester_data`` reconstruct what the lab recorded years ago; a
    row that arrives without a number arrives that way because nobody wrote one
    on the box, and inventing one during a re-import would put a number on a
    2019 record that no freezer agrees with.
    """
    previous = getattr(_state, "off", False)
    _state.off = True
    try:
        yield
    finally:
        _state.off = previous


def is_suspended() -> bool:
    return getattr(_state, "off", False)


_WITHHELD = "_lab_number_withheld"


def withhold(instance):
    """Leave this one record's number blank instead of issuing it.

    For the row whose number cell said something that could not be read.
    ``bulk_cell_lines`` writes that row anyway — an odd batch label is no
    reason to discard a good cell line — and the number has to stay **blank**,
    not become the next free one: the person was telling us what is written on
    the tube, we could not read it, and quietly filing the row under a
    different number papers over exactly that. The preview names the refusal,
    the save counts it, and the cell stays empty until somebody fixes it.
    """
    setattr(instance, _WITHHELD, True)
    return instance


def kind_of(instance) -> str | None:
    from pipeline.models import Antibody, CellLine, CellLineVial

    if isinstance(instance, Antibody):
        return ANTIBODY
    if isinstance(instance, CellLine):
        return CELL_LINE
    # A freeze-down batch draws from the **same** run as the line — `highest`
    # and `holder` have always read both tables, because both numbers get
    # written on tubes. A batch created with no number is therefore issued the
    # site's next one, the same way a line is, rather than by whichever write
    # path happened to make it.
    if isinstance(instance, CellLineVial):
        return CELL_LINE
    return None


def assign(instance, *, db: str = DB) -> int | None:
    """Give a record being created its site's next number. Returns what it got.

    Five things it refuses to do, and each is a decision rather than an
    oversight: it never renumbers a record that is only being **saved** again
    (assignment is at birth); it never overwrites a number somebody **typed**;
    it never numbers a record with **no site**, because the run of numbers is
    the site's; it never touches **old** rows, which is the same rule as the
    first one seen from the owner's side — blank data stays blank; and it stands
    back where a caller has called ``withhold`` on the record, which is how a
    number cell nobody could read stays empty rather than being replaced by a
    number nobody typed.
    """
    kind = kind_of(instance)
    if kind is None or is_suspended() or getattr(instance, _WITHHELD, False):
        return None
    if not instance._state.adding:
        return None
    k = spec(kind)
    if getattr(instance, k.attr, None) is not None:
        return None
    site_id = getattr(instance, "site_id", None)
    if not site_id:
        return None
    number = next_number(kind, site_id, db=db)
    setattr(instance, k.attr, number)
    return number


def ensure_numbered(records, *, db: str = DB) -> list[dict]:
    """Give a number to each of these that has none. Returns what was issued.

    **The backstop for "a number exists before the experiment".** Since 2 Sep
    2026 an antibody logged on the board is created *without* one, so that a
    bench receiving reagents over weeks can deal the numbers out in one go and
    keep a protein's vials in one freezer box (``services/renumber.py``). That
    freedom has to end somewhere, and it ends here: planning a session is the
    moment a bench sheet gets printed, ``sheet_number`` prints the A-number on
    it, and a blank cell there is a tube nobody can identify — the same defect
    that column had when it printed a record id.

    Ordered, and per site: two records at one bench take consecutive numbers,
    because each is saved before the next asks for the highest. ``assign`` holds
    every rule about *when* a number is issued at birth; this is the one place
    that issues one to a record that already exists, and it still refuses a
    record with no site and never overwrites a number already there.
    """
    issued = []
    if is_suspended():
        return issued
    for obj in records:
        kind = kind_of(obj)
        if kind is None:
            continue
        k = spec(kind)
        if getattr(obj, k.attr, None) is not None:
            continue
        site_id = getattr(obj, "site_id", None)
        if not site_id:
            continue
        number = next_number(kind, site_id, db=db)
        setattr(obj, k.attr, number)
        # `update_fields` so nothing else on the row is rewritten — the same
        # reason `renumber.apply` uses it.
        obj.save(using=db, update_fields=[k.attr])
        issued.append({"number": label(number, kind=kind),
                       "name": holder_label(obj)})
    return issued
