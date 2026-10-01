"""The DepMap lookup matches a gene exactly, whatever case it was typed in.

It asked ``iexact`` — a sequential scan of 461,160 rows on live, run on every
pause in typing on the public selection tool, which parked all four threads and
502'd the site (30 Sep 2026) — and, finding nothing, fell back to
``icontains``, so a half-typed "CD" was answered with every CD gene's
expression as if it were one gene's. Exact spellings now (``gene_symbol.
spellings``), so the silent half to pin is that a partial symbol finds nothing
and a mis-cased one still finds its gene.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import DepMapExpression
    DepMapExpression.objects.using(DB).all().delete()
    for gene, line, val in [("CD4", "HAP1", 3.0), ("CD8A", "HeLa", 5.0),
                            ("C9orf72", "HAP1", 4.0)]:
        DepMapExpression.objects.using(DB).create(
            gene_name=gene, cell_line=line, tpm_log2=val)


def test_a_partial_symbol_is_not_another_gene(seeded):
    from pipeline.services import depmap
    assert depmap.get_expression("CD")["found"] is False


def test_case_does_not_matter(seeded):
    from pipeline.services import depmap
    assert depmap.get_expression("cd4")["hap1_tpm"] == 3.0
    assert depmap.get_expression("C9ORF72")["hap1_tpm"] == 4.0
