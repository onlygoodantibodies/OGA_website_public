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
from pipeline.services import sites as site_svc
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
    "comments", "site", "ab_number",
}

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
    "site": "Which YCharOS site holds the vial.",
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
    return (Antibody.objects.using(DB)
            .select_related("target", "company", "site"))


def apply_filters(qs, *, q="", company="", site="", gene="", recommended="",
                  clonality="", application="", out_of_market=""):
    """Filters mirror how people look an antibody up: by gene, by supplier, by
    catalogue number, and by whether OGA recommends it."""
    if q:
        # One list, in `services/find.py`, shared with the nav search box. This
        # one was missing lot number and comments.
        qs = qs.filter(find.antibody_q(q))
    # One gene, or a comma-separated list of them, so a link built on one board
    # means the same here — and **`NA` for the rows with no gene at all**.
    #
    # `Antibody.target` is nullable and 8 rows on file have none, so they answered
    # no gene filter and there was no way to list them: the thirteenth field test
    # typed `NA`, which is what the cell-lines board's own hint tells you to type
    # for exactly this, and got nothing. Same word, same boards, two behaviours.
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
        "clone_id": antibody.clone_id or "",
        "host_species": antibody.host_species or "",
        "isotype": antibody.isotype or "",
        "species_reactivity": antibody.species_reactivity or "",
        "concentration": (float(antibody.concentration)
                          if antibody.concentration is not None else None),
        "site": antibody.site.name if antibody.site_id else "",
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
