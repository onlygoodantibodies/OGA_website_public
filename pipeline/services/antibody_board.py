"""The antibodies board — read layer.

Same four-function contract as the target and session boards
(``board_queryset`` → ``apply_filters`` → ``row_for`` → ``board_rows``), so the
shared ``static/pipeline/board.js`` drives it unchanged.

An antibody is one row, like a target — no second level — so this is the
simplest of the three. What it replaces is a four-way split: a search page, a
read-only detail page, a separate edit form, and a bulk paste box.

Two things stay off the board on purpose:

  * **RRID** is written through ``rrid_utils`` (bare ``AB_<n>`` plus a registry
    link), never typed into a cell. It *can* be given on a paste or a sheet —
    the ``rrid`` column of the new-entry grid — which goes through the same
    writer. Only the inline cell is closed.
  * **Identity** — target, company, catalogue number — is the antibody's key
    ``(catalogue, company, target, lot, site)``. Retyping one silently makes the
    row a different antibody, so identity is changed on the antibody page where
    the consequence is visible, not inline.
"""
from __future__ import annotations

from django.db.models import Q

from pipeline.models import Antibody, Company, Site
from pipeline.services import board_page as board_page_svc
from pipeline.services import clonality as clonality_svc
from pipeline.services import find
from pipeline.services import lab_numbers
from pipeline.services import received as received_svc
from pipeline.services import sites as site_svc
from pipeline.services import storage as storage_svc
from pipeline.services import targets as target_svc

DB = "pipeline_db"

APPLICATIONS = ["wb", "ip", "if", "fc"]

# What the supplier claims covers more ground than what OGA tests, and the model
# has the columns for it — see `board_columns._SUPPLIER_APPS`. Two lists on
# purpose: the OGA verdict is about the four applications OGA characterises.
SUPPLIER_APPLICATIONS = APPLICATIONS + ["ihc", "elisa"]

# Free-text and boolean fields the board may edit in place.
EDITABLE_FIELDS = {
    "lot_number", "antigen", "clonality", "clone_id", "host_species",
    "isotype", "species_reactivity", "concentration", "supplier_url",
    "comments", "site", "ab_number", "storage", "box", "received_date",
    "acquisition_method",
}

# The two cells that are not columns on ``Antibody`` at all: a freezer and a box
# live on ``InventoryLocation``, one row per place a vial is kept, and
# ``services/storage.py`` is the one reader and writer. They are in
# ``EDITABLE_FIELDS`` above so "is this cell editable" has a single answer, and
# the patch endpoint branches on them before the generic ``setattr`` — which
# would otherwise put a box number on the antibody as an attribute nothing
# saves.
LOCATION_FIELDS = {"storage", "box"}

# ``received_date`` is a date whose *precision* is a second column, so it goes
# through ``services/received.py`` rather than ``parse_date``: "August 2026" is
# a real answer and must not become 1 August.
DATE_FIELDS = {"received_date"}

# ``ab_number`` is an IntegerField and the cell holds ``A-118``, so the patch
# endpoint reads it through ``services/lab_numbers.py`` rather than ``int()`` —
# the same arrangement the cell-lines board has for C-numbers, and for the same
# reason: an emptied cell means "not written down" and must clear the field
# rather than fail on ``int("")``.
NUMERIC_FIELDS = {"ab_number"}

