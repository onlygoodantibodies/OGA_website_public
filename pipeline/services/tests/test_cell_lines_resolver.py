"""The one cell-line resolver — `services/cell_lines.py`.

Everything here is a defect the seventh field test found, pinned so the five
disagreeing resolvers that produced them cannot come back one at a time. The
worst of them wrote **another institution's knockout into a session's wild-type
slot**, silently, and it reached a generated Data Note relabelled as this gene's
knockout.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture
def lines(_pipeline_db):
    from pipeline.models import CellLine, Site, Target
    CellLine.objects.using(DB).all().delete()
    Target.objects.using(DB).filter(gene_name__in=["STMN2", "PRKN", "ELP3"]).delete()

    lei, _ = Site.objects.using(DB).get_or_create(
        short_code="LEI", defaults={"name": "Leicester"})
    mcg, _ = Site.objects.using(DB).get_or_create(
        short_code="MCG", defaults={"name": "McGill"})

    stmn2 = Target.objects.using(DB).create(gene_name="STMN2", protein_name="Stathmin-2")
    prkn = Target.objects.using(DB).create(gene_name="PRKN", protein_name="Parkin")

    # The shape that did the damage: five rows answering to `SH-SY5Y`. A wild
    # type carries NO gene — that is the rule the readers kept breaking.
    wt_lei = CellLine.objects.using(DB).create(name="SH-SY5Y", genotype="WT", site=lei)
    wt_mcg = CellLine.objects.using(DB).create(name="SH-SY5Y", genotype="WT", site=mcg)
    ko_prkn = CellLine.objects.using(DB).create(
        name="SH-SY5Y", genotype="KO", site=mcg, target=prkn)
    ko_stmn2 = CellLine.objects.using(DB).create(
        name="SH-SY5Y STMN2 KO", genotype="KO", site=lei, target=stmn2,
        parent_line=wt_lei)
    return {"lei": lei, "mcg": mcg, "stmn2": stmn2,
            "wt_lei": wt_lei, "wt_mcg": wt_mcg,
            "ko_prkn": ko_prkn, "ko_stmn2": ko_stmn2}


def test_a_wild_type_slot_never_resolves_to_a_knockout(lines):
    """The run-7 defect itself: `SH-SY5Y` in a WT box gave McGill's PRKN KO."""
    from pipeline.services import cell_lines as clines
    line, err = clines.resolve_wt("SH-SY5Y", site_id=lines["lei"].pk)
    assert line is not None and err is None
    assert line.pk == lines["wt_lei"].pk
    assert line.genotype == "WT"


def test_a_shared_name_across_sites_is_a_question_not_a_coin_toss(lines):
    """No site preference to fall back on → both are named and nothing is chosen."""
    from pipeline.services import cell_lines as clines
    line, err = clines.resolve_wt("SH-SY5Y")
    assert line is None
    assert "matches 2 cell lines" in err
    assert "Leicester" in err and "McGill" in err


def test_the_apps_own_rendering_resolves_back(lines):
    """`SH-SY5Y — Leicester` is what the board prints, so it is what gets retyped.

    It used to be refused outright — *"no cell line called 'SH-SY5Y — Leicester'"*
    — while the bare name it would accept silently re-resolved to the wrong row.
    """
    from pipeline.services import cell_lines as clines
    for typed in ("SH-SY5Y — Leicester", "SH-SY5Y - Leicester"):
        line, err = clines.resolve_wt(typed)
        assert err is None, typed
        assert line.pk == lines["wt_lei"].pk, typed


def test_a_label_round_trips_to_the_row_it_was_rendered_from(lines):
    """Every caller says which slot it is filling, and with that a label is exact.

    The genotype is not optional decoration here: `SH-SY5Y — McGill` is the
    McGill *wild type's* label, and McGill also has a knockout stored under the
    bare name `SH-SY5Y`, so the string alone genuinely names two rows. Each
    caller knows which kind it wants — a WT cell, a KO cell, a workbook's
    `cell_line_wt` column — and passes it.
    """
    from pipeline.services import cell_lines as clines
    for row in (lines["wt_lei"], lines["wt_mcg"], lines["ko_prkn"], lines["ko_stmn2"]):
        found, err = clines.resolve(clines.label(row), genotype=row.genotype)
        assert err is None, clines.label(row)
        assert found.pk == row.pk, clines.label(row)


