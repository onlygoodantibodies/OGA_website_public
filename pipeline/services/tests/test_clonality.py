"""What `services/clonality.py` must never get wrong.

The defect this module exists for is silent by construction: a screen printed
`Monoclonal` over a recombinant and no message contradicted it. So the tests
pin the two site conventions against each other — a Leicester-shaped row and a
McGill-shaped row describing the same reagent must answer alike — and pin that
the filters return the rows their labels name, since a filter that quietly drops
half the matches is the same failure wearing a different hat.
"""

import pytest

from pipeline.services import clonality


class Row:
    """The two columns, without a database. `label`/`is_recombinant` read
    nothing else, and the point of the tests is the pair."""

    def __init__(self, clonality="unknown", is_recombinant=False):
        self.clonality = clonality
        self.is_recombinant = is_recombinant


# --- The two conventions, on the same reagent -------------------------------
# ab124807 clone EPR7154(B) is stocked at both sites. Leicester holds it as
# monoclonal with the tick on; McGill as the clonality itself. Both are right.

LEICESTER = Row("monoclonal", True)
MCGILL = Row("recombinant", False)


def test_both_conventions_are_recombinant():
    assert clonality.is_recombinant(LEICESTER)
    assert clonality.is_recombinant(MCGILL)


def test_neither_column_alone_would_have_answered():
    """Why the pair, and not whichever column looks tidier."""
    assert LEICESTER.clonality != clonality.RECOMBINANT      # enum alone: misses Leicester
    assert MCGILL.is_recombinant is False                     # flag alone: misses McGill


def test_leicester_keeps_the_half_mcgill_lost():
    """Leicester's pair says which kind of recombinant; McGill's enum cannot.

    This is why the fix derives rather than normalising one convention onto the
    other — writing `recombinant` over Leicester's rows would throw away the
    only record that they are monoclonal.
    """
    assert clonality.label(LEICESTER) == "Recombinant monoclonal"
    assert clonality.label(MCGILL) == "Recombinant"


@pytest.mark.parametrize("stored,flag,expected", [
    ("monoclonal", True, "Recombinant monoclonal"),
    ("polyclonal", True, "Recombinant polyclonal"),
    ("recombinant", True, "Recombinant"),
    ("recombinant", False, "Recombinant"),
    ("unknown", True, "Recombinant"),      # 13 live Leicester rows are this
    ("monoclonal", False, "Monoclonal"),
    ("polyclonal", False, "Polyclonal"),
    ("unknown", False, "Unknown"),
    ("", False, "Unknown"),
])
def test_label_of_every_pair(stored, flag, expected):
    assert clonality.label_of(stored, flag) == expected
    assert clonality.label(Row(stored, flag)) == expected


def test_every_label_is_offerable():
    """A label the picker cannot offer is a row nobody can filter to."""
    for stored in ("monoclonal", "polyclonal", "recombinant", "unknown", ""):
        for flag in (True, False):
            assert clonality.label_of(stored, flag) in clonality.LABELS


# --- Filters ---------------------------------------------------------------
# Against the isolated SQLite pipeline_db from conftest, since the question is
# what the *database* returns: `label_q` and `recombinant_q` are ORM Q objects
# and `~Q` over an OR is exactly the shape that quietly returns the wrong set.

DB = "pipeline_db"

PAIRS = [(stored, flag)
         for stored in ("monoclonal", "polyclonal", "recombinant", "unknown")
         for flag in (True, False)] + [("", False)]


@pytest.fixture()
def seeded(_pipeline_db):
    """One antibody per (clonality, is_recombinant) pair, plus a blank enum."""
    from pipeline.models import Antibody, Target

    Antibody.objects.using(DB).all().delete()
    Target.objects.using(DB).all().delete()
    target = Target.objects.using(DB).create(gene_name="CLONTEST")
    for i, (stored, flag) in enumerate(PAIRS):
        Antibody.objects.using(DB).create(
            target_id=target.pk, catalogue_number=f"CLON{i}",
            clonality=stored, is_recombinant=flag)
    return list(Antibody.objects.using(DB).all())


def test_filters_return_exactly_the_rows_their_label_names(seeded):
    """Every label's filter returns the rows that print that label — no more,
    and (the silent half) no fewer."""
    from pipeline.models import Antibody

    for text in clonality.LABELS:
        expected = {ab.pk for ab in seeded if clonality.label(ab) == text}
        got = set(Antibody.objects.using(DB)
                  .filter(clonality.label_q(text)).values_list("pk", flat=True))
        assert got == expected, text


def test_recombinant_filter_spans_both_conventions(seeded):
    from pipeline.models import Antibody

    expected = {ab.pk for ab in seeded if clonality.is_recombinant(ab)}
    got = set(Antibody.objects.using(DB)
              .filter(clonality.recombinant_q(True)).values_list("pk", flat=True))
    assert got == expected
    # Both McGill's convention and Leicester's are in there.
    assert len(expected) > len([p for p in PAIRS if p[0] == "recombinant"])
    # And the complement is the complement.
    not_got = set(Antibody.objects.using(DB)
                  .filter(clonality.recombinant_q(False)).values_list("pk", flat=True))
    assert not_got == {ab.pk for ab in seeded} - expected


def test_a_bookmarked_enum_value_still_filters(seeded):
    """`?clonality=monoclonal` is in people's bookmarks and asks the stored
    column. It must keep meaning that, not stop matching."""
    from pipeline.models import Antibody

    got = set(Antibody.objects.using(DB)
              .filter(clonality.label_q("monoclonal")).values_list("pk", flat=True))
    expected = set(Antibody.objects.using(DB)
                   .filter(clonality="monoclonal").values_list("pk", flat=True))
    assert got == expected and got


def test_options_are_only_labels_the_rows_hold(seeded):
    from pipeline.models import Antibody

    options = clonality.options_for(Antibody.objects.using(DB).all())
    held = {clonality.label(ab) for ab in seeded}
    assert set(options) == held
    assert options == [t for t in clonality.LABELS if t in held]   # LABELS order
