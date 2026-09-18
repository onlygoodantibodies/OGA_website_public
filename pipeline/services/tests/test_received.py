"""A received date says only as much as somebody actually knows.

uOttawa's records say ``August 2026``. That is a real answer — an order
confirmation carries a month where a packing slip carries a day — and the two
ways of holding it that a ``DateField`` invites are both wrong: store the 1st
and every screen publishes a day nobody wrote down, or keep a second free-text
column and the two disagree the moment one is edited.

What is pinned here is the pair behaving as one fact: the precision travels with
the date, nothing prints past it, and the sheet spelling reads back as what it
was. Pure functions, no database — the cheapest test that can fail.
"""
from __future__ import annotations

from datetime import date

from pipeline.services import received as R


def test_a_day_a_month_and_a_year_are_three_answers():
    assert R.parse("2026-08-14") == (date(2026, 8, 14), R.DAY, "")
    assert R.parse("Aug 2026") == (date(2026, 8, 1), R.MONTH, "")
    assert R.parse("August 2026") == (date(2026, 8, 1), R.MONTH, "")
    assert R.parse("2026-08") == (date(2026, 8, 1), R.MONTH, "")
    assert R.parse("2026") == (date(2026, 1, 1), R.YEAR, "")


def test_a_month_never_prints_a_day():
    # The whole reason the precision column exists. The stored date IS the 1st;
    # printing it is what would turn storage into a claim.
    when, precision, _ = R.parse("August 2026")
    assert R.label(when, precision) == "Aug 2026"
    assert "1" not in R.label(when, precision).split(" ")[0]
    assert R.label(*R.parse("2026")[:2]) == "2026"
    assert R.label(*R.parse("2026-08-14")[:2]) == "14 Aug 2026"


def test_the_sheet_spelling_reads_back_as_what_it_was():
    # download → edit → upload must not promote a month to a day. `stored` is
    # what the column carries and `parse` is what reads it, so this is the round
    # trip in one line per precision.
    for text in ("2026-08-14", "2026-08", "2026"):
        when, precision, err = R.parse(text)
        assert err == ""
        assert R.stored(when, precision) == text


def test_an_ambiguous_slashed_date_is_refused_rather_than_guessed():
    # `01/02/2026` is 1 February to the benches that wrote this and 2 January to
    # a third, and nothing in the cell settles it. A wrong arrival date is the
    # quiet kind of wrong — it ends up in a methods section.
    when, precision, err = R.parse("01/02/2026")
    assert when is None and precision == ""
    assert "1 Feb" in err and "2 Jan" in err


def test_an_unambiguous_slashed_date_is_read_day_first():
    assert R.parse("14/08/2026")[:2] == (date(2026, 8, 14), R.DAY)


def test_a_spreadsheet_date_cell_arrives_as_a_datetime_and_is_still_a_date():
    # openpyxl hands back a `datetime` for any date-formatted cell and
    # `views/imports.py::_file_to_text` calls `str()` on it. Refusing that shape
    # would refuse the ordinary spreadsheet.
    assert R.parse("2026-08-14 00:00:00")[:2] == (date(2026, 8, 14), R.DAY)


def test_blank_is_not_a_refusal():
    assert R.parse("") == (None, "", "")
    assert R.parse(None) == (None, "", "")
    assert R.label(None, R.DAY) == "" and R.stored(None, R.DAY) == ""


def test_something_that_is_not_a_date_says_what_is_accepted():
    _when, _precision, err = R.parse("Ab request reference")
    assert "Ab request reference" in err and "Aug 2026" in err


def test_a_year_outside_living_memory_is_refused():
    # A column of another kind pasted into this one — a lot number, a catalogue
    # — must not file a reagent as having arrived in 1998.
    assert R.parse("1066")[2] != ""
    assert R.parse("2026")[2] == ""
