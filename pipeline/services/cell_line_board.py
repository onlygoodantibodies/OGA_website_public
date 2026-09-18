"""The cell lines board — read layer.

Fourth board on the same contract as targets, sessions and antibodies
(``board_queryset`` → ``apply_filters`` → ``row_for`` → ``board_rows``).

A cell line is one row. What it replaces is the same four-way split the
antibodies board replaced: a list page, a read-only detail page, a separate edit
form and a bulk paste box.

Not editable in a cell, deliberately:

  * **name, target, genotype, parent line** — the structure. A KO line means
    "this gene knocked out in that parent"; retyping any of those in a grid
    makes the row describe a different line while every session that used it
    still points here.
  * **supplier** — companies go through ``Company.resolve``, never a bare name
    (see CLAUDE.md). Changing it belongs on the cell line page.
"""
from __future__ import annotations

from django.db.models import Q

from pipeline.models import CellLine, Site
from pipeline.services import board_page as board_page_svc
from pipeline.services import c_number as c_number_svc
from pipeline.services import cell_lines as cell_lines_svc
from pipeline.services import find
from pipeline.services import ko_validation
from pipeline.services import sites as site_svc
from pipeline.services import targets as target_svc

DB = "pipeline_db"

# `clone` is deliberately **not** here: it is identity, changed through the
# identity dialog like the name, the gene and the genotype. A gene and a
# background define the knockout and the clone says which one, so retyping it in
# a grid cell makes the row describe a different single-cell line while every
# session, vial and reading recorded against it stays attached — the same
# sentence this module's docstring already writes about the other four.
EDITABLE_FIELDS = {
    "c_number", "catalogue_number", "lot_number", "cellosaurus_id", "species",
    "growth_properties", "medium", "origin", "origin_comments",
    "location_original_vial", "ko_validation_notes", "site",
}

# c_number is an IntegerField. Typed into a text cell it arrives as a string, and
# an emptied cell arrives as "" — which int() refuses, so clearing a wrong
# C-number used to fail with "could not save that" and no way round it.
NUMERIC_FIELDS = {"c_number"}

# ``arrived_with_ko`` is NOT here, and must not be: it is a ForeignKey to the KO
# line a wild-type arrived alongside, not a yes/no flag. Treating it as one let
# the patch endpoint try to store True in a FK, and putting it in a board row
# put a CellLine object into a JsonResponse — see ``row_for``.
BOOLEAN_FIELDS = {"ko_validated", "received", "thawed"}

# One registry for the board's hover text, and for a workbook's header comments
# when this board grows a round-trip — so the page and the sheet cannot end up
# describing the same column differently.
# Keyed on the **registry's** column keys (services/board_columns.py), which are
# the same keys `row_for` returns and the export writes. Three of these used to
# be keyed on display names — `cellosaurus`, `supplier`, `site_status` — so when
# the board's <thead> started rendering from the registry those three headers
# silently lost their hover text. `tests_columns.py` pins the two lists against
# each other now, because an empty title="" is invisible: it reads as a column
# nobody thought worth explaining.
COLUMN_TIPS = {
    "name": "The line's name. Not editable in the grid: every session that used "
            "this line points at this row, so renaming it here would rewrite "
            "history rather than correct it. Click the name to change what the "
            "line is.",
    "gene": "For a KO line, the gene that is knocked out. Blank for a wild "
            "type, which is not a knockout of anything — 'NA' in a sheet means "
            "the same and is read as blank. Not editable here: it is what the "
            "line is.",
    "genotype": "WT, KO, or other. Not editable here for the same reason as "
                "the gene: a session's whole meaning depends on which of the "
                "two lines was which.",
    "parent": "The wild type this knockout was made from. Two things where they "
              "differ: the line it is linked to, then the C-number the bench "
              "recorded — which names a specific freeze-down batch, and matters "
              "because one name covers several stocks (HeLa covers 17, from five "
              "suppliers). 'not linked' means the reference resolved to nothing "
              "or to the wrong background; run backfill_cell_line_parents to see "
              "why. A KO only means something alongside the WT it came from.",
    "clone": "Which single-cell clone this knockout is — often made with a "
             "different guide. The gene and the background say what was knocked "
             "out and in what; the clone says which one, so two rows here named "
             "the same thing are two different knockouts and not one listed "
             "twice. Where there is more than one on file the cell says how "
             "many. Not editable here: like the gene and the genotype, it is "
             "what the line is — click it to change it.",
    "c_number": "This line's own freeze-down batch, as written on the tube.",
    "cellosaurus_id": "Cellosaurus accession — the public identifier for a cell "
                   "line. Worth filling in: it is what makes the line "
                   "citable in a paper.",
    # This tip named an internal function and a page that has been retired. A
    # scientist cannot act on either. Name the thing on screen instead.
    "company": "Supplier and catalogue number. The catalogue number is editable "
                "here; the supplier is matched against the ones already on file, "
                "so change it by clicking the line's name.",
    "ko_validated": "Whether the knockout has actually been confirmed, and "
                    "how. Click to toggle. The badge reads the tick and the "
                    "reason together, so a row ticked over a reason saying the "
                    "check failed asks to be looked at rather than showing a "
                    "green confirmation. An unconfirmed KO line makes every "
                    "result that used it provisional.",
    "growth_properties": "How the line grows — adherent or suspension. Click to "
                         "change it; the box offers the spellings already in use.",
    "species": "Which species the line came from. Nearly all are human, which is "
               "why the few that are not are worth seeing.",
    "medium": "Growth medium and supplements.",
    "site": "Which site holds the line, and whether it has arrived and "
                   "been thawed yet.",
}