def test_one_site_holding_a_wt_and_a_ko_of_one_name_is_refused_not_guessed(lines):
    """The bare name is all McGill's WT and McGill's PRKN KO have in common.

    Asked without a genotype, that is two answers and the honest reply is to say
    so. `.first()` under `Meta.ordering = ['name']` is what used to happen, and
    which row won was arbitrary.
    """
    from pipeline.services import cell_lines as clines
    line, err = clines.resolve("SH-SY5Y — McGill")
    assert line is None
    assert "SH-SY5Y — McGill" in err and "SH-SY5Y PRKN KO — McGill" in err


def test_a_knockout_is_labelled_by_its_own_gene_once(lines):
    """`SH-SY5Y` + PRKN reads `SH-SY5Y PRKN KO`, not `SH-SY5Y KO PRKN KO`."""
    from pipeline.services import cell_lines as clines
    assert clines.label(lines["ko_prkn"]) == "SH-SY5Y PRKN KO — McGill"
    # A name that already names its gene is left alone.
    assert clines.label(lines["ko_stmn2"]) == "SH-SY5Y STMN2 KO — Leicester"


def test_a_bare_ko_suffix_is_not_doubled(_pipeline_db):
    from pipeline.models import CellLine, Site, Target
    from pipeline.services import cell_lines as clines
    site, _ = Site.objects.using(DB).get_or_create(
        short_code="LEI", defaults={"name": "Leicester"})
    t = Target.objects.using(DB).create(gene_name="SOD1", protein_name="SOD1")
    row = CellLine.objects.using(DB).create(
        name="SW620 KO", genotype="KO", target=t, site=site)
    try:
        assert clines.label(row) == "SW620 SOD1 KO — Leicester"
        # …and that label finds the row again, which is the point of the decoration.
        found, err = clines.resolve("SW620 SOD1 KO — Leicester", genotype="KO")
        assert err is None and found.pk == row.pk
    finally:
        row.delete()
        t.delete()


def test_the_wrong_kind_is_refused_by_naming_the_right_ones(lines):
    """A refusal that says only 'not found' is half a message."""
    from pipeline.services import cell_lines as clines
    line, err = clines.resolve("SH-SY5Y STMN2 KO", genotype="WT",
                               site_id=lines["lei"].pk)
    assert line is None
    assert "not as a wild-type line" in err
    assert "SH-SY5Y STMN2 KO" in err


def test_an_unknown_name_lists_what_is_on_file(lines):
    from pipeline.services import cell_lines as clines
    line, err = clines.resolve_wt("NOTALINE-R7", site_id=lines["lei"].pk)
    assert line is None
    assert "no wild-type cell line called 'NOTALINE-R7'" in err
    assert "SH-SY5Y" in err


def test_the_resolver_never_creates(lines):
    from pipeline.models import CellLine
    from pipeline.services import cell_lines as clines
    before = CellLine.objects.using(DB).count()
    clines.resolve_wt("NOTALINE-R7", site_id=lines["lei"].pk)
    clines.resolve("SH-SY5Y")
    assert CellLine.objects.using(DB).count() == before


def test_a_blank_is_not_an_error(lines):
    from pipeline.services import cell_lines as clines
    for blank in ("", "   ", None):
        line, err = clines.resolve(blank, genotype="WT")
        assert line is None and err is None


# ── the wild types a site can pick from ──────────────────────────────────
#
# The gene page printed these in prose and asked you to retype one into a
# knockout's `parent` cell. What goes in that cell is the parser's business, so
# the picker's values come from here rather than from a template.

def test_a_sites_wild_types_come_back_as_the_parser_takes_them(lines):
    """`resolve_parent` matches a bare name or a C-number and never `label()`'s
    "HAP1 — Leicester". A picker offering the app's own display string would be
    refused by the app's own parser — the mistake this module already records
    the sessions board making, one column over."""
    from pipeline.services import cell_lines
    out = cell_lines.wild_type_options(site_id=lines["lei"].pk)
    assert [o["value"] for o in out] == ["SH-SY5Y"]
    assert all("—" not in o["value"] for o in out)
    # And it is only this site's: McGill's identically-named WT is not offered.
    assert len(out) == 1


def test_only_wild_types_are_offered(lines):
    """A parent is by definition the wild type a knockout was made from, and
    Leicester's own `SH-SY5Y STMN2 KO` sits right beside it."""
    from pipeline.services import cell_lines
    out = cell_lines.wild_type_options(site_id=lines["lei"].pk)
    assert "SH-SY5Y STMN2 KO" not in [o["value"] for o in out]


