"""Tests for the ``backfill_depmap_expression`` management command.

Proves: a blank target is filled with its cached HAP1 log2(TPM+1); a gene with
DepMap data but no HAP1 row is skipped; a gene absent from the cache is skipped;
a populated value is never overwritten; dry-run writes nothing.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import Target, DepMapExpression
    for M in (DepMapExpression, Target):
        M.objects.using(DB).all().delete()

    def tgt(gene, **kw):
        return Target.objects.using(DB).create(gene_name=gene, protein_name=f"{gene} protein", **kw)

    def dm(gene, line, val):
        DepMapExpression.objects.using(DB).create(gene_name=gene, cell_line=line, tpm_log2=val)

    t = {
        "fill": tgt("APOE", status="published"),
        "no_hap1": tgt("XGENE"),                                 # DepMap data, but no HAP1 row
        "no_dm": tgt("YGENE"),                                   # not in the cache at all
        "populated": tgt("ZGENE", depmap_expression=Decimal("9.99")),
    }
    dm("APOE", "HAP1", 4.37)
    dm("APOE", "HeLa", 6.10)
    dm("XGENE", "HeLa", 3.00)         # no HAP1 for XGENE
    dm("ZGENE", "HAP1", 1.00)         # would be 1.00, but ZGENE already populated
    return t


def test_apply_fills_from_hap1(seeded):
    call_command("backfill_depmap_expression", "--apply")
    for x in seeded.values():
        x.refresh_from_db(using=DB)

    assert seeded["fill"].depmap_expression == Decimal("4.37")   # HAP1 value, not HeLa's 6.10
    assert seeded["no_hap1"].depmap_expression is None           # has data but no HAP1 → skipped
    assert seeded["no_dm"].depmap_expression is None             # not cached → skipped
    assert seeded["populated"].depmap_expression == Decimal("9.99")  # never overwritten


def test_dry_run_writes_nothing(seeded):
    call_command("backfill_depmap_expression")   # no --apply
    seeded["fill"].refresh_from_db(using=DB)
    assert seeded["fill"].depmap_expression is None


def test_scope_published_only(seeded):
    seeded["no_hap1"].status = "published"        # give the non-HAP1 one published status
    seeded["no_hap1"].save(using=DB)
    call_command("backfill_depmap_expression", "--apply", "--scope", "published")
    seeded["fill"].refresh_from_db(using=DB)
    assert seeded["fill"].depmap_expression == Decimal("4.37")   # published + HAP1 → filled
