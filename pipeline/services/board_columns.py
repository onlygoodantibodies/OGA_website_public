"""What a download contains, and in what order — the same answer as the board.

The owner's review: *"Download excel includes fields that are not on the board
(e.g. lot) — they will probably need aligning — also the order being different
is confusing"*, and of the antibodies board, *"the downloaded sheet does not
match the board. They need to be the same data fields and order."*

Both were true, and the cause is that each entity had the list written out
separately for each surface. Cell lines: the export led with `c number` and
carried `lot` and `clone`, which the board does not draw, while the board's KO
validation, its reason, `received` and `thawed` were in no sheet at all.
Antibodies: the export had no comments and none of the OGA recommendations, and
sorted its columns differently again.

So the order lives here once, and the export follows the board.

**A column is not one fact but three**, which is why this is a record rather
than a list of strings:

* what the **board** does with it — draw it, or not;
* what an **upload** does with it — write it back, or refuse to;
* how the **sheet** renders it, which is not always how the screen does.

That last one is the trap the whole design nearly fell into. It is tempting to
build the sheet straight from `row_for()` — one source of truth for a cell's
value as well as its heading — but the two disagree deliberately in three
places, and taking the board's answer would have been a silent data change:

* antibody `gene`: the export writes ``gene_name or protein_name``, the board
  writes ``gene_name``. A target with a protein name and no symbol would export
  a blank gene, and the importer needs a gene to resolve a target — so exactly
  those rows would stop round-tripping.
* cell-line `gene`: the board maps the placeholder ``NA`` target to blank
  (``services/targets.py::gene_of``), which is right on screen and wrong in a
  sheet, where ``NA`` is the thing ``bulk_cell_lines`` reads to settle a blank
  genotype as WT.
* `concentration`: ``str(Decimal)`` in the sheet, ``float`` in the row payload.

So each column carries its own ``cell(obj)``, and where the sheet and the screen
differ they differ on purpose and in writing.

**A column an upload cannot write is kept and named, never dropped.** An OGA
recommendation is a verdict — the public gene pages, the browser extension index
and the MCP server all read it — so a stale spreadsheet must not re-assert it.
It stays in the download, its header says ``(read-only)``, and the upload
preview counts what it ignored. That is the same rule
``session_import._unrecognised_cols`` follows: keeping a column silently is what
loses data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from pipeline.services import lab_numbers
from pipeline.services import received as received_svc
from pipeline.services import targets as target_svc

# What an upload does with a column.
WRITE = "write"      # the parser accepts it and `apply` stores it
MATCH = "match"      # used to find the row, not overwritten
READ = "read"        # in the sheet for reference; an upload ignores it

# What the board does with it.
GRID = "grid"        # drawn, and editable in a cell
IDENTITY = "identity"  # drawn, changed only through the identity dialog
OFF = "off"          # not on the board


@dataclass(frozen=True)
class Column:
    key: str                       # the row_for() key, where there is one
    sheet: str                     # header in the export and the blank template
    label: str = ""                # the board's <th>; defaults to `sheet`
    upload: str = WRITE
    board: str = GRID
    cell: Optional[Callable] = None   # model instance -> sheet value
    # A unit, kept out of the heading text — see `unit` below.
    unit: str = ""

    @property
    def heading(self) -> str:
        return f"{self.sheet} (read-only)" if self.upload == READ else self.sheet

    @property
    def th(self) -> str:
        """The board's own heading — empty when this column is drawn *inside*
        another's cell, which several are: a cell line's catalogue number sits
        under Supplier / catalogue, its KO reason under KO validated, and
        received/thawed under Site / status. Those still belong in the sheet as
        columns of their own; they just do not add a heading to the grid."""
        return self.label

    def value(self, obj):
        return "" if self.cell is None else self.cell(obj)


def _gene(obj) -> str:
    """The gene as a *sheet* wants it.

    ``gene_name or protein_name`` — the export has always had that fallback and
    it is load-bearing: the antibody importer resolves a target by gene, so a
    row whose target has only a protein name needs something in the cell or it
    cannot come back. And unlike the board, the placeholder ``NA`` is written
    out rather than blanked, because ``NA`` is exactly what the cell-line
    importer reads to settle a blank genotype as a wild type.
    """
    target = getattr(obj, "target", None)
    if target is None:
        return ""
    return (target.gene_name or target.protein_name or "").strip()


def _company(obj) -> str:
    # `.name`, not `display_name` — a record shows the name it is stored under
    # (CLAUDE.md; `cropper/db.py::company_label`).
    return obj.company.name if obj.company_id else ""


def _site(obj) -> str:
    return obj.site.name if obj.site_id else ""


def _yes_no(value) -> str:
    return "yes" if value else "no"


_APPS = (("wb", "WB"), ("ip", "IP"), ("if", "IF"), ("fc", "FC"))

# What the *supplier* claims is a wider question than what OGA tested, and the
# model has always had the columns for it. Typing `WB, IHC` stored WB and lost
# IHC without a word, because every list in the chain stopped at four — so the
# supplier column reads six and the OGA verdict stays four. They answer
# different questions and are drawn side by side, which is exactly why they must
# not share a list.
_SUPPLIER_APPS = _APPS + (("ihc", "IHC"), ("elisa", "ELISA"))


def _recommended(obj) -> str:
    return ", ".join(lbl for a, lbl in _APPS
                     if getattr(obj, f"{a}_recommended", False))


def _supplier_validated(obj) -> str:
    return ", ".join(lbl for a, lbl in _SUPPLIER_APPS
                     if getattr(obj, f"supplier_validated_{a}", False))


def _ab_reference(obj):
    """The lab's A-number, or an empty cell.

    This used to fall back to the record id so that every row had *something*
    under ``ab #``, on the reasoning that a blank is useless as a reference.
    The first field test on A-numbers showed the fallback is worse than the
    blank: the board said `not numbered` and the sheet said `4550` about the
    same vial, and 4550 is neither the vial's number nor a value the upload
    reads back. See ``services/lab_numbers.py::sheet_number``, the one writer
    for this cell and for every bench sheet's copy of it.
    """
    return lab_numbers.sheet_number(obj)


def _concentration(obj) -> str:
    # str(Decimal), not float: the sheet shows what is stored.
    return str(obj.concentration) if obj.concentration is not None else ""


def _parent(obj) -> str:
    """The sheet's parent cell — **the bench's own text wins.**

    This emitted the linked line's *name* whenever the FK was set, so
    `backfill_cell_line_parents` linking 156 rows silently rewrote what a
    downloaded sheet says: `C-16`, the freeze-down batch the bench recorded,
    became a bare `HeLa`. The board can draw both (`cell_lines.parent_label`);
    a **sheet cell has to stay one value the parser accepts back**, and
    `bulk_cell_lines.resolve_parent` takes a name or a C-number but not
    `HeLa · C-16`.

    So the recorded text is what travels, because it is the record and it
    round-trips exactly; the linked line's name is the fallback for the 20 rows
    that carry a link and no text.
    """
    recorded = (obj.parental_line_name or "").strip()
    if recorded:
        return recorded
    return obj.parent_line.name if obj.parent_line_id else ""


def _c_number(obj) -> str:
    from pipeline.services import c_number as c_number_svc
    return "" if obj.c_number is None else c_number_svc.label(obj.c_number)


def _storage_types(obj) -> str:
    """The freezer this vial lives in. Imported inside the function because
    ``services/storage.py`` reaches the ORM and this module is imported from
    ``views/imports.py`` at module scope — the same arrangement ``_batches``
    below has, for the same reason."""
    from pipeline.services import storage as storage_svc
    return storage_svc.types(obj)


def _boxes(obj) -> str:
    from pipeline.services import storage as storage_svc
    return storage_svc.boxes(obj)


def _received(obj) -> str:
    """``2026-08-14``, ``2026-08`` or ``2026`` — never more precise than what
    was recorded, and always a spelling ``received.parse`` reads back.

    Not the printed label: *14 Aug 2026* round-trips fine but sorts
    alphabetically in a spreadsheet and is not a date to Excel.
    """
    return received_svc.cell_value(obj)


def _batches(obj) -> str:
    """Every freeze-down batch on a line, the way the board draws them.

    `cell_lines` is the one reader for both halves — which numbers a line has
    (its own plus its vials') and how a run of them collapses. Imported inside
    the function because that module reaches the ORM and this one is imported
    from `views/imports.py` at module scope.
    """
    from pipeline.services import cell_lines as cell_line_svc
    return cell_line_svc.format_batches(cell_line_svc.batch_numbers(obj))


# ── antibodies ───────────────────────────────────────────────────────────────
#
# Board order. `ab #` leads because it is a reference rather than a value and
# the sheet is read left to right; everything after it is the board's own order,
# which is what the review asked for.
ANTIBODIES: tuple[Column, ...] = (
    # The lab's own A-number, drawn *inside* the first cell above the catalogue
    # rather than as a column of its own — the same `label=""` arrangement as
    # `clone_id` under Clonality. The first column is the sticky one, and what a
    # reader needs pinned while scrolling an antibodies board sideways is the
    # catalogue number; the A-number rides along with it.
    #
    # Writable, but only from a cell that carries the `A`. A sheet downloaded
    # before numbers were issued again holds a bare *record* id in this column,
    # and writing that back would relabel the row with a number nobody chose —
    # `lab_numbers.read_reference` is what tells the two apart, and
    # `bulk_antibodies` leaves the record-id case alone.
    Column("ab_number", "ab #", "", board=GRID, cell=_ab_reference),
    Column("catalogue", "catalogue", "Catalogue", upload=MATCH, board=IDENTITY,
           cell=lambda a: a.catalogue_number or ""),
    Column("company", "company", "Supplier", upload=MATCH, board=IDENTITY, cell=_company),
    Column("gene", "gene", "Gene", upload=MATCH, board=IDENTITY, cell=_gene),
    Column("rrid", "rrid", "RRID", board=IDENTITY, cell=lambda a: a.rrid or ""),
    Column("lot_number", "lot", "Lot", cell=lambda a: a.lot_number or ""),
    # **A unit is never case-transformed.** The board's header row is styled
    # `uppercase`, and CSS uppercases `µ` (U+00B5 MICRO SIGN) to `Μ` (U+039C
    # GREEK CAPITAL MU) — which is the same glyph as a Latin M in every font
    # this site uses. So a column headed `Conc. (µg/mL)` rendered as
    # **CONC. (MG/ML)** over values like 1000, 500 and 529, and anybody reading
    # the screen at face value was out by a thousand. Exactly the failure
    # `services/concentration.py` exists to prevent, arriving through the
    # stylesheet instead of through the parser. The unit is carried apart from
    # the heading so the template can draw it `normal-case`; nothing else about
    # the column changes, and the sheet keeps its ASCII `ug`.
    Column("concentration", "concentration (ug/mL)", "Conc.", unit="µg/mL",
           cell=_concentration),
    Column("clonality", "clonality", "Clonality", cell=lambda a: a.clonality or ""),
    # No heading of its own: the board draws the clone id under Clonality, which
    # is what that column's tip has always described. It is still its own column
    # in the sheet.
    Column("clone_id", "clone", "", cell=lambda a: a.clone_id or ""),
    # No heading of its own — drawn under Clonality beside the clone id, which
    # is where a reader looks for it and what keeps a 15-wide board from
    # becoming 17. Filled on **2,823** antibodies and in no sheet and on no
    # screen until now (1 Sep 2026); uOttawa wrote their own `Isotype` column
    # because of it.
    Column("isotype", "isotype", "", cell=lambda a: a.isotype or ""),
    Column("host_species", "host", "Host", cell=lambda a: a.host_species or ""),
    # The verdict. In the sheet so a download is a complete record, read-only so
    # a stale copy cannot re-assert it — the public gene pages, the extension
    # index and the MCP server all read this.
    Column("recommended", "OGA recommends", "OGA recommends",
           upload=READ, cell=_recommended),
    # What the supplier says it is for, beside what OGA found. One name on every
    # sheet (`cropper/metadata.py::HEADER_ALIASES`): this said `supplier claims`
    # while the blank template said `applications`, and nothing here checked that
    # a column declared writable is one the parser can actually read — so this
    # one was declared `WRITE`, kept out of `read_only()`, and dropped in silence
    # on every upload. `TheSheetRoundTripsTests` now pins the declaration against
    # the alias map.
    Column("supplier_validated", "supplier recommendations", "Supplier recommends",
           cell=_supplier_validated),
    # **What the supplier says it cross-reacts with**, so it sits beside their
    # other claim rather than beside Host. `H, M, R` is the lab's own notation
    # and 2,798 rows carry it; like isotype it was in the database, on no screen
    # and in no sheet. Its heading says whose claim it is, for the same reason
    # `OGA recommends` and `Supplier recommends` are drawn as a pair.
    Column("species_reactivity", "reactivity", "Reactivity (supplier)",
           cell=lambda a: a.species_reactivity or ""),
    Column("site", "site", "Site", cell=_site),
    # ── where the vial actually is, and when it turned up ────────────────────
    #
    # Neither of these is new. `InventoryLocation` has carried the freezer and
    # the box since the first migration and 3,058 antibodies on live have one
    # (107 distinct −20 °C boxes at McGill); `Antibody.received_date` is filled
    # on 2,841 rows. Both were reachable only through Django admin — drawn on no
    # screen, in no sheet, and editable by nobody — which is why uOttawa asked
    # for "a box number field and the month it was received" as if they did not
    # exist. From here they are ordinary columns.
    #
    # Storage and box are two cells rather than one because they answer two
    # questions a person asks separately: *which freezer do I walk to* and
    # *which box do I pull*. `services/storage.py` composes them for anything
    # that wants one string, and shows **every** location a row has — 134
    # antibodies on live are genuinely in two places, and a cell that silently
    # drew the first would send somebody to the wrong freezer while looking
    # exactly like an answer.
    Column("storage", "storage", "Storage", cell=_storage_types),
    Column("box", "box", "Box", cell=_boxes),
    # The date carries its own precision (`services/received.py`), so a row that
    # only knows *August 2026* exports `2026-08` and comes back as August rather
    # than being promoted to the 1st on the round trip.
    Column("received", "received", "Received", cell=_received),
    # **In kind or purchased**, beside the date it arrived, because the two are
    # the same question about the same delivery. Filled on every one of the
    # 3,261 live rows by the Access import — 3,034 in kind, 146 purchased, 81
    # unknown — and drawn on no screen and in no sheet until now, so uOttawa
    # asked for it believing it had never been recorded (4 Sep 2026).
    #
    # The sheet emits the stored code (`in_kind`), not the label, for the same
    # reason `clonality` does: `dataset.py` compares against the stored column,
    # so a label here would preview a spurious change on every row. The board
    # draws the label; `metadata.parse_acquisition` reads either back.
    Column("acquisition_method", "acquisition", "Acquisition",
           cell=lambda a: a.acquisition_method or ""),
    Column("supplier_url", "product url", board=OFF, cell=lambda a: a.supplier_url or ""),
    Column("comments", "comments", "Comments", cell=lambda a: a.comments or ""),
)

# ── cell lines ───────────────────────────────────────────────────────────────
#
# `name` leads, as the board does — the export led with `c number`, which is the
# freeze-down batch rather than the line.
CELL_LINES: tuple[Column, ...] = (
    Column("name", "name", "Name", upload=MATCH, board=IDENTITY,
           cell=lambda c: c.name or ""),
    Column("gene", "gene", "Gene", upload=MATCH, board=IDENTITY, cell=_gene),
    Column("genotype", "genotype", "Genotype", upload=MATCH, board=IDENTITY,
           cell=lambda c: c.genotype or ""),
    Column("parent", "parent", "Parent", board=IDENTITY, cell=_parent),
    # **Identity, not a cell, and drawn where the rest of the identity is.**
    # A gene and a background define the knockout; the clone says which one —
    # a separate single-cell origin, often a separate guide — so it is part of
    # what the row *is*, on the same footing as the gene and the genotype. It
    # was `board=OFF` from the day this registry was written: recorded on 61
    # live rows, editable through the patch endpoint, in the sheet, searchable
    # by `find.py`, and drawn on no screen at all. `MATCH` because
    # `bulk_cell_lines.find_cell_line` now resolves on it: a clone in an
    # uploaded sheet says which row to fill in, and a sheet must not be able to
    # rename one clone into another.
    Column("clone", "clone", "Clone", upload=MATCH, board=IDENTITY,
           cell=lambda c: c.clone or ""),
    # `C-631`, the way it is written on the tube — the board's own rendering, and
    # what `c_number.parse` reads back (it takes `631`, `C-631` and `C631`
    # alike, and `bulk_cell_lines` compares the *parsed* integer against the
    # stored one, so a prefixed cell previews no spurious change). The sheet
    # emitted the bare integer while the board beside it said `C-631`, which is
    # one field written two ways in two places a person reads in the same minute.
    Column("c_number", "c number", "C-number", cell=_c_number),
    # **Read-only, and in the sheet because a download that cannot carry the
    # numbers on the tubes is a lossy round trip.** The freeze-down batches were
    # in no sheet at all: run 17 froze two batches, downloaded the board it had
    # just used, and C-10004 and C-10005 — the numbers actually written on the
    # vials — appeared nowhere in the file. Not writable, because a batch is one
    # press of `+ batch` (one batch, one C-number, issued by `lab_numbers` on
    # `pre_save`), and a column people could type into would be a second write
    # path for numbering that the whole of `services/lab_numbers.py` exists to
    # keep in one place. `read_only()` names it, so a preview says it is ignored
    # rather than leaving somebody to find out.
    Column("batches", "freeze-down batches", "", upload=READ, board=OFF,
           cell=_batches),
    Column("cellosaurus_id", "cellosaurus", "Cellosaurus",
           cell=lambda c: c.cellosaurus_id or ""),
    Column("company", "supplier", "Supplier / catalogue", upload=MATCH,
           board=IDENTITY, cell=_company),
    Column("catalogue_number", "catalogue", "", cell=lambda c: c.catalogue_number or ""),
    # On the board and, until now, in no sheet at all. The board's heading is
    # "KO confirmed" because that is what its badge answers — the tick and the
    # reason read together (`services/ko_validation.py`) rather than the bit on
    # its own. The *sheet* header is untouched: it is the stored column, it sits
    # next to `ko validation notes` so both halves travel together, and renaming
    # it would make every downloaded sheet on somebody's disk stop matching.
    Column("ko_validated", "ko validated", "KO confirmed", upload=READ,
           cell=lambda c: _yes_no(c.ko_validated)),
    Column("ko_validation_notes", "ko validation notes", "", upload=READ,
           cell=lambda c: c.ko_validation_notes or ""),
    # **Adherent or suspension.** Filled on 411 rows by the Access import, in
    # `EDITABLE_FIELDS` and in `row_for` from the day this board was written,
    # and in no column on any screen and no sheet — the same dark shape as the
    # freezer box and the received date, and register #91's open question.
    # uOttawa answered it: a student looking a line up needs to know how it
    # grows before they thaw it (14 Sep 2026). Beside `medium`, because the two
    # are one question — how to culture this line.
    Column("growth_properties", "growth properties", "Growth",
           cell=lambda c: c.growth_properties or ""),
    # **Human or mouse.** McGill asked for it the same afternoon as Growth, for
    # the same reason — it is a fact you need before you use a line, and the
    # board did not say it. Live is 604 Human, 8 Mouse, 2 Rat, 1 Monkey, 1 Dog,
    # so the answer is nearly always the same, which is exactly why a reader
    # needs to see the exception rather than assume the rule.
    Column("species", "species", "Species", cell=lambda c: c.species or ""),
    Column("medium", "medium", "Medium", cell=lambda c: c.medium or ""),
    Column("site", "site", "Site / status", cell=_site),
    Column("received", "received", "", upload=READ, cell=lambda c: _yes_no(c.received)),
    Column("thawed", "thawed", "", upload=READ, cell=lambda c: _yes_no(c.thawed)),
    Column("lot_number", "lot", board=OFF, cell=lambda c: c.lot_number or ""),
    Column("origin_comments", "comments", board=OFF,
           cell=lambda c: c.origin_comments or ""),
)

_REGISTRY = {"antibodies": ANTIBODIES, "cell-lines": CELL_LINES}


def registry(kind: str) -> tuple[Column, ...]:
    return _REGISTRY.get(kind, ())


def sheet_headers(kind: str) -> list[str]:
    """The export's columns, in board order."""
    return [c.heading for c in registry(kind)]


def read_only(kind: str) -> list[str]:
    """Headers an upload will not act on — named, never silently ignored."""
    return [c.heading for c in registry(kind) if c.upload == READ]


def row_values(kind: str, obj) -> list:
    return [c.value(obj) for c in registry(kind)]


def board_columns(kind: str) -> list[Column]:
    """The columns the board draws, in order — for the template's ``<thead>``."""
    return [c for c in registry(kind) if c.board != OFF and c.label]