def test_two_of_your_own_lines_sharing_a_name_are_offered_by_c_number(lines):
    """A name that matches several is a question, not a coin toss — the rule the
    resolver holds, applied to the list you pick from. Offering `HAP1` twice
    would make the picker the coin toss."""
    from pipeline.models import CellLine, CellLineVial
    from pipeline.services import cell_lines, lab_numbers
    lei = lines["lei"]
    a = CellLine.objects.using(DB).create(name="HAP1", genotype="WT", site=lei,
                                          c_number=48)
    # The freeze-down batch answers where the line itself does not — 102 of the
    # 145 C-numbered parents on file are a vial's number. That shape is Access
    # era: a line created now is given its own number when it is saved
    # (`services/lab_numbers.py`), so reproducing it means saying so.
    with lab_numbers.suspended():
        b = CellLine.objects.using(DB).create(name="HAP1", genotype="WT", site=lei)
    CellLineVial.objects.using(DB).create(cell_line=b, c_number=631)

    out = {o["value"]: o for o in cell_lines.wild_type_options(site_id=lei.pk)}
    assert "C-48" in out and "C-631" in out
    assert "HAP1" not in out, "an ambiguous name would pick one of the two"
    assert "HAP1" in out["C-48"]["hint"]
    a.delete(); b.delete()


def test_an_unshared_name_keeps_its_name_and_shows_its_c_number(lines):
    """The name is what a person recognises, so it stays the value; the number
    is the hint, because a site whose convention is C-numbers can then type it."""
    from pipeline.models import CellLine
    from pipeline.services import cell_lines
    hap1 = CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                             site=lines["lei"], c_number=48)
    out = {o["value"]: o for o in cell_lines.wild_type_options(site_id=lines["lei"].pk)}
    assert out["HAP1"]["hint"] == "C-48"
    hap1.delete()


def test_no_site_offers_nothing_rather_than_the_consortiums(lines):
    """`known_labels` falls back to every site's lines when yours has none,
    which is right for a refusal listing what you could have meant and wrong for
    a picker: it would offer another institution's parental as a default."""
    from pipeline.services import cell_lines
    assert cell_lines.wild_type_options(site_id=None) == []


# ── the picker the Plan a session panel offers ───────────────────────────────
#
# Its WT and KO boxes are free text on a surface whose rule is that anything new
# is created with the session, so a typo does not fail — it mints a
# near-duplicate line and controls the session against it.

def test_every_offered_value_resolves_to_the_line_it_came_from(lines):
    """**The offered value is what the parser accepts, never what the app
    displays** — the rule `wild_type_options` states for `bulk_cell_lines`, which
    does *not* read `label()`. Here the parser is `resolve`, which does, so the
    check is the round trip rather than the format."""
    from pipeline.services import cell_lines
    lei = lines["lei"].pk
    for genotype in ("WT", "KO"):
        offered = cell_lines.picker_options(genotype=genotype, site_id=lei)
        assert offered, f"Leicester has {genotype} lines and none were offered"
        for option in offered:
            line, err = cell_lines.resolve(option["value"], genotype=genotype,
                                           site_id=lei)
            assert line is not None, f"{option['value']!r} was offered and refused: {err}"
            assert line.site_id == lei


def test_a_knockout_is_offered_under_its_gene(lines):
    """`SH-SY5Y STMN2 KO`, not a second `SH-SY5Y` — two genes' knockouts of one
    parental are otherwise the same word twice."""
    from pipeline.services import cell_lines
    offered = [o["value"] for o in
               cell_lines.picker_options(genotype="KO", site_id=lines["lei"].pk)]
    assert any("STMN2" in v for v in offered)


def test_the_wild_type_box_is_never_offered_a_knockout(lines):
    """The slot says which genotype it wants — the rule that stopped a Leicester
    STMN2 session being controlled against McGill's PRKN knockout."""
    from pipeline.services import cell_lines
    offered = [o["value"] for o in
               cell_lines.picker_options(genotype="WT", site_id=lines["lei"].pk)]
    assert not any("KO" in v for v in offered)


def test_another_sites_line_is_not_offered(lines):
    """McGill's `SH-SY5Y` is still reachable by naming it after the dash; it is
    not something Leicester should be able to pick by accident."""
    from pipeline.services import cell_lines
    offered = [o["value"] for o in
               cell_lines.picker_options(genotype="WT", site_id=lines["lei"].pk)]
    assert not any("McGill" in v for v in offered)


