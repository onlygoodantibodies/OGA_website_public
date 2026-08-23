"""Tests for the ``enrich_targets_from_uniprot`` management command.

UniProt is mocked (no network): proves gap-fill fills blank mass/aliases/
alt_name, never overwrites a populated field, never writes a "-" alt-name
placeholder, respects --scope, skips accession-less targets, and that dry-run
writes nothing.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command

DB = "pipeline_db"

# What our mocked UniProt returns, keyed by accession.
_FAKE = {
    "P02649": {"found": True, "mass_kda": 36.15,
               "gene_synonyms": ["AD2"], "alternative_names": ["Apo-E"]},
    "Q9ULC5": {"found": True, "mass_kda": 75.99,
               "gene_synonyms": ["ACS2", "ACS5", "FACL5"],
               "alternative_names": ["LACS 5"]},
    "Q9H2P0": {"found": True, "mass_kda": 123.56,
               "gene_synonyms": ["ADNP1", "KIAA0784"],
               "alternative_names": ["-"]},          # placeholder only → alt_name stays blank
}


@pytest.fixture()
def fake_uniprot(monkeypatch):
    from pipeline.services import uniprot

    def _fake(accession):
        return _FAKE.get((accession or "").strip(),
                          {"found": False, "error": "not found"})

    monkeypatch.setattr(uniprot, "lookup_accession", _fake)


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import Target
    Target.objects.using(DB).all().delete()
    mk = lambda **kw: Target.objects.using(DB).create(**kw)
    return {
        # all three blank → all filled
        "apoe": mk(gene_name="APOE", protein_name="Apolipoprotein E",
                   uniprot_id="P02649", status="published"),
        # mass already set → not overwritten; aliases/alt_name filled
        "acsl5": mk(gene_name="ACSL5", protein_name="Long-chain ... ligase 5",
                    uniprot_id="Q9ULC5", status="published",
                    theoretical_mass_kda=Decimal("99.99"), aliases=""),
        # alt name is only "-" placeholder → alternative_name stays blank
        "adnp": mk(gene_name="ADNP", protein_name="ADNP protein",
                   uniprot_id="Q9H2P0", status="in_progress"),
        # no accession → skipped entirely
        "noacc": mk(gene_name="XXX", protein_name="Unknown", status="published"),
    }


def test_apply_gapfills_without_overwrite(seeded, fake_uniprot):
    call_command("enrich_targets_from_uniprot", "--apply")
    for t in seeded.values():
        t.refresh_from_db(using=DB)

    # APOE: all three blank → filled
    assert seeded["apoe"].theoretical_mass_kda == Decimal("36.15")
    assert seeded["apoe"].aliases == "AD2"
    assert seeded["apoe"].alternative_name == "Apo-E"

    # ACSL5: mass populated → kept; aliases + alt_name filled
    assert seeded["acsl5"].theoretical_mass_kda == Decimal("99.99")   # NOT overwritten
    assert seeded["acsl5"].aliases == "ACS2, ACS5, FACL5"
    assert seeded["acsl5"].alternative_name == "LACS 5"

    # ADNP: only a "-" alt name → alternative_name stays blank; mass/aliases fill
    assert seeded["adnp"].theoretical_mass_kda == Decimal("123.56")
    assert seeded["adnp"].aliases == "ADNP1, KIAA0784"
    assert seeded["adnp"].alternative_name == ""

    # no accession → untouched
    assert seeded["noacc"].theoretical_mass_kda is None


def test_scope_published_skips_in_progress(seeded, fake_uniprot):
    call_command("enrich_targets_from_uniprot", "--apply", "--scope", "published")
    seeded["adnp"].refresh_from_db(using=DB)      # in_progress → skipped
    seeded["apoe"].refresh_from_db(using=DB)      # published → filled
    assert seeded["adnp"].theoretical_mass_kda is None
    assert seeded["apoe"].theoretical_mass_kda == Decimal("36.15")


def test_dry_run_writes_nothing(seeded, fake_uniprot):
    call_command("enrich_targets_from_uniprot")   # no --apply
    seeded["apoe"].refresh_from_db(using=DB)
    assert seeded["apoe"].theoretical_mass_kda is None
    assert seeded["apoe"].aliases == ""