def board_queryset():
    # `vials` prefetched because `row_for` prints every batch number: without
    # it the board runs one query per row, which is the cost rule
    # `tests_timeouts.py` pins as "does not grow with row count".
    return (CellLine.objects.using(DB)
            .select_related("target", "company", "site", "parent_line")
            .prefetch_related("vials"))


def apply_filters(qs, *, q="", genotype="", gene="", site="", ko_validated="",
                  received=""):
    if q:
        # The same fields the nav search box uses — `services/find.py` owns the
        # list. They were two lists and they disagreed: run 9 typed a catalogue
        # number into this box and got nothing while the global box found both
        # rows.
        #
        # **`.distinct()` comes with that `Q`, and reusing one without the other
        # is what run 17 found.** Two of its clauses reach `vials`, which puts a
        # join on the batch table into the query — so a *batch* number matches
        # one joined row and looks right, while the **line's own** number is
        # true of every joined row at once. HeLa's seventeen batches drew the
        # one line seventeen times, byte-identical, under a count inflated to
        # agree. `find.py::_cell_lines` has said this in a comment since
        # C-numbers became searchable, which is why `/pipeline/find/` answered
        # *1 match* for the string this board answered seventeen times.
        #
        # Only this clause needs it: gene, site, genotype and KO confirmation
        # are forward FKs or columns on the line, and none of them multiplies a
        # row.
        #
        # Safe under `board_page.page_of`, which asks this queryset for bare
        # `pk`s while it is ordered on a joined column — checked because live is
        # PostgreSQL and the suite is SQLite, which is laxer here. Django adds
        # the ordering columns to a `SELECT DISTINCT` itself, so the statement is
        # valid there; and every column it adds is one value per line, so the
        # dedupe still collapses the rows the vial join made.
        qs = qs.filter(find.cell_line_q(q)).distinct()
    if genotype:
        qs = qs.filter(genotype=genotype)
    if gene:
        # **`NA` means the wild types**, and it has to, because a wild type has
        # no gene — so `filter(target__gene_name=…)` can never return one, and
        # anything gated on "the board is narrowed to a gene" would put every
        # parental line permanently out of reach. Deleting is gated exactly that
        # way, which is how this surfaced.
        #
        # `NA` is already the app's word for "there isn't one"
        # (`services/targets.py::NOT_APPLICABLE`, the same sentinel the paste
        # box takes in the gene column), so this is the existing convention
        # reaching the filter rather than a special case invented for it. Both
        # shapes count: no target at all, and the Access-era placeholder target
        # called NA that 96 of the 136 wild types still point at.
        # A comma-separated list narrows to exactly those genes, the same as on
        # the other three boards — and `NA` is one of the terms it may carry, so
        # `?gene=NA,STMN2` is "the wild types and the STMN2 knockouts". Building
        # it term by term is what makes that possible: `gene_q` alone cannot,
        # since a wild type has no gene for it to match.
        # Lifted into `services/targets.py::gene_q_with_na` once the antibodies
        # board needed the same rule — 8 antibodies have no target either, and a
        # word that works on one board and not its neighbour reads as broken.
        gene_filter = target_svc.gene_q_with_na("target", gene)
        if gene_filter is not None:
            qs = qs.filter(gene_filter)
    if site:
        qs = site_svc.filter_by(qs, "site_id", site)
    # The filter has to mean what the badge means, or narrowing to "Confirmed"
    # puts a row the board itself draws as *check this row* at the top of the
    # list — which is exactly how this was found. `services/ko_validation.py`
    # owns the rule for both.
    if ko_validated == "yes":
        qs = qs.filter(ko_validation.confirmed_q())
    elif ko_validated == "no":
        qs = qs.exclude(ko_validation.confirmed_q())
    elif ko_validated == "disputed":
        qs = qs.filter(ko_validation.disputed_q())
    if received in ("yes", "no"):
        qs = qs.filter(received=(received == "yes"))
    return qs