# One registry for the hover text on the board. When this board grows a file
# round-trip, the workbook's header comments come from here too — the target and
# session boards already work that way, and the point is that the page and the
# spreadsheet cannot end up saying different things about the same column.
COLUMN_TIPS = {
    "catalogue": "The supplier's catalogue number. Not editable in the grid: "
                 "together with supplier, gene, lot and site it is the "
                 "antibody's identity, and retyping one part makes the row a "
                 "different antibody while every result still points here. "
                 "Click it to change what this antibody is.",
    # This tip used to name Company.resolve. A function name is not something a
    # scientist can act on, and the third field test read it as leaked code.
    "company": "The supplier. Not editable in the grid — a supplier is matched "
               "against the ones already on file so the same vendor never ends "
               "up recorded under two spellings. Click the catalogue number to "
               "change it.",
    "gene": "The gene this antibody is against. Not editable in the grid — part "
            "of the identity key. Click the catalogue number to change it.",
    "rrid": "The Research Resource Identifier. Not editable in this cell — the "
            "bare AB_number and its registry link are written together so the "
            "two halves cannot drift apart. If you already know it, put it in "
            "the RRID column when you add or paste the antibody and it is kept. "
            "Otherwise it stays empty: looking one up means calling the Antibody "
            "Registry, which is too slow to do while you wait.",
    "lot_number": "Lot number of the vial in hand. Different lots of the same "
                  "catalogue number can behave differently, which is why lot "
                  "is part of the identity.",
    "concentration": "Stock concentration in µg/mL — the column stores a number "
                     "and the unit is always µg/mL. Type a unit and it is "
                     "converted (1.0 mg/mL becomes 1000); type one this cannot "
                     "convert and it is refused rather than filed as the bare "
                     "digits. This is where a paste that could not read a "
                     "concentration tells you to come.",
    "acquisition_method": "Whether this vial was contributed in kind by the "
                          "supplier or purchased by the lab. Click to change it.",
    "clonality": "Monoclonal or polyclonal, whether it is recombinant, and the "
                 "clone ID — which is often how a paper cites an antibody. "
                 "Recombinant is its own tick because it is its own column, and "
                 "the two sites filled the pair differently: Leicester records "
                 "a recombinant as monoclonal with the tick on, McGill as the "
                 "clonality itself. Every screen that only prints the clonality "
                 "composes the two.",
    "host_species": "The species the antibody was raised in. Determines which "
                    "secondary you need.",
    "recommended": "What OGA recommends this antibody for, from "
                   "knockout-controlled testing. Click an application to "
                   "toggle it. This is our verdict, not the supplier's.",
    "supplier_validated": "What the supplier claims the antibody works for. "
                          "Recorded separately from our own verdict on "
                          "purpose — the two often disagree.",
    "species_reactivity": "Which species the supplier says the antibody "
                          "cross-reacts with — their claim, like the column "
                          "beside it, not something OGA tested. The lab writes "
                          "it as initials: H, M, R for human, mouse, rat. The "
                          "cell offers the combinations already on file.",
    "site": "Which YCharOS site holds the vial.",
    "storage": "Which freezer the vial is kept in — 4C, -20, -80, LN2 or RT. "
               "A vial can be recorded in two places (a working aliquot and a "
               "backup); both are shown, and a row with two is edited in "
               "Django admin rather than here, because one cell cannot say "
               "which of them you meant.",
    "box": "The box the vial is in, as it is written on the box. Free text — "
           "a number, a name, whatever your freezer uses. Set the storage "
           "temperature too, or the record says which box with no freezer to "
           "look in.",
    "received": "When this vial arrived. Type as much as you know: a full date "
                "(2026-08-14), a month (Aug 2026) or a year. A month stays a "
                "month — nothing turns it into the 1st. A slashed date where "
                "both numbers could be the month is refused rather than "
                "guessed.",
    "comments": "Free text. Anything worth knowing that no other column holds.",
}

BOOLEAN_FIELDS = {
    "is_recombinant", "out_of_market", "empty_vial",
    "wb_recommended", "ip_recommended", "if_recommended", "fc_recommended",
    "supplier_validated_wb", "supplier_validated_ip",
    "supplier_validated_if", "supplier_validated_fc",
    "supplier_validated_ihc", "supplier_validated_elisa",
}


def board_queryset():
    # `locations` is prefetched because `row_for` draws the freezer and the box,
    # and without it a 50-row page is 50 extra queries — the same reason the
    # cell-line export prefetches `vials` to print freeze-down batches.
    return (Antibody.objects.using(DB)
            .select_related("target", "company", "site")
            .prefetch_related("locations"))