def test_two_of_your_own_lines_with_one_label_are_offered_by_c_number(lines):
    """Same rule as `wild_type_options`, and it bites here too: the label carries
    the site, so two of *your* HAP1 wild types render identically and offering
    that string would hand the parser a value it refuses as ambiguous."""
    from pipeline.models import CellLine, CellLineVial
    from pipeline.services import cell_lines, lab_numbers
    lei = lines["lei"]
    a = CellLine.objects.using(DB).create(name="HAP1", genotype="WT", site=lei,
                                          c_number=48)
    # Access-era shape: the number lives on the vial. See the sibling test.
    with lab_numbers.suspended():
        b = CellLine.objects.using(DB).create(name="HAP1", genotype="WT", site=lei)
    CellLineVial.objects.using(DB).create(cell_line=b, c_number=631)
    try:
        offered = {o["value"]: o for o in
                   cell_lines.picker_options(genotype="WT", site_id=lei.pk)}
        assert "C-48" in offered and "C-631" in offered
        assert f"HAP1 — {lei.name}" not in offered
        assert offered["C-48"]["hint"] == f"HAP1 — {lei.name}"
    finally:
        a.delete(); b.delete()


def test_no_site_offers_nothing(lines):
    """Same reason `wild_type_options` refuses: a consortium-wide list makes
    another institution's line the easiest thing to pick."""
    from pipeline.services import cell_lines
    assert cell_lines.picker_options(genotype="WT", site_id=None) == []


def test_a_refusal_between_two_identical_labels_names_something_typable(lines):
    """**"Name the one you mean" is not an answer when both are spelled the
    same.**

    The eleventh field test typed `U2OS TRPA1 KO A1` and was refused with
    *"matches 2 cell lines: U2OS TRPA1 KO A1 — Leicester, U2OS TRPA1 KO A1 —
    Leicester. Name the one you mean — the site after the dash is part of the
    name."* Two options, spelled identically, and a closing sentence pointing at
    the part that was the same. There was no way to follow it.

    A shared label falls back to a C-number — the rule `picker_options` above
    already holds — and the value it offers has to be one the resolver takes,
    which is the other half of that rule and the reason this asserts by
    resolving rather than by reading the sentence.
    """
    from pipeline.models import CellLine, CellLineVial
    from pipeline.services import cell_lines, lab_numbers
    lei = lines["lei"]
    # Numbers issued by hand, not by the `pre_save` signal, so the two branches
    # this exercises stay the ones being tested: `a` carries the line's own
    # number and `b` only a freeze-down batch's — which is the commoner shape,
    # 102 of the 145 C-numbered parents on file.
    with lab_numbers.suspended():
        a = CellLine.objects.using(DB).create(name="U2OS TRPA1 KO A1",
                                              genotype="KO", site=lei,
                                              c_number=1201)
        b = CellLine.objects.using(DB).create(name="U2OS TRPA1 KO A1",
                                              genotype="KO", site=lei)
        CellLineVial.objects.using(DB).create(cell_line=b, c_number=1301)
    try:
        line, err = cell_lines.resolve("U2OS TRPA1 KO A1", genotype="KO",
                                       site_id=lei.pk)
        assert line is None and err
        assert "C-1201" in err and "C-1301" in err
        assert "the site after the dash" not in err, (
            "the site is what they share — that advice cannot be followed")

        # The whole point: what the message offers, the resolver accepts.
        for number, expected in (("C-1201", a.pk), ("C-1301", b.pk)):
            got, e = cell_lines.resolve(number, genotype="KO", site_id=lei.pk)
            assert e is None and got.pk == expected, f"{number} → {e}"
    finally:
        a.delete(); b.delete()


def test_a_row_with_no_number_anywhere_is_named_by_its_id(lines):
    """The fallback, because a C-number is a convention and not every row has
    one. `candidates` reads a bare number as a pk, so this is typable too.

    New rows are issued a number on `pre_save` now, so this suspends that — but
    the case is not hypothetical: only 43 of the 564 lines on file carry a
    `CellLine.c_number`, and `lab_numbers.withhold` deliberately leaves the cell
    empty when somebody typed a batch label nothing could read.
    """
    from pipeline.models import CellLine
    from pipeline.services import cell_lines, lab_numbers
    lei = lines["lei"]
    with lab_numbers.suspended():
        a = CellLine.objects.using(DB).create(name="U2OS ELP3 KO", genotype="KO",
                                              site=lei)
        b = CellLine.objects.using(DB).create(name="U2OS ELP3 KO", genotype="KO",
                                              site=lei)
    try:
        _line, err = cell_lines.resolve("U2OS ELP3 KO", genotype="KO",
                                        site_id=lei.pk)
        assert str(a.pk) in err and str(b.pk) in err
        got, e = cell_lines.resolve(str(b.pk), genotype="KO", site_id=lei.pk)
        assert e is None and got.pk == b.pk
    finally:
        a.delete(); b.delete()


def test_distinct_labels_keep_the_message_they_had(lines):
    """The ordinary case is untouched: when the labels really do tell the rows
    apart, the site after the dash is the right thing to point at."""
    from pipeline.services import cell_lines
    _line, err = cell_lines.resolve("SH-SY5Y", genotype="WT")
    assert err and "the site after the dash" in err
    assert "type C-" not in err