def row_for(line, *, clone_total=None) -> dict:
    """One board row. Every value must be JSON — a plain string, number or bool.

    ``clone_total`` is how many clones of this knockout are on file, supplied by
    the caller from one batched query (`cell_lines.clone_counts`). Left out it is
    asked per row, which is the N+1 `tests_cell_line_board.py` already caught
    once on `batch_numbers` — so the two callers that draw many rows pass it and
    the single-row patch response does not need to.

    This is where the board died. ``arrived_with_ko`` is a ForeignKey to the KO
    line a wild-type was shipped alongside, and returning it returned the
    *CellLine object*, which ``JsonResponse`` cannot serialise: one such row took
    the whole response down with a 500, so the board showed nothing and blamed
    the filters. Only wild-type rows carry it, which is why filtering to KO
    looked fine and opening the board did not. It is not returned at all now —
    nothing on the board draws the pairing — and the row payload is pinned as
    JSON-serialisable by tests_board_fieldtest.py.
    """
    return {
        "id": line.pk,
        "name": line.name or "",
        # ``C-631``, the way it is written on the tube — not the bare integer.
        # The cell shows this and an edit types it back; `c_number.parse` reads
        # `631`, `C-631` and `C631` alike, so the round trip is closed.
        "c_number": c_number_svc.label(line.c_number),
        # Every freeze-down batch on the line, consecutive runs collapsed. The
        # line's own column holds one number — the first batch's, bridged up
        # there by the Access-era importer — so a row found by searching a
        # *second* batch's number would otherwise show a number that is not the
        # one you typed, and read as a wrong result.
        "batches": cell_lines_svc.batch_label(line),
        # `NA` is not a gene. The Access-era import created a target called NA
        # ("Not applicable") and 96 of the 136 wild types point at it, so this
        # column printed NA in the GENE cell of rows whose whole point is that
        # they have no gene — and the board's own "no gene — wild type" note,
        # which is the correct rendering, never fired for any of them.
        # `targets.gene_of` is the one reader (services/targets.py).
        "gene": target_svc.gene_of(line.target) if line.target_id else "",
        # So the GENE cell can link to the gene's page, the way the targets and
        # antibodies boards do. Paired with `gene` above and never read without
        # it: a wild type on the NA placeholder has a target_id and no gene, and
        # a link to *that* row's page is a link to a page about nothing.
        "target_id": line.target_id,
        "genotype": line.genotype or "",
        # The linked parental *and* the batch the bench recorded — see
        # `cell_lines.parent_label`. Drawing only the linked name lost the
        # C-number off 156 rows the moment they were linked.
        "parent": cell_lines_svc.parent_label(line),
        "company": line.company.name if line.company_id else "",
        "catalogue_number": line.catalogue_number or "",
        "lot_number": line.lot_number or "",
        "cellosaurus_id": line.cellosaurus_id or "",
        "species": line.species or "",
        "clone": line.clone or "",
        # What the CLONE cell draws: the clone as recorded, and — when this
        # knockout has siblings — how many there are. A cell showing one of
        # several looks like an answer (the rule `services/storage.py` holds for
        # an antibody kept in two boxes), and "all the clones of a KO" is the
        # thing a bench asked for by name and the board could not say.
        "clone_note": cell_lines_svc.clone_note(line, total=clone_total),
        "clone_total": clone_total if clone_total is not None else 1,
        "growth_properties": line.growth_properties or "",
        "medium": line.medium or "",
        "origin": line.origin or "",
        "origin_comments": line.origin_comments or "",
        "location_original_vial": line.location_original_vial or "",
        "site": line.site.name if line.site_id else "",
        "ko_validated": line.ko_validated,
        "ko_validation_notes": line.ko_validation_notes or "",
        # What the tick and the reason say *together*. The board drew the tick
        # alone as a green pill reading "validated", so six live rows carried a
        # green confirmation directly above their own record saying the
        # validation failed. `services/ko_validation.py` is the one reader, and
        # the gene page's table uses it too.
        **{f"ko_{k}": v for k, v in ko_validation.badge(line).items()},
        "received": line.received,
        "thawed": line.thawed,
    }


