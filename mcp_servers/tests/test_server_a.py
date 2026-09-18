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


# ── antibodies_by_support — a GENE is required (no whole-DB list) ────────────

def test_by_support_requires_gene():
    with pytest.raises(ValueError):
        portal.antibodies_by_support("WB", "supportive")   # no gene


def test_unknown_application():
    with pytest.raises(ValueError):
        portal.antibodies_by_support("ELISA", gene="SNCA")


def test_supportive_for_wb_in_gene():
    rows, _ = portal.antibodies_by_support("WB", "supportive", "SNCA")
    cats = {r["antibody_name"] for r in rows}
    assert "ab212184" in cats     # SNCA good ab, WB supportive
    assert "2642" not in cats     # SNCA published ab, WB not supportive


def test_a_negative_states_what_the_result_rests_on():
    """Whichever negative rung 2642 lands on, the basis must be stated.

    Asked across both rungs rather than through the old boolean: the question
    is what the connector says a negative RESTS on, and that is true of the
    qualified negative as much as the flat one.
    """
    rows = []
    for rung in ("limited_support", "not_supportive"):
        found, _ = portal.antibodies_by_support("WB", rung, "SNCA")
        rows.extend(found)
    by_cat = {r["antibody_name"]: r for r in rows}
    assert "2642" in by_cat
    wb = by_cat["2642"]["assessment"]["WB"]
    assert wb["status"] == "not_recommended"
    assert wb["support"] in ("limited_support", "not_supportive")
    # the basis is stated: a curated gene published this application's figure and
    # the curator did not flag this antibody — a real negative, not an absence
    assert set(wb["tested_from"]) == {"published_figure", "gene_curated"}
    assert "curated gene" in wb["verdict_source"]


def test_untested_is_not_returned_among_the_negatives():
    """The boolean form could not draw this line and this one must.

    `_published_antibodies` scopes to antibodies carrying a figure for SOME
    application, so a `False` flag on an application nobody ran was coming back
    among the failures. Asking for a rung asks the resolved question instead.
    """
    negatives = set()
    for rung in ("limited_support", "not_supportive"):
        rows, _ = portal.antibodies_by_support("FC", rung, "SNCA")
        negatives |= {r["antibody_name"] for r in rows}
    untested, _ = portal.antibodies_by_support("FC", "not_tested", "SNCA")
    assert {r["antibody_name"] for r in untested}
    assert negatives & {r["antibody_name"] for r in untested} == set()


def test_if_alias_icc_if():
    a, _ = portal.antibodies_by_support("IF", "supportive", "SYT1")
    b, _ = portal.antibodies_by_support("ICC-IF", "supportive", "SYT1")
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
    """The prose is the site's frame, not this connector's own: OGA
    characterises antibodies, a gene page is headed "characterisation data",
    and a model quoting this is quoting a page a reader may go and read. The
    per-application `status` values are untouched — see the test below."""
    rows, _ = portal.antibody_validation(catalogue="ab212184")
    s = rows[0]["summary"]
    assert "in the dataset" in s
    assert "Characterisation data supports" in s and "WB" in s
    assert rows[0]["provenance"]["report_dois"]        # consensus-protocol DOI(s)


def test_the_controlled_vocabulary_is_not_the_prose():
    """`status` is the machine contract and does not move when the wording
    does. A consumer switching on it keeps working; `verdict` is the words
    beside it."""
    rows, _ = portal.antibody_validation(catalogue="ab212184")
    entry = rows[0]["assessment"]["WB"]
    assert entry["status"] == "recommended"
    assert entry["verdict"] == "Supportive"
    assert entry["verdict_sentence"].startswith("Supportive")


