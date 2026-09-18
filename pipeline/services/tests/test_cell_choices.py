"""A limited set of valid options is a control, not a text box.

The owner's rule, applied across the boards. What is pinned here is the shape
rather than any one field's list, because the lists are read from the database
and will move:

* a **closed** set is a `<select>` *and* a refusal by the writer — a picker
  narrows what a person can send, it does not decide what is stored;
* an **open** one is a `<datalist>` and no refusal, because a vocabulary nobody
  may add to stops describing the bench;
* and the same field offers the same thing on every surface that edits it,
  which is the half that had already gone wrong: `granting_agency`, `project`,
  `site` and `essential_gene` were pickers on a gene's own page and bare text
  boxes on the targets board, through one patch endpoint.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import Site
    for code, name in (("CHO", "Chosen"), ("OTR", "Other bench")):
        Site.objects.using(DB).get_or_create(short_code=code,
                                             defaults={"name": name})
    return True


def _closed(spec):
    return spec.get("strict") is True and spec["values"]


def test_every_board_offers_the_same_sites(seeded):
    """One reader, because four boards each wrote the list out again.

    `sites.strict_id` refuses a name that is not on file, so a text box here
    could only ever offer a spelling nobody accepts — and four copies of the
    list is four chances for one of them to fall behind.
    """
    from pipeline.services import (antibody_board, cell_line_board, members,
                                   session_board, target_board)

    everywhere = [antibody_board.cell_choices()["site"],
                  cell_line_board.cell_choices()["site"],
                  session_board.cell_choices()["site"],
                  target_board.cell_choices()["site"],
                  members.cell_choices()["site"]]
    for spec in everywhere:
        assert _closed(spec)
    named = [[v["value"] for v in spec["values"] if v["value"]]
             for spec in everywhere]
    assert all(n == named[0] for n in named), named
    assert "Chosen" in named[0]


def test_a_gene_page_and_the_targets_board_offer_one_vocabulary(seeded):
    """The split this was written to close.

    Both surfaces edit these four fields through the same patch endpoint, and
    the lists were built in the gene page's template and nowhere else — so the
    board one click away handed you a blank box for the same field. Now there is
    one function and both read it, which is what makes them unable to drift.
    """
    from pathlib import Path

    from django.conf import settings

    from pipeline.services import target_board

    choices = target_board.cell_choices()
    assert set(choices) == {"site", "granting_agency", "project",
                            "essential_gene"}
    assert _closed(choices["site"])
    # Funders and projects are created on demand by the same writer the board
    # uses, so narrowing them would make a new funder unrecordable.
    for field in ("granting_agency", "project", "essential_gene"):
        assert not choices[field].get("strict"), field

    # Neither template may rebuild the list. This is a source assertion on
    # purpose and it guards one specific regression: the lists came back by
    # somebody adding a second copy to one page, which no behavioural test
    # notices until the two have already diverged. That they are *wired* is
    # what driving the boards in a browser showed.
    templates = Path(settings.BASE_DIR) / "pipeline/templates/pipeline"
    for name in ("target_board.html", "target_detail.html"):
        source = (templates / name).read_text()
        assert "cell-choices" in source, name
        for gone in ("board-agencies", "board-projects", "board-sites"):
            assert gone not in source, f"{name} builds its own {gone} list again"


def test_the_people_board_closes_both_of_its_cells(seeded):
    """Site and role on the page that decides who can reach the pipeline.

    `members.set_field` refuses an unknown role by name and `strict_id` an
    unknown site, so both were grids offering a mistake and reporting it after
    the save — on the one board where a wrong value means somebody cannot sign
    in at all.
    """
    from pipeline.models import Member
    from pipeline.services import members

    choices = members.cell_choices()
    assert _closed(choices["site"]) and _closed(choices["role"])
    assert [v["value"] for v in choices["role"]["values"]] == \
        [v for v, _ in Member.Role.choices]


def test_a_closed_set_is_refused_by_the_writer_not_only_the_picker(seeded):
    """A dropdown is half of enforcing a closed set.

    `clonality` had a picker's worth of options and **no server-side check at
    all** — the patch endpoint fell through to a bare `setattr`, so `mono` was
    stored verbatim and `clonality.label` then held a value its own vocabulary
    does not contain. The picker narrows what a person can send; a stale page,
    a script or a second tab can still send anything.
    """
    from pipeline.models import Member
    from pipeline.services import members

    with pytest.raises(members.Refused) as refusal:
        members.set_field(0, "role", "wizard")
    # It refuses before it can even find the person, so assert on the message a
    # real member would get instead.
    assert "not on file" in str(refusal.value) or "not a role" in str(refusal.value)
    assert "wizard" not in {v for v, _ in Member.Role.choices}


def test_the_cell_lines_board_offers_nothing_for_a_cell_it_does_not_draw(seeded):
    """A picker for a cell nobody draws is dead configuration, and naming it
    reads as done when it is not.

    `species`, `growth_properties` and `origin` all have exactly the vocabulary
    worth offering — 5, 4 and 11 distinct values on live — and the board drew no
    cell for any of the three. Two of them have one since 14 Sep 2026, because
    two benches asked on the same email thread within the hour: uOttawa for
    `growth_properties`, McGill for `species`. `origin` still does not.

    So the pin is **derived** rather than a list: every picker must name a
    column this board actually draws and lets you edit. Written as a list it
    would have to be edited to go on passing, which is how it would come to be
    edited without the question being asked.
    """
    from pipeline.services import board_columns, cell_line_board

    offered = set(cell_line_board.cell_choices())
    drawn = {c.key for c in board_columns.board_columns("cell-lines")}
    assert offered <= drawn, offered - drawn
    editable = cell_line_board.EDITABLE_FIELDS | {"site"}
    assert offered <= editable, offered - editable
    # The one that is still dark stays out until it has a cell. `species` left
    # this list on 14 Sep 2026, an hour after `growth_properties`, because
    # McGill asked for it on the same email thread.
    assert "origin" not in offered
    # And every entry is a `cellChoices` **entry**, not the bare list behind
    # one. `editorHtml` reads `.values`, so a list here renders a plain text box
    # and nothing anywhere says the picker did not appear — the shape is the
    # whole of whether the control exists.
    for field, entry in cell_line_board.cell_choices().items():
        assert isinstance(entry, dict), field
        assert "values" in entry and "strict" in entry, field
        assert isinstance(entry["values"], list), field


# ── run 18 findings ──────────────────────────────────────────────────────────

def test_a_role_reads_back_in_either_spelling(seeded):
    """One fact, one vocabulary — run 18 F3.

    The board's cell is a `<select>` showing *Administrator*; the Add panel's
    grid is a text box serialised to TSV, and it offered `admin`. Same five
    roles, two vocabularies, on one page — which a tester found by comparing
    the two screens. The panel offers the readable words now, so the parser has
    to read them.
    """
    from pipeline.models import Member
    from pipeline.services import members

    for code, label in Member.Role.choices:
        assert members.role_from(code) == (code, "")
        assert members.role_from(label) == (code, ""), label
        assert members.role_from(str(label).upper()) == (code, "")
    # Blank is not a refusal — the callers decide whether a role is required.
    assert members.role_from("") == ("", "")
    # And an unknown one names every spelling on offer.
    _code, refusal = members.role_from("wizard")
    assert "wizard" in refusal
    for code, label in Member.Role.choices:
        assert label in refusal and code in refusal