# ─────────────────────────────────────────────────────────────
# `session_options` — the two boxes on the step-by-step session form
# ─────────────────────────────────────────────────────────────

def test_the_ko_box_never_offers_a_line_that_is_not_a_knockout(lines):
    """The twelfth field test's finding, and the reason this function exists.

    A fresh TRPA1 with no cell lines of its own offered exactly one KO option —
    a McGill line of genotype `other` with no gene at all — because the query
    admitted `Q(genotype='other', target__isnull=True)` and the page then put
    anything of genotype `other` in *both* dropdowns. A dropdown with one
    option reads as the answer.
    """
    from pipeline.models import CellLine, Target
    from pipeline.services import cell_lines

    trpa1 = Target.objects.using(DB).create(gene_name="TRPA1", protein_name="TRPA1")
    stray = CellLine.objects.using(DB).create(
        name="CellLine-134", genotype="other", site=lines["mcg"])
    try:
        opts = cell_lines.session_options(trpa1)
        assert [o["id"] for o in opts["ko"]] == []
        assert stray.pk not in [o["id"] for o in opts["wt"]]
    finally:
        stray.delete()
        trpa1.delete()


def test_an_empty_ko_box_says_so_and_names_the_gene(lines):
    """A picker holding only its own placeholder says nothing about which of
    "none exist" and "this is broken" is true."""
    from pipeline.models import Target
    from pipeline.services import cell_lines

    trpa1 = Target.objects.using(DB).create(gene_name="TRPA1", protein_name="TRPA1")
    try:
        opts = cell_lines.session_options(trpa1)
        assert "No knockout line on file for TRPA1" in opts["ko_note"]
        assert "cell lines board" in opts["ko_note"]
    finally:
        trpa1.delete()


def test_the_ko_box_never_offers_another_genes_knockout(lines):
    """The half the old query had right, kept right."""
    from pipeline.services import cell_lines
    opts = cell_lines.session_options(lines["stmn2"])
    ids = [o["id"] for o in opts["ko"]]
    assert ids == [lines["ko_stmn2"].pk]
    assert lines["ko_prkn"].pk not in ids
    assert not opts["ko_note"]


def test_the_wt_box_still_finds_the_parentals_that_have_no_gene(lines):
    """**A wild type has no gene**, so `target__isnull` is the normal case in
    the one query in this file that is allowed to mention a target at all. Get
    this wrong and every session on a fresh gene loses its control."""
    from pipeline.services import cell_lines
    opts = cell_lines.session_options(lines["stmn2"])
    ids = [o["id"] for o in opts["wt"]]
    assert lines["wt_lei"].pk in ids and lines["wt_mcg"].pk in ids
    assert lines["ko_stmn2"].pk not in ids
    assert not opts["wt_note"]


def test_an_option_is_labelled_the_way_the_board_prints_it(lines):
    """Two labs both call their parental SH-SY5Y, so the site is part of the
    label — and a knockout carries the gene it is a knockout of."""
    from pipeline.services import cell_lines
    opts = cell_lines.session_options(lines["stmn2"])
    wt = {o["id"]: o["label"] for o in opts["wt"]}
    assert wt[lines["wt_lei"].pk].endswith("Leicester")
    assert wt[lines["wt_mcg"].pk].endswith("McGill")
    assert "STMN2 KO" in opts["ko"][0]["label"]


def test_a_freeze_down_number_is_shown_before_the_site(lines):
    """The lab identifies a line by its C-number, and 102 of the 145 C-numbered
    parents on file carry it on a vial rather than on the line. The site is the
    last thing said about a line everywhere else, so a number after it would
    read as belonging to the site.

    **Both numbers, not just the vial's.** This asserted `[C-77]` alone, which
    is what run 17 met: the line's own number — `C-1` here, issued by
    `lab_numbers` when the fixture saved it — was the one printed on the line's
    record and the one the board can find, and the picker was the one place it
    could not be read. A tube carries whichever of the two was written on it.
    """
    from pipeline.models import CellLineVial
    from pipeline.services import cell_lines
    vial = CellLineVial.objects.using(DB).create(
        cell_line=lines["wt_lei"], c_number=77)
    try:
        opts = cell_lines.session_options(lines["stmn2"])
        label = next(o["label"] for o in opts["wt"] if o["id"] == lines["wt_lei"].pk)
        own = lines["wt_lei"].c_number
        assert label == f"SH-SY5Y [C-{own}, C-77] — Leicester"
    finally:
        vial.delete()