def test_the_assessment_names_the_rung_as_well_as_the_status():
    """`support` is the four-rung value; `status` is the older three.

    A model reads a dict in order and takes the first field that looks like the
    answer, so `support` leads. What is pinned here is that both are present and
    that they do not contradict each other: `status` has always been the
    connector's contract and a caller written against it must keep working.
    """
    rows, _ = portal.antibody_validation(catalogue="ab212184")
    entry = rows[0]["assessment"]["WB"]
    assert entry["support"] == "supportive"
    assert entry["status"] == "recommended"
    # Order matters for a model, not for a parser. `support` is first.
    assert list(entry)[0] == "support"

    untested = rows[0]["assessment"]["FC"]
    assert untested["support"] == "not_tested" == untested["status"]


def test_a_qualified_negative_is_the_middle_rung_not_the_harsh_one():
    """The one case the legacy `status` cannot express.

    `2642` is tested and not supported overall for WB, and the seed records that
    it does detect its target. Reported through `status` alone that is
    indistinguishable from an antibody that showed nothing — 491 of the 1,833
    negative results on the live dataset — which is the whole reason `support`
    exists on this connector.

    Asserted unconditionally, which it was not when first written: the fixture
    seeded no `AntibodyOutcome` at all, so the middle rung was unreachable and
    this test passed down an `else` branch while naming the rung it never
    checked. `seed_scratch` gives 2642 its axes now.
    """
    rows, _ = portal.antibody_validation(catalogue="ab27766")
    entry = rows[0]["assessment"]["WB"]
    assert entry["support"] == "limited_support"
    assert entry["verdict"] == "Limited support"
    assert entry["qualifier"] == "detects the target"

    # The pair that shows why both fields ship: this row and 2642 are two
    # different findings, and the legacy field calls them the same thing.
    flat, _ = portal.antibody_validation(catalogue="2642")
    other = flat[0]["assessment"]["WB"]
    assert other["support"] == "not_supportive"
    assert entry["status"] == other["status"] == "not_recommended"


def test_the_summary_does_not_fold_the_middle_rung_into_the_negative():
    """The sentence a model is most likely to quote verbatim.

    It grouped applications by `status`, so a *Limited support* result was
    printed in the same clause as an outright negative — the distinction
    carried correctly everywhere else in the response and lost in the one
    string most likely to reach a reader unaltered.
    """
    rows, _ = portal.antibody_validation(catalogue="ab27766")
    summary = rows[0]["summary"]
    assert "Limited support for WB" in summary
    assert "not supportive for WB" not in summary
    # The clause says what was seen, so a reader is not left with a bare rung.
    assert "still seen to do what the application is for" in summary


def test_by_support_can_ask_for_one_rung():
    """Asking for one rung returns only that rung.

    The boolean form this replaced returned both negatives in one list, which
    is why it was removed rather than kept as an alias. SNCA/WB holds one
    antibody at each of the two rungs it could not separate, so the split is
    asserted rather than merely iterated over an empty list.
    """
    at = {}
    for rung in portal.SUPPORT_RUNGS:
        rows, _ = portal.antibodies_by_support("WB", rung, "SNCA")
        for row in rows:
            assert row["assessment"]["WB"]["support"] == rung
        at[rung] = {r["antibody_name"] for r in rows}

    assert at["supportive"] == {"ab212184"}
    assert at["limited_support"] == {"ab27766"}
    assert at["not_supportive"] == {"2642"}


def test_by_support_refuses_an_unknown_rung():
    """Named, not guessed at — the same shape as the unknown-application
    refusal beside it."""
    import pytest as _pytest
    with _pytest.raises(ValueError) as caught:
        portal.antibodies_by_support("WB", "recommended", "SNCA")
    assert "recommended" in str(caught.value)
    assert "limited_support" in str(caught.value)


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
    # Three since 14 Sep 2026: SNCA carries one antibody at each of the three
    # rungs a published figure can reach — supportive, limited support and not
    # supportive — so the middle one is reachable from this fixture at all.
    assert len(rep["antibodies"]) == 3
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
    assert {r["antibody_name"] for r in rows} == {"ab212184", "2642", "ab27766"}


def test_antibody_validation_requires_an_arg():
    with pytest.raises(ValueError):
        portal.antibody_validation()
