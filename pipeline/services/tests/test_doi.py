"""What a DOI cell may hold — ``services/doi.py``.

The thirteenth field test typed ``definitely not a doi 12345`` into the Zenodo
cell on the target board. It saved, flipped the gene to **completed**, put a green
**Reported** badge on the gene page, and rendered as a link that — not being an
absolute address — resolved against this site and landed on a Not Found page. Then
the cell stopped being editable, because the board drew a link *instead of* an
editable cell once the field had anything in it.

Four rules are pinned here. Three are about refusing; the fourth is the one that
is easy to lose in a later widening:

  * a value that is neither a DOI nor an address is refused **by name**, with what
    is accepted — the ``services/concentration.py`` rule;
  * blank is never a refusal, because on this field blank is how a typo is undone;
  * a stored value that is not absolute is never given an ``href``, since a
    relative one works, goes nowhere, and reads as the record being missing;
  * **what the board prints is a value the parser accepts** — the cell shows
    ``10.5281/zenodo.1`` for a doi.org URL, and retyping exactly that has to come
    back to the same stored address. ``cell_lines.wild_type_options`` learned this
    one board over: offering the app's own rendering to a parser that refuses it
    is how a cell comes to reject its own contents.

No database: this is a pure reader. The endpoint's half is in
``pipeline/tests_board_patch.py``.
"""
from __future__ import annotations

import pytest

from pipeline.services import doi


@pytest.mark.parametrize("typed,stored", [
    ("10.5281/zenodo.16812915", "https://doi.org/10.5281/zenodo.16812915"),
    ("doi:10.5281/zenodo.1", "https://doi.org/10.5281/zenodo.1"),
    ("DOI: 10.12688/f1000research.1.2", "https://doi.org/10.12688/f1000research.1.2"),
    ("https://doi.org/10.5281/zenodo.1", "https://doi.org/10.5281/zenodo.1"),
    ("http://dx.doi.org/10.5281/zenodo.1", "http://dx.doi.org/10.5281/zenodo.1"),
    ("https://zenodo.org/records/16812915", "https://zenodo.org/records/16812915"),
    # A browser hides the scheme, so this is what a paste from the address bar
    # looks like. Stored absolute, or it would be drawn as a relative link.
    ("zenodo.org/records/16812915", "https://zenodo.org/records/16812915"),
    ("  10.5281/zenodo.1  ", "https://doi.org/10.5281/zenodo.1"),
])
def test_what_it_takes_is_stored_as_an_address(typed, stored):
    value, error = doi.parse(typed, field="Zenodo DOI")
    assert error == ""
    assert value == stored


@pytest.mark.parametrize("typed", [
    "definitely not a doi 12345",   # the field test's own value
    "coming",                       # what Carl's F1000 column says, legitimately
    "publish elsewhere",
    "MMP7",
    "10.5281 /zenodo.1",            # a DOI with a space in it is not a DOI
    "https://",
    "12345",
])
def test_what_it_refuses_says_so_by_name_and_says_what_is_accepted(typed):
    value, error = doi.parse(typed, field="Zenodo DOI")
    assert value == ""
    # Names what was typed, names the field, and carries an example — "that is
    # wrong" without "here is what is right" is half a message.
    assert typed in error
    assert "Zenodo DOI" in error
    assert doi.EXAMPLE in error


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_is_not_a_refusal(blank):
    """Blank is how a wrong value comes back out. Refusing it would leave the
    typo in place, which is the whole finding."""
    assert doi.parse(blank) == ("", "")


@pytest.mark.parametrize("stored", [
    "definitely not a doi 12345",   # what the board wrote before this existed
    "10.5281/zenodo.1",             # bare: Carl's workbook stored these unchanged
    "coming",
])
def test_a_value_that_is_not_an_address_gets_no_href(stored):
    """The harmful half. A relative href is resolved against this site, so the
    link *works*, goes nowhere useful, and answers with a Not Found page that
    reads as the record having been lost."""
    assert doi.link(stored) == ""


def test_an_absolute_address_is_its_own_href():
    assert doi.link("https://doi.org/10.5281/zenodo.1") == "https://doi.org/10.5281/zenodo.1"
    assert doi.link("  http://zenodo.org/records/1  ") == "http://zenodo.org/records/1"


@pytest.mark.parametrize("stored,shown", [
    ("https://doi.org/10.5281/zenodo.16812915", "10.5281/zenodo.16812915"),
    ("https://zenodo.org/records/1", "zenodo.org/records/1"),
    # http keeps its scheme: dropping it would make the printed value parse back
    # to a *different* (https) address, which is the round trip below breaking.
    ("http://zenodo.org/records/1", "http://zenodo.org/records/1"),
])
def test_a_cell_prints_the_doi_not_the_doi_org_url(stored, shown):
    assert doi.display(stored) == shown


@pytest.mark.parametrize("typed", [
    "10.5281/zenodo.16812915",
    "https://zenodo.org/records/1",
    "http://zenodo.org/records/1",
    "zenodo.org/records/1",
])
def test_what_the_cell_prints_is_a_value_the_parser_accepts(typed):
    """The rule that survives a later widening of `display`.

    The board draws an editable cell holding `display(stored)`. Whatever a person
    does with that cell — retype it, copy it into a spreadsheet, paste it into
    another row — has to arrive back at the same stored address, or the app is
    refusing its own output.
    """
    stored, _ = doi.parse(typed)
    again, error = doi.parse(doi.display(stored))
    assert error == ""
    assert again == stored