def apply_filters(qs, *, q="", company="", site="", gene="", recommended="",
                  clonality="", application="", out_of_market="", numbered=""):
    """Filters mirror how people look an antibody up: by gene, by supplier, by
    catalogue number, and by whether OGA recommends it."""
    if q:
        # One list, in `services/find.py`, shared with the nav search box. This
        # one was missing lot number and comments.
        qs = qs.filter(find.antibody_q(q))
    # One gene, or a comma-separated list of them, so a link built on one board
    # means the same here — and **`NA` for the rows with no gene at all**.
    #
    # The thirteenth field test typed `NA` here — which is what the cell-lines
    # board's own hint tells you to type for exactly this — and got nothing. Same
    # word, adjacent boards, two behaviours, which from outside is a search box
    # that does not work rather than one that is narrower.
    #
    # **`Antibody.target` is NOT NULL**, which this comment used to say the
    # opposite of. So the shape it reaches here is not "no target" but a target
    # whose `gene_name` is blank, plus the Access-era `NA` placeholder. Checked
    # against live on 2 Sep 2026: **0 rows** of either, where the cell-lines board
    # has 148. That is a reason to keep the filter, not to drop it — it is the
    # shape a blank gene cell takes, and a board that answers a word its
    # neighbour teaches is the whole point — but do not read a count off this.
    # `services/targets.py::gene_q_with_na` is the one reader for both.
    if gene:
        gene_filter = target_svc.gene_q_with_na("target", gene)
        if gene_filter is not None:
            qs = qs.filter(gene_filter)
    if company:
        qs = qs.filter(company_id=company)
    if site:
        # A pk, a name or a short code — see services/sites.py::filter_by.
        qs = site_svc.filter_by(qs, "site_id", site)
    if clonality:
        # The label, through services/clonality.py — `clonality__iexact` asked
        # the enum alone, so filtering the board to `recombinant` returned
        # McGill's rows and none of Leicester's 178, which record the same fact
        # in `is_recombinant`. A bare enum value still works, for a filter that
        # rode in on a `?clonality=` URL.
        qs = qs.filter(clonality_svc.label_q(clonality))
    if application in APPLICATIONS:
        qs = qs.filter(**{f"{application}_recommended": True})
    if recommended in ("yes", "no"):
        want = recommended == "yes"
        any_rec = Q(wb_recommended=True) | Q(ip_recommended=True) \
            | Q(if_recommended=True) | Q(fc_recommended=True)
        qs = qs.filter(any_rec) if want else qs.exclude(any_rec)
    if out_of_market in ("yes", "no"):
        qs = qs.filter(out_of_market=(out_of_market == "yes"))
    # **Which rows have no A-number.** Two different people need this and for
    # opposite reasons: a bench that deliberately held its numbers back needs to
    # find those rows again to assign them, and Leicester's 325 unnumbered rows
    # are unnumbered *on purpose* — that bench never used the convention, and
    # nothing backfills them. So this is a filter and never a warning.
    if numbered in ("yes", "no"):
        qs = qs.filter(ab_number__isnull=(numbered == "no"))
    return qs


def row_for(antibody) -> dict:
    return {
        "id": antibody.pk,
        # The lab's own reference, as it is written on the freezer box. Sent as
        # the label rather than the bare integer because that is what the cell
        # shows *and* what an edit of it types back — `lab_numbers.parse` reads
        # `A-118`, `A118` and `118` alike, so the round trip is closed.
        "ab_number": lab_numbers.label(antibody.ab_number,
                                       kind=lab_numbers.ANTIBODY),
        "catalogue": antibody.catalogue_number or "",
        "company": antibody.company.name if antibody.company_id else "",
        # Through `gene_of`, as the cell-lines board's GENE cell already is: the
        # Access-era placeholder target called NA is not a gene, and printing it
        # in this column says an antibody is against a gene called NA. It is also
        # what makes the cell and the `?gene=NA` filter agree — an empty cell and
        # the word that finds it.
        "gene": target_svc.gene_of(antibody.target) if antibody.target_id else "",
        "target_id": antibody.target_id,
        "rrid": antibody.rrid or "",
        "rrid_link": antibody.rrid_link or "",
        "lot_number": antibody.lot_number or "",
        "clonality": antibody.clonality or "",
        # The stored code. The cell draws the label from `cellChoices` and keeps
        # this in `data-value`, so one fact has one control and the grid sends
        # back something the writer accepts.
        "acquisition_method": antibody.acquisition_method or "",
        "clone_id": antibody.clone_id or "",
        "host_species": antibody.host_species or "",
        "isotype": antibody.isotype or "",
        "species_reactivity": antibody.species_reactivity or "",
        "concentration": (float(antibody.concentration)
                          if antibody.concentration is not None else None),
        "site": antibody.site.name if antibody.site_id else "",
        # Where the vial is. Two cells, and a third value that says whether one
        # of them may be typed into: a vial recorded in two places has two
        # boxes, and `storage.one_of` refuses the edit rather than letting a
        # grid cell pick one. The board draws the reason beside the value —
        # never on a `title`, which a reader who has not hovered never sees.
        "storage": storage_svc.types(antibody),
        "box": storage_svc.boxes(antibody),
        "locations": len(storage_svc.locations(antibody)),
        # The stored spelling, so an edit round-trips: `received.parse` reads
        # back `2026-08` as August, where the printed label below is what the
        # cell shows.
        "received_date": received_svc.cell_value(antibody),
        "received_label": received_svc.of(antibody),
        "supplier_url": antibody.supplier_url or "",
        "comments": antibody.comments or "",
        "is_recombinant": antibody.is_recombinant,
        "out_of_market": antibody.out_of_market,
        "empty_vial": antibody.empty_vial,
        "recommended": {a: getattr(antibody, f"{a}_recommended", False)
                        for a in APPLICATIONS},
        "supplier_validated": {
            a: getattr(antibody, f"supplier_validated_{a}", False)
            for a in SUPPLIER_APPLICATIONS},
    }