def _ordered(**filters):
    return (apply_filters(board_queryset(), **filters)
            .order_by("target__gene_name", "genotype", "name"))


def board_rows(**filters) -> list[dict]:
    """Every matching row — for exports and for tests that want the whole set."""
    lines = list(_ordered(**filters))
    totals = cell_lines_svc.clone_counts(lines)
    return [row_for(c, clone_total=totals.get(c.pk)) for c in lines]


def board_page(*, page=1, per_page=board_page_svc.DEFAULT_PER_PAGE, locate=None,
               **filters) -> dict:
    """One page of cell lines — see ``services/board_page.py``.

    The slicer is asked for the *objects* and the rows are built here, so the
    clone counts are one query for the page rather than one per drawn row.
    Slicing still happens before any row is built, which is the invariant
    `board_page.slice_rows` exists to hold.
    """
    result = board_page_svc.slice_rows(_ordered(**filters), lambda line: line,
                                       page=page, per_page=per_page, locate=locate)
    lines = result["rows"]
    totals = cell_lines_svc.clone_counts(lines)
    result["rows"] = [row_for(c, clone_total=totals.get(c.pk)) for c in lines]
    return result


def cell_choices() -> dict:
    """What each cell may hold — see ``antibody_board.cell_choices``.

    `site` is the one closed set here and it is closed because `strict_id`
    already refuses anything else; the rest are conventions the bench writes in
    its own words, so they are reminders drawn from the column itself.

    **`site` is the only entry, and the two reasons for that are different.**

    `medium` is drawn and holds 120 distinct values on live, well past
    `vocabulary.MAX_OPTIONS` — `on_file` would answer with nothing, so naming it
    would read as an oversight when it is a decision. A datalist of 120 media is
    a scroll, not a reminder; same for `clone` (61) and
    `location_original_vial` (82).

    `species` (5 distinct over 567 rows), `growth_properties` (4 over 411) and
    `origin` (11 over 488) were the opposite problem and the more interesting
    one: they have exactly the vocabulary worth offering, and this board drew no
    cell for any of them. They are in `EDITABLE_FIELDS`, so the patch endpoint
    would save them, and no column existed to type into — the same dark shape as
    the box and the received date, and register #91's question.

    **The benches answered it for two of the three within an hour of each
    other** (14 Sep 2026). uOttawa: a student looking a line up needs to know
    whether it is adherent or in suspension before they thaw it. McGill, on the
    same thread: the species matters too, human or mouse. Both are columns now
    (`services/board_columns.py`) and therefore pickers here. `origin` still
    draws no cell, so it is still not here — a picker for a cell nobody draws is
    dead configuration.

    Both are `<datalist>`s and not `<select>`s. Adherent and suspension are what
    anybody writes, and the other spellings on file are somebody describing a
    line more precisely than an enum would let them; species is 604 Human, 8
    Mouse, 2 Rat, 1 Monkey and 1 Dog, and the next line through the door could
    be from an animal none of those name.
    """
    from pipeline.services import vocabulary

    return {
        "site": vocabulary.sites(blank="— no site —"),
        "growth_properties": vocabulary.choices(CellLine, "growth_properties"),
        "species": vocabulary.choices(CellLine, "species"),
    }


def panel_choices() -> dict:
    """Vocabularies for the **Add cell lines** panel, which is not the grid.

    Two sets the grid has no cell for. `genotype` is part of the identity, so it
    is chosen when a line is created and changed afterwards only through the
    identity dialog — which means the Add panel is the one surface that needs to
    offer it. `storage` on this sheet is the *location note*
    (`bulk_cell_lines.HEADER_ALIASES` maps it to `location`, and `_storage_type`
    reads the shelf back out of it), so the temperatures are offered as the
    convention the sheet's own example already teaches rather than as a
    narrowing.
    """
    from pipeline.models import CellLine
    from pipeline.services import storage as storage_svc

    return {
        "genotype": [v for v, _label in
                     (CellLine._meta.get_field("genotype").choices or [])],
        "storage": list(storage_svc.TYPE_LABELS.values()),
    }


def filter_options() -> dict:
    return {
        "sites": list(Site.objects.using(DB).filter(is_active=True).order_by("name")),
        "genotypes": [{"value": v, "label": l}
                      for v, l in CellLine._meta.get_field("genotype").choices or []],
    }
