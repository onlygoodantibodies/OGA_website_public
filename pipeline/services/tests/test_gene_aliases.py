"""Resolving a gene by an older name — `services/targets.py::match_gene`.

The cropper asked ``gene_name__iexact`` and nothing else, so a symbol that has
since been renamed came back as a gene the pipeline had never seen and the tool
offered to create it. The owner caught it by hand: *"I nearly added a new gene
that was an old gene."* Doing that from the figure cropper is the expensive
version — the antibodies and the published figures for one gene end up split
across two target rows that nothing joins, and the public site is derived from
the figure relation, so half the gene's evidence stops being reachable.

What is pinned here is the resolution and its two refusals, because all three
are silent when wrong: the wrong gene is *a* gene, and a figure filed under it
looks exactly like a figure filed correctly.

Deliberately not pinned: the wording of the banner or the confirm dialog.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"

# Every gene this module touches, cleared before it builds them, because the
# services suite shares one migrated SQLite file rather than rolling back.
GENES = ["LRRK2", "FLT1", "RAB43", "RAB41", "CALM1", "PICALM", "CSF1",
         "SQSTM1", "NA", "TRPA1"]


@pytest.fixture
def genes(_pipeline_db):
    from pipeline.models import Target
    Target.objects.using(DB).filter(gene_name__in=GENES).delete()

    def make(gene, *, alternative_name="", aliases=""):
        return Target.objects.using(DB).create(
            gene_name=gene, alternative_name=alternative_name, aliases=aliases)

    made = {
        "lrrk2": make("LRRK2", aliases="PARK8, DARDARIN"),
        "flt1": make("FLT1", alternative_name="VEGFR-1", aliases="FLT, VEGFR1"),
        # RAB41 is a target of its own *and* one of RAB43's other names — the
        # shape that makes exact-name-wins load-bearing rather than tidy.
        "rab43": make("RAB43", aliases="RAB41"),
        "rab41": make("RAB41", aliases="RAB41B"),
        # `CALM` is CALM1's everyday name and also on PICALM's synonym list.
        "calm1": make("CALM1", aliases="CALM, CAMI"),
        "picalm": make("PICALM", aliases="CALM, CLTH"),
        "csf1": make("CSF1", alternative_name="M-CSF", aliases="M-CSF, MCSF"),
        "sqstm1": make("SQSTM1", aliases="p62, A170"),
        "na": make("NA"),
    }
    yield made
    Target.objects.using(DB).filter(gene_name__in=GENES).delete()


def _match(gene):
    from pipeline.services.targets import match_gene
    return match_gene(gene)


def test_an_older_symbol_resolves_to_the_gene_on_file(genes):
    match = _match("PARK8")
    assert match.target is not None and match.target.pk == genes["lrrk2"].pk
    assert match.matched_via == "alias"
    assert match.on_file_as == "LRRK2"
    assert match.gene_name == "LRRK2", "a write must use the spelling on file"


def test_both_alias_columns_are_read(genes):
    """Which column a synonym is in is an artefact of which importer ran.

    ``alternative_name`` is Access's column plus what
    ``resolve_or_create_target`` writes; ``aliases`` is what
    ``enrich_targets_from_uniprot`` writes, and on the live data it is the one
    holding 448 of the 583 targets' symbols. Reading one is reading half.
    """
    assert _match("VEGFR1").gene_name == "FLT1"     # aliases
    assert _match("VEGFR-1").gene_name == "FLT1"    # alternative_name


def test_an_exact_name_beats_an_alias(genes):
    """Load-bearing, not tidiness: on the live data ``RAB41`` is a target of its
    own *and* is listed among RAB43's other names. Alias-first would file
    RAB41's figures under RAB43."""
    match = _match("RAB41")
    assert match.target.pk == genes["rab41"].pk
    assert match.matched_via == "name"


def test_a_name_that_matches_several_is_a_question_not_a_coin_toss(genes):
    """Thirteen other-names on the live data name two targets each — ``CALM`` is
    CALM1 and PICALM, ``SPARC`` is SPOCK2 and SPOCK3. Picking one publishes a
    blot on the wrong gene's page."""
    match = _match("CALM")
    assert match.target is None, "an ambiguous alias must not resolve"
    assert match.ambiguous == ["CALM1", "PICALM"]


def test_one_target_reached_by_two_of_its_own_names_is_one_answer(genes):
    """The two columns overlap constantly on the real rows — that is agreement,
    not ambiguity."""
    match = _match("M-CSF")
    assert match.gene_name == "CSF1"
    assert not match.ambiguous


def test_case_is_ignored_on_both_sides(genes):
    assert _match("P62").gene_name == "SQSTM1"
    assert _match("sqstm1").matched_via == "name"


def test_a_gene_nobody_has_recorded_is_simply_not_found(genes):
    match = _match("TRPA1")
    assert match.target is None and not match.ambiguous
    assert match.gene_name == "TRPA1", "a create should use what was typed"


def test_NA_is_not_a_gene(genes):
    """`NA` says this row has no gene. It must not resolve, here as anywhere —
    96 wild types on file point at an Access-era target literally called NA."""
    assert _match("NA").target is None
    assert _match("").target is None


def test_a_substring_is_not_a_match(genes):
    """`RAB4` sits inside `RAB41` and `RAB41B`, and a LIKE-based reader joins
    them."""
    match = _match("RAB4")
    assert match.target is None and not match.ambiguous