def _ordered(**filters):
    return (apply_filters(board_queryset(), **filters)
            .order_by("target__gene_name", "company__name", "catalogue_number"))


def board_rows(**filters) -> list[dict]:
    """Every matching row — for exports and for tests that want the whole set."""
    return [row_for(a) for a in _ordered(**filters)]


def board_page(*, page=1, per_page=board_page_svc.DEFAULT_PER_PAGE, locate=None,
               **filters) -> dict:
    """One page of antibodies. This is the board the review crashed: 3,225 rows
    of fifteen cells, built here and painted in one string there."""
    return board_page_svc.slice_rows(_ordered(**filters), row_for,
                                     page=page, per_page=per_page, locate=locate)


def cell_choices() -> dict:
    """What each cell may hold, for ``board.js``'s ``cellChoices``.

    Every cell on this board was a bare text box, including three the server
    then refused: `clonality` is a four-value enum, `site` is refused unless it
    is one on file, and `storage` is one of six temperatures. A grid that hands
    you a text box for a closed set is offering a mistake and reporting it after
    the save — the defect the sessions board's `status` cell had, one board over.

    The open ones are the other half of the same rule and the reason they are
    not `<select>`s: an isotype, a host species and a reactivity string are
    conventions, not enums, and a vocabulary nobody may add to stops describing
    the bench. They are drawn from what the column already holds
    (`services/vocabulary.py`), folded so a picker cannot offer `rabbit` and
    `Rabbit` side by side and entrench a split it was drawn to fix.
    """
    from pipeline.services import storage as storage_svc
    from pipeline.services import vocabulary

    return {
        # Closed: the model's own enum. `is_recombinant` stays a separate tick —
        # the two columns answer one question and neither answers it alone
        # (`services/clonality.py`), so folding them into one picker here would
        # be a third reader of a fact this repo has twice been burned by.
        "clonality": vocabulary.enum(Antibody.Clonality.choices),
        # Closed: three values, and the writer refuses anything else
        # (`views/antibody_board.py`). A picker alone is half of enforcing a
        # closed set — that is the lesson `clonality` taught one line up.
        "acquisition_method": vocabulary.enum(Antibody.AcquisitionMethod.choices),
        # Closed, and blank is a real answer: clearing the site is how a vial
        # stops belonging to a bench, and `sites.strict_id` reads "" as exactly
        # that. One reader for all four boards — each used to write the list out
        # again, which is how the same field came to be a picker on one screen
        # and a text box on the next.
        "site": vocabulary.sites(blank="— no site —"),
        # Closed. The cell holds the printed label (`−20°C`), which is what
        # `storage.read_type` reads back — so the values offered are the labels
        # themselves rather than the stored codes.
        "storage": {"strict": True,
                    "values": [{"value": "", "label": "— not recorded —"}]
                    + [{"value": text, "label": text}
                       for text in storage_svc.TYPE_LABELS.values()]},
        # Open, from the rows themselves.
        "host_species": vocabulary.choices(Antibody, "host_species"),
        "isotype": vocabulary.choices(Antibody, "isotype"),
        "species_reactivity": vocabulary.choices(Antibody, "species_reactivity"),
    }


def filter_options() -> dict:
    return {
        "companies": list(Company.objects.using(DB).order_by("name")),
        "sites": list(Site.objects.using(DB).filter(is_active=True).order_by("name")),
        # The labels the rows actually hold, not the enum's four values — the
        # picker has to offer `Recombinant monoclonal`, which is a pair of
        # columns rather than anything the clonality column ever stores.
        "clonalities": clonality_svc.options_for(Antibody.objects.using(DB)),
        "applications": APPLICATIONS,
    }


def unnumbered_count(member) -> int:
    """How many of this bench's antibodies have no A-number yet.

    Since 2 Sep 2026 logging an antibody does not mint one — the number decides
    which freezer box a vial goes in, so numbering on arrival scatters a
    protein across as many boxes as it had deliveries. That makes "not numbered
    yet" a normal, growing state rather than an anomaly, and a set somebody has
    to be able to find: the alternative is discovering it on a printed bench
    sheet with a blank ``ab #`` column.

    Scoped to the member's own site, because that is whose numbers they are
    (``renumber.plan`` refuses another bench's). ``0`` for an account with no
    site, which has nothing of its own to number.
    """
    site_id = getattr(member, "site_id", None)
    if not site_id:
        return 0
    return (Antibody.objects.using(DB)
            .filter(site_id=site_id, ab_number__isnull=True).count())
