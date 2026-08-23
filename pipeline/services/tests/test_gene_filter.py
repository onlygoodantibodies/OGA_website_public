"""``?gene=`` reads one gene, or a list of them — `services/targets.py`.

The filter has always meant *an exact gene*, spelled the same way on all four
boards so a link can carry one from board to board. It now also carries several,
because a bulk add of targets lands on the board narrowed to exactly the genes it
just added, rather than on 585 rows with yours somewhere in them.

One gene is unchanged, which is the point: every link that existed keeps meaning
what it meant. What is pinned here is the reading — the boards' own use of it is
in `pipeline/tests_navigation.py`, and the front end's mirror of this split is
`OGABoard.geneTerms`, which `oneGene` gates deleting on.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def genes(_pipeline_db):
    """Targets to filter, cleared before **and after**.

    `conftest._pipeline_db` is one SQLite file for the whole session with no
    per-test rollback, so a module that leaves rows behind breaks the next one
    on a UNIQUE constraint that reads as a bug in whatever ran second.
    """
    from pipeline.models import Target, TargetNomination

    def _clear():
        TargetNomination.objects.using("pipeline_db").all().delete()
        Target.objects.using("pipeline_db").all().delete()

    _clear()
    yield Target
    _clear()


def test_one_gene_reads_as_one_gene():
    from pipeline.services import targets
    assert targets.gene_terms("STMN2") == ["STMN2"]
    assert targets.gene_terms("  STMN2  ") == ["STMN2"]


def test_nothing_reads_as_nothing():
    """"No gene filter" and "a gene filter that matches nothing" are different
    answers, and `gene_q` returning None is how a caller tells them apart — a
    filter that quietly widens to everything is worse than one matching no rows.
    """
    from pipeline.services import targets
    for value in ("", "   ", None, ",", " , , "):
        assert targets.gene_terms(value) == []
        assert targets.gene_q("gene_name", value) is None


def test_a_list_may_be_spaced_the_way_a_person_reads_it():
    """A link carries `STMN2,ELP3`; somebody who read that types the space back
    in. Both are the same filter."""
    from pipeline.services import targets
    for value in ("STMN2,ELP3", "STMN2, ELP3", "STMN2 ELP3", "STMN2,  ELP3 "):
        assert targets.gene_terms(value) == ["STMN2", "ELP3"]


def test_a_repeated_gene_is_read_once_and_order_is_kept():
    from pipeline.services import targets
    assert targets.gene_terms("STMN2, elp3, STMN2") == ["STMN2", "elp3"]


def test_the_query_is_case_insensitive_per_term(genes):
    """`__in` would have been the obvious way to write this and is
    case-sensitive on PostgreSQL, where the Access-era rows are not reliably
    upper-case. An OR of `iexact` terms costs query size and nothing else.
    """
    from pipeline.services import targets

    Target = genes
    for gene in ("STMN2", "Elp3", "SOD1"):
        Target.objects.using("pipeline_db").create(gene_name=gene)

    q = targets.gene_q("gene_name", "stmn2, ELP3")
    found = set(Target.objects.using("pipeline_db").filter(q)
                .values_list("gene_name", flat=True))
    assert found == {"STMN2", "Elp3"}


def test_a_term_stays_exact(genes):
    """Widening to a list must not widen what one term matches: STMN1 and STMN2
    are different genes, and `q` is the box for guessing."""
    from pipeline.services import targets

    Target = genes
    Target.objects.using("pipeline_db").create(gene_name="STMN1")
    Target.objects.using("pipeline_db").create(gene_name="STMN2")

    q = targets.gene_q("gene_name", "STMN")
    assert not Target.objects.using("pipeline_db").filter(q).exists()
