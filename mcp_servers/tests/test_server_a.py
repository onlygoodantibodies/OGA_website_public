"""Server A (read-only) — per-antibody / per-gene lookups + the factual layer.

Tools serialise through the canonical public API code (portal.py) over the ORM,
add a controlled-vocabulary assessment (recommended / not_recommended /
not_tested), and refuse whole-database analytics (no free SQL; a gene is required
for the recommendation list).
"""
from __future__ import annotations

import pytest

from mcp_servers.common import portal


@pytest.fixture(scope="module", autouse=True)
def _django(_seeded_pipeline_db):
    portal.setup()


# ── list_targets (gene names + status only) ──────────────────────────────────

def test_list_targets_all():
    rows = portal.list_targets()
    genes = {r["gene_name"] for r in rows}
    assert {"SNCA", "SYT1"} <= genes
    assert "MAPT" not in genes and "GFAP" not in genes
    # internal pipeline workflow status is never exposed
    assert all("status" not in r for r in rows)


def test_list_targets_matches_site_gene_set():
    # A published target with NO gene symbol is counted on neither the homepage
    # (get_live_stats) nor Server A — the two gene sets must match exactly.
    from pipeline.models import Target, Antibody, Company, PublicationImage
    from django.db.models import Q
    DB = "pipeline_db"
    company = Company.objects.using(DB).first()
    t = Target.objects.using(DB).create(protein_name="Nameless protein", gene_name=None)
    ab = Antibody.objects.using(DB).create(target=t, catalogue_number="NONAME-1",
                                           company=company)
    PublicationImage.objects.using(DB).create(
        antibody=ab, application_type="WB", image="publication_images/x_WB.png")
    try:
        site_count = (Target.objects.using(DB)
                      .filter(gene_name__isnull=False,
                              antibodies__publication_images__isnull=False)
                      .exclude(gene_name="").distinct().count())
        rows = portal.list_targets()
        assert len(rows) == site_count                       # counts match the site
        assert t.id not in {r["id"] for r in rows}           # nameless target excluded
    finally:
        ab.delete(using=DB)
        t.delete(using=DB)


def test_list_targets_only_with_recommendations():
    genes = {r["gene_name"] for r in portal.list_targets(only_with_recommendations=True)}
    assert {"SNCA", "SYT1"} <= genes
    assert "MAPT" not in genes and "GFAP" not in genes


# ── antibodies_by_recommendation — a GENE is required (no whole-DB list) ──────

def test_by_recommendation_requires_gene():
    with pytest.raises(ValueError):
        portal.antibodies_by_recommendation("WB", recommended=True)   # no gene


def test_unknown_application():
    with pytest.raises(ValueError):
        portal.antibodies_by_recommendation("ELISA", gene="SNCA")


def test_recommended_for_wb_in_gene():
    rows, _ = portal.antibodies_by_recommendation("WB", recommended=True, gene="SNCA")
    cats = {r["antibody_name"] for r in rows}
    assert "ab212184" in cats     # SNCA good ab, WB recommended
    assert "2642" not in cats     # SNCA published ab, not WB recommended


def test_not_recommended_for_wb_states_what_the_verdict_rests_on():
    rows, _ = portal.antibodies_by_recommendation("WB", recommended=False, gene="SNCA")
    by_cat = {r["antibody_name"]: r for r in rows}
    assert "2642" in by_cat
    wb = by_cat["2642"]["assessment"]["WB"]
    assert wb["status"] == "not_recommended"
    # the basis is stated, and it is the curator's flag — not a computation over
    # per-session lab records, which this connector does not carry
    # the basis is stated: a curated gene published this application's figure and
    # the curator did not flag this antibody — a real negative, not an absence
    assert set(wb["tested_from"]) == {"published_figure", "gene_curated"}
    assert "curated gene" in wb["verdict_source"]


def test_if_alias_icc_if():
    a, _ = portal.antibodies_by_recommendation("IF", recommended=True, gene="SYT1")
    b, _ = portal.antibodies_by_recommendation("ICC-IF", recommended=True, gene="SYT1")
    assert {r["antibody_name"] for r in a} == {r["antibody_name"] for r in b}


# ── the factual interpretation layer (4b) ────────────────────────────────────

def test_assessment_controlled_vocabulary():
    rows, _ = portal.antibody_validation(catalogue="ab212184")
    ab = rows[0]
    assert ab["assessment"]["WB"]["status"] == "recommended"
    # FC has no recommendation, no evidence, no figure → not tested (≠ bad)
    assert ab["assessment"]["FC"]["status"] == "not_tested"
    assert ab["assessment"]["FC"]["tested"] is False


def test_tested_not_recommended_is_distinct_from_untested():
    rows, _ = portal.antibody_validation(catalogue="2642")
    a = rows[0]["assessment"]["WB"]
    assert a["status"] == "not_recommended" and a["tested"] is True


def test_summary_is_factual_and_cites_doi():
    rows, _ = portal.antibody_validation(catalogue="ab212184")
    s = rows[0]["summary"]
    assert "in the dataset" in s
    assert "Recommended for" in s and "WB" in s
    assert rows[0]["provenance"]["report_dois"]        # consensus-protocol DOI(s)


def test_serialised_shape_matches_portal():
    rows, _ = portal.antibody_validation(gene="SNCA")
    ab = rows[0]
    assert set(ab) >= {"antibody_name", "gene", "metadata", "recommendations",
                       "experiments", "assessment", "summary", "provenance",
                       "reports"}
    # embed cards are dropped: every caller is told to use the direct media files
    # in `experiments` instead, so shipping them was pure payload
    assert "embed_urls" not in ab
    assert "evidence" not in ab
    assert set(ab["recommendations"]) == {"WB", "ICC-IF", "IP", "FC"}


# ── target_report / gene_detail ──────────────────────────────────────────────

def test_target_report():
    rep = portal.gene_detail("snca")   # case-insensitive
    assert rep["found"] is True and rep["gene"] == "SNCA"
    assert len(rep["antibodies"]) == 2
    assert "cell_lines" not in rep                      # cell-line data not surfaced
    assert rep["antibodies"][0]["assessment"]          # factual layer present


def test_target_report_missing():
    assert portal.gene_detail("NOPE")["found"] is False


def test_target_report_includes_reports():
    rep = portal.gene_detail("SNCA")
    assert rep["reports"] and rep["reports"][0]["zenodo_doi"]


# ── search: by catalogue/RRID/gene, never by company ─────────────────────────

def test_search_by_catalogue():
    rows, _ = portal.search_antibodies("ab212184")
    assert rows and rows[0]["antibody_name"] == "ab212184"


def test_search_does_not_enumerate_by_company():
    # a company name must NOT return that vendor's antibodies
    rows, _ = portal.search_antibodies("Abcam")
    assert rows == []


# ── antibody_validation lookups ──────────────────────────────────────────────

def test_antibody_validation_by_rrid():
    rows, _ = portal.antibody_validation(rrid="AB_2895247")
    assert rows and rows[0]["antibody_name"] == "ab212184"


def test_antibody_validation_by_gene_lists_all():
    rows, _ = portal.antibody_validation(gene="SNCA")
    assert {r["antibody_name"] for r in rows} == {"ab212184", "2642"}


def test_antibody_validation_requires_an_arg():
    with pytest.raises(ValueError):
        portal.antibody_validation()
