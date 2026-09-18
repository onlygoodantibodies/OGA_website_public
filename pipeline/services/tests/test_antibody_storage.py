"""Where an antibody vial is, written from a sheet and edited in a cell.

``InventoryLocation`` has held a freezer and a box since the first migration and
3,058 antibodies on live carry one (read 1 Sep 2026) — all of it invisible, in
no sheet and editable by nobody, which is why the first bench to keep its own
spreadsheet wrote a "Box #" column believing the portal had none.

Two things here would be silently wrong and neither shows on a screen:

* a re-uploaded sheet **stacking** a second freezer on the same vial, or
  overwriting a box somebody corrected on the board;
* a cell edit on a vial recorded in **two** places picking one of them — 134
  antibodies on live are genuinely in two (a −20 °C working aliquot and a −80 °C
  backup), and the wrong one silently relabelled sends somebody to the wrong
  freezer.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import (Antibody, Company, InventoryLocation, Site,
                                 Target)
    for M in (InventoryLocation, Antibody, Company, Target):
        M.objects.using(DB).all().delete()
    site, _ = Site.objects.using(DB).get_or_create(
        short_code="STO", defaults={"name": "Storage Site"})
    other, _ = Site.objects.using(DB).get_or_create(
        short_code="STP", defaults={"name": "Storage Partner"})
    target = Target.objects.using(DB).create(gene_name="ADAM10",
                                             protein_name="ADAM10")
    Company.objects.using(DB).create(name="Abcam")
    return {"site": site, "other": other, "target": target}


def _antibody(seeded, catalogue="ab124695", **kw):
    from pipeline.models import Antibody
    return Antibody.objects.using(DB).create(
        target=seeded["target"], catalogue_number=catalogue,
        site=kw.pop("site", seeded["site"]), **kw)


# ── the paste / upload path ──────────────────────────────────────────────────

def test_a_sheet_that_says_where_the_vial_is_records_it_once(seeded):
    from pipeline.models import InventoryLocation
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    row = {"storage_type": "(-20°C)", "box": "42"}
    S.write_row(ab, row)
    S.write_row(ab, row)          # the same sheet uploaded a second time

    locs = InventoryLocation.objects.using(DB).filter(antibody=ab)
    assert locs.count() == 1
    assert locs.first().storage_type == "-20" and locs.first().box == "42"


def test_a_sheet_fills_a_blank_and_never_overwrites(seeded):
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    S.write_row(ab, {"storage_type": "-20", "box": "42"})
    # Somebody corrected the box on the board; a months-old sheet must not put
    # the old value back. Fill-only-blank, like every other column on this path.
    S.write_row(ab, {"storage_type": "-80", "box": "7", "position": "A1"})

    loc = S.locations(ab)[0]
    assert loc.box == "42" and loc.storage_type == "-20"
    assert loc.position == "A1"          # the blank one still fills


def test_a_row_saying_nothing_about_storage_creates_nothing(seeded):
    from pipeline.models import InventoryLocation
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    assert S.write_row(ab, {"gene": "ADAM10", "catalogue": "ab124695"}) is None
    assert not InventoryLocation.objects.using(DB).filter(antibody=ab).exists()


def test_a_unicode_minus_off_a_supplier_page_is_the_same_shelf(seeded):
    from pipeline.services import storage as S
    assert S.read_type("(−20 °C)")[0] == "-20"      # U+2212
    assert S.read_type("-80C")[0] == "-80"
    assert S.read_type("LN2")[0] == "ln2"
    assert S.read_type("+4°C")[0] == "4c"


def test_a_paste_keeps_a_row_it_cannot_place_and_a_cell_refuses_it(seeded):
    # One reader, and the caller says what an unrecognised value means: a paste
    # files it under "other" and keeps the antibody, a person typing into one
    # cell gets told what is on offer while they are still looking at it.
    from pipeline.services import storage as S
    assert S.read_type("in Ali's drawer", default=S.OTHER) == (S.OTHER, "")
    code, err = S.read_type("in Ali's drawer")
    assert code == "" and "4C, -20, -80" in err


# ── the cell edit ────────────────────────────────────────────────────────────

def test_a_vial_in_two_places_is_never_silently_edited(seeded):
    from pipeline.models import InventoryLocation
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    InventoryLocation.objects.using(DB).create(
        antibody=ab, site=seeded["site"], storage_type="-20", box="42")
    InventoryLocation.objects.using(DB).create(
        antibody=ab, site=seeded["site"], storage_type="-80", box="7")

    # Both are shown, in both columns — a cell drawing only the first would look
    # exactly like an answer.
    assert S.boxes(ab) == "42 · 7"
    assert S.summary(ab) == "−20°C box 42 · −80°C box 7"

    _loc, err = S.set_field(ab, "box", "99")
    assert "2 places" in err
    assert S.boxes(ab) == "42 · 7"          # nothing moved


def test_a_first_box_creates_the_location_and_clearing_it_removes_it(seeded):
    from pipeline.models import InventoryLocation
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    _loc, err = S.set_field(ab, "box", "42")
    assert err == ""
    assert InventoryLocation.objects.using(DB).filter(antibody=ab).count() == 1

    # A row a cell edit created must go when the cell is emptied — the same rule
    # the target board's report row follows, and the reason a freezer does not
    # fill with rows saying only that something is somewhere.
    _loc, err = S.set_field(ab, "box", "")
    assert err == ""
    assert not InventoryLocation.objects.using(DB).filter(antibody=ab).exists()


def test_clearing_a_box_keeps_a_freezer_somebody_recorded(seeded):
    # The other side of the rule above, and the one that would surprise: two
    # cells must not move on one edit. "It is in the −20, box not written down"
    # is true and worth keeping; removing it is `set_type`'s job.
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    S.set_type(ab, "-20")
    S.set_field(ab, "box", "42")

    _loc, err = S.set_field(ab, "box", "")
    assert err == ""
    assert S.boxes(ab) == "" and S.types(ab) == "−20°C"


def test_removing_the_temperature_under_a_box_is_refused_by_name(seeded):
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    S.set_field(ab, "box", "42")
    S.set_type(ab, "-20")

    _loc, err = S.set_type(ab, "")
    assert "box 42" in err
    assert S.types(ab) == "−20°C"           # unchanged


def test_an_antibody_with_no_site_says_which_cell_to_fill_first(seeded):
    # `InventoryLocation.site` is a required FK and 32 antibodies on live have
    # no site. "Could not save that" would name nothing; the site column is two
    # cells along.
    from pipeline.services import storage as S

    ab = _antibody(seeded, catalogue="ab000000", site=None)
    _loc, err = S.set_field(ab, "box", "42")
    assert "no site" in err.lower() and "site" in err.lower()
    assert S.boxes(ab) == ""


# ── what the board and the sheet show ────────────────────────────────────────

def test_the_board_row_and_the_sheet_agree_about_the_box(seeded):
    from pipeline.models import InventoryLocation
    from pipeline.services import antibody_board, board_columns, received
    from pipeline.services import storage as S

    ab = _antibody(seeded)
    InventoryLocation.objects.using(DB).create(
        antibody=ab, site=seeded["site"], storage_type="-20", box="42")
    ab.received_date, ab.received_precision = received.parse("Aug 2026")[:2]
    ab.save(using=DB)

    row = antibody_board.row_for(
        antibody_board.board_queryset().get(pk=ab.pk))
    assert row["box"] == "42" and row["storage"] == "−20°C"
    assert row["locations"] == 1

    # The sheet carries the round-trip spelling, the screen the readable one —
    # and the screen never prints a day the record does not have.
    assert row["received_date"] == "2026-08"
    assert row["received_label"] == "Aug 2026"
    sheet = dict(zip(board_columns.sheet_headers("antibodies"),
                     board_columns.row_values("antibodies", ab)))
    assert sheet["box"] == "42" and sheet["received"] == "2026-08"


# ── the whole way in, in uOttawa's own layout ────────────────────────────────

UOTTAWA_SHEET = (
    "Gene Name\tBox #\tAb #\tCompany\tCatalogue #\tLot #\tRRID\tClonality\t"
    "Clone ID\tHost\tConcentration (mg/mL)\tSupplier Recommendations\tStorage\t"
    "Date received\tSite\n"
    "ADAM10\t42\t\tAbcam\tab124695\t1102604-18\tAB_10972023\t"
    "Recombinant monoclonal\tEPR5622\tRabbit\t0.498\tWb, IP\t(-20°C)\t"
    "August 2026\tStorage Site\n")


def test_the_sheet_that_read_as_zero_rows_now_lands_whole(seeded):
    """One test for both halves of what uOttawa's spreadsheet found.

    44 antibodies in the lab's own layout uploaded as **0 rows**, because
    `Catalogue #` matched no alias and `parse_table` drops every row with no
    catalogue. And had they been read, `Concentration (mg/mL)` over a bare
    `0.498` would have been filed as 0.498 µg/mL — a thousandfold out, on a
    column no screen contradicts.

    `header_led=True` because this is the upload path; a paste leaves it off.
    """
    from pipeline.services import bulk_antibodies as bulk
    from pipeline.services import storage as S

    rows = bulk.parse(UOTTAWA_SHEET, header_led=True)
    assert len(rows) == 1

    out = bulk.apply(rows)
    assert len(out["created"]) == 1

    from pipeline.models import Antibody
    ab = Antibody.objects.using(DB).get(pk=out["created"][0]["id"])
    assert float(ab.concentration) == 498.0          # not 0.498
    assert ab.lot_number == "1102604-18"
    assert S.boxes(ab) == "42" and S.types(ab) == "−20°C"
    # A month is a real answer and stays a month.
    from pipeline.services import received
    assert received.of(ab) == "Aug 2026"
    assert received.cell_value(ab) == "2026-08"


def test_the_stored_number_is_never_written_in_scientific_notation(seeded):
    """`Decimal.normalize()` reaches for an exponent the moment there are
    trailing zeros, so the note read *"1 is stored as 1E+3 µg/mL"* — on the one
    number the message exists to make checkable, and on the commonest value in
    the sheet that prompted all of this: 12 of uOttawa's 42 concentrations are a
    bare `1`. Found while writing a field-test brief that would have asked a
    tester to verify the broken wording.
    """
    from pipeline.services.bulk_antibodies import _heading_unit_note

    for cell, expected in (("1", "1000"), ("0.5", "500"), ("2.4", "2400"),
                           ("0.498", "498")):
        note = _heading_unit_note(f"{cell} mg/ml", "heading")
        assert f"stored as {expected}" in note, note
        assert "E+" not in note


def test_the_preview_says_the_heading_changed_the_number(seeded):
    # A conversion the reader cannot see in the cell is one a preview owes them:
    # the cell says 0.498 and the unit is two rows up in the header.
    from pipeline.services import bulk_antibodies as bulk

    rows = bulk.parse(UOTTAWA_SHEET, header_led=True)
    note = bulk.plan(rows)[0]["note"]
    assert "mg/ml" in note.lower() and "498" in note


def test_re_uploading_the_same_sheet_changes_nothing(seeded):
    from pipeline.models import InventoryLocation
    from pipeline.services import bulk_antibodies as bulk

    rows = bulk.parse(UOTTAWA_SHEET, header_led=True)
    first = bulk.apply(rows)
    second = bulk.apply(bulk.parse(UOTTAWA_SHEET, header_led=True))
    assert len(first["created"]) == 1 and len(second["created"]) == 0
    assert len(second["updated"]) == 1
    ab_id = first["created"][0]["id"]
    assert InventoryLocation.objects.using(DB).filter(antibody_id=ab_id).count() == 1


def test_a_column_with_nowhere_to_go_is_named_not_dropped(seeded):
    """`board_columns.read_only()` was written for this and had no callers.

    uOttawa's sheet carried four columns nothing read. Two of them —
    `Reactivity (Supplier)` and `Isotype` — turned out to be real model fields
    filled on ~2,800 rows, so they are read now and must NOT appear here; that
    is the point of naming them rather than dropping them, and this test is
    where the difference is kept honest. The other two have genuinely nowhere
    to go, and saying so is how somebody finds out today rather than in a month.
    """
    from pipeline.views.imports import _ignored_note

    header = ("Gene Name\tCatalogue #\tReactivity (Supplier)\tIsotype\t"
              "Number of vials\tAb request reference\tOGA recommends (read-only)")
    note = _ignored_note("antibodies", header + "\nADAM10\tab124695\tH\tIgG\t"
                                                "1 x 100 µL\tAugust 2026\t")
    for column in ("Number of vials", "Ab request reference"):
        assert column in note
    assert "OGA recommends (read-only)" in note and "not be written back" in note
    # A column that *is* read is never accused of being ignored — including the
    # two that only became readable with this change.
    for column in ("Catalogue #", "Gene Name", "Isotype", "Reactivity (Supplier)"):
        assert column not in note


def test_the_refusal_asks_the_parser_which_columns_are_missing(seeded):
    # `Gene Name` is read. A refusal listing `gene` among the missing columns
    # sends somebody to rename a heading that was already working.
    from pipeline.views.imports import _nothing_read_refusal

    refusal = _nothing_read_refusal(
        "antibodies", "Gene Name\tCompany\tNotes\nADAM10\tAbcam\t-\n")
    assert "catalogue" in refusal
    assert "no catalogue, gene" not in refusal and "gene column" not in refusal


# ── a limited set of valid options is a picker, not a text box ───────────────

def test_the_offered_list_does_not_offer_two_spellings_of_one_answer(seeded):
    """`vocabulary.on_file` folds case and offers the spelling already most used.

    Live holds `rabbit` 2,113 times and `Rabbit` 250; `IgG1 Kappa` 30 and
    `IgG1 kappa` 11 (1 Sep 2026). A datalist built from raw distinct values
    would offer both, side by side — and a picker that offers a split is a
    picker that entrenches it, because the reader takes whichever is nearer the
    cursor. Nothing is rewritten: which spelling the column *should* carry is a
    curation question, the same one `fix_gene_case` refuses to answer alone.
    """
    from pipeline.models import Antibody
    from pipeline.services import vocabulary

    for i, host in enumerate(["rabbit"] * 3 + ["Rabbit", "Mouse", "mouse"]):
        _antibody(seeded, catalogue=f"ab90{i}", host_species=host)

    offered = vocabulary.on_file(Antibody, "host_species")
    assert offered == ["rabbit", "Mouse"], offered   # commonest spelling, commonest first
    assert "Rabbit" not in offered


def test_a_column_with_no_convention_is_left_as_a_text_box(seeded):
    # A datalist of hundreds is a scroll, not a reminder. Over the limit the
    # answer is an empty list, and the cell stays a plain box — which is the
    # honest control for free text.
    from pipeline.models import Antibody
    from pipeline.services import vocabulary

    for i in range(vocabulary.MAX_OPTIONS + 2):
        _antibody(seeded, catalogue=f"ab80{i}", lot_number=f"lot-{i}")
    assert vocabulary.on_file(Antibody, "lot_number") == []


def test_the_closed_sets_are_closed_and_the_conventions_are_not(seeded):
    from pipeline.services import antibody_board

    choices = antibody_board.cell_choices()
    for field in ("clonality", "site", "storage"):
        assert choices[field]["strict"] is True, field
    for field in ("isotype", "species_reactivity", "host_species"):
        assert not choices[field].get("strict"), field
    # Clearing a site and clearing a storage temperature are both real edits, so
    # each closed set carries a blank option — without one, opening a dropdown
    # on an empty cell and closing it writes the first value, which nobody chose.
    assert choices["site"]["values"][0]["value"] == ""
    assert choices["storage"]["values"][0]["value"] == ""
    # The clonality enum has `unknown` of its own, so a blank option there would
    # be a second way to say the same thing.
    assert [v["value"] for v in choices["clonality"]["values"]] == [
        "monoclonal", "polyclonal", "recombinant", "unknown"]


def test_isotype_and_reactivity_round_trip_through_a_sheet(seeded):
    # uOttawa's own columns. Both were dropped in silence before this; both are
    # real model fields filled on ~2,800 rows.
    from pipeline.models import Antibody
    from pipeline.services import board_columns
    from pipeline.services import bulk_antibodies as bulk

    sheet = ("Gene Name\tCatalogue #\tCompany\tIsotype\tReactivity (Supplier)\tSite\n"
             "ADAM10\tab124695\tAbcam\tIgG\tH, M, R\tStorage Site\n")
    rows = bulk.parse(sheet, header_led=True)
    out = bulk.apply(rows)
    ab = Antibody.objects.using(DB).get(pk=out["created"][0]["id"])
    assert ab.isotype == "IgG" and ab.species_reactivity == "H, M, R"

    sheet_row = dict(zip(board_columns.sheet_headers("antibodies"),
                         board_columns.row_values("antibodies", ab)))
    assert sheet_row["isotype"] == "IgG" and sheet_row["reactivity"] == "H, M, R"
