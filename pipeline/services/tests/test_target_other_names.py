"""A gene's other names come from two columns, and the app draws one list.

``Target.alternative_name`` is Access's ``AlternProteinName``, and also what
``resolve_or_create_target`` fills with UniProt's gene synonyms.
``Target.aliases`` is the gene-symbol column ``enrich_targets_from_uniprot``
writes — and the one the Downloads & uploads round trip writes, which is how a
field test's added alias reached the database, was findable in the search box,
and appeared on no screen anywhere.

Which of the two a name is in is an artefact of which importer wrote it, so
``services/targets.py::other_names`` folds them into one list. It reads
attributes only — no database, no queries — so the gene page and the target
board can both call it per row without either becoming an N+1.
"""
from __future__ import annotations

from types import SimpleNamespace


def other_names(target):
    # Imported inside the call: conftest boots Django, so a module-level import
    # of anything touching pipeline.models fails at collection.
    from pipeline.services.targets import other_names as fn
    return fn(target)


def _t(**kw):
    return SimpleNamespace(**{"gene_name": "", "protein_name": "",
                              "alternative_name": "", "aliases": "", **kw})


def test_both_columns_are_shown():
    assert other_names(_t(gene_name="TRPA1", alternative_name="ANKTM1",
                          aliases="COWORK RUN15 alias probe")) == [
        "ANKTM1", "COWORK RUN15 alias probe"]


def test_a_name_in_both_columns_is_shown_once():
    """Two importers write these and they overlap — `resolve_or_create_target`
    puts UniProt's gene synonyms in `alternative_name` while
    `enrich_targets_from_uniprot` puts them in `aliases`. Printing a name twice
    on one line is the app looking broken about a value that is fine."""
    assert other_names(_t(alternative_name="ANKTM1, PARK8",
                          aliases="anktm1")) == ["ANKTM1", "PARK8"]


def test_the_genes_own_name_is_not_one_of_its_other_names():
    """"TRPA1 — also known as TRPA1" is noise, and the Access import left the
    symbol in the alternative-name column on some rows."""
    assert other_names(_t(gene_name="TRPA1", protein_name="Transient receptor",
                          alternative_name="TRPA1, Transient receptor",
                          aliases="ANKTM1")) == ["ANKTM1"]


def test_access_era_placeholders_are_dropped():
    """`-` is what the Access export wrote for "no alternative name", and
    `enrich_targets_from_uniprot` already refuses to store it. Rows that predate
    that check still carry it, and "Also known as: -" says nothing."""
    assert other_names(_t(alternative_name="-", aliases="NA")) == []


def test_nothing_recorded_is_an_empty_list_not_a_blank_entry():
    assert other_names(_t()) == []
    assert other_names(None) == []
