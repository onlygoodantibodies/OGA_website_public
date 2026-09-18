"""The public-data boundary: Server A must never surface anything that isn't on
the public site. These tests pin the rule end-to-end against the seeded db.

The structured tools now enforce the boundary in the ORM (portal.py's
``publication_images__isnull=False`` filter); analytics_sql stays view-scoped.

Seed split (see seed_scratch.py):
  * PUBLIC (has a publication image): SNCA (ab212184, 2642), SYT1 (NB120-1234)
  * NON-public (no figure):           MAPT (ab8069), GFAP (3670)
"""
from __future__ import annotations

import pytest

from mcp_servers.common import portal


@pytest.fixture(scope="module", autouse=True)
def _django(_seeded_pipeline_db):
    portal.setup()


# ── unpublished targets are invisible ───────────────────────────────────────

def test_unpublished_target_not_listed():
    genes = {r["gene_name"] for r in portal.list_targets()}
    assert "MAPT" not in genes and "GFAP" not in genes


@pytest.mark.parametrize("gene", ["MAPT", "GFAP"])
def test_unpublished_target_report_not_found(gene):
    assert portal.gene_detail(gene)["found"] is False


# ── unpublished antibodies are invisible on every lookup ────────────────────

@pytest.mark.parametrize("needle", ["ab8069", "3670"])
def test_unpublished_antibody_not_searchable(needle):
    rows, _ = portal.search_antibodies(needle)
    assert rows == []


@pytest.mark.parametrize("gene", ["MAPT", "GFAP"])
def test_unpublished_antibody_no_validation(gene):
    rows, _ = portal.antibody_validation(gene=gene)
    assert rows == []


def test_unpublished_not_recommended_hidden():
    # MAPT's ab is not recommended for WB, but it is non-public -> never returned,
    # even on the per-gene "explain the non-recommendation" path.
    for rung in ("limited_support", "not_supportive"):
        rows, _ = portal.antibodies_by_support("WB", rung, "MAPT")
        assert rows == []
    # the public SNCA gene still surfaces its published negative ab (2642),
    # whichever of the two negative rungs it resolves to.
    found = set()
    for rung in ("limited_support", "not_supportive"):
        rows, _ = portal.antibodies_by_support("WB", rung, "SNCA")
        found |= {r["antibody_name"] for r in rows}
    assert "2642" in found


# ── internal columns never leave the database ───────────────────────────────

# The connector carries what the LIVE SITE and PORTAL API carry, and nothing that
# exists only inside the pipeline. Per-session lab results (wb_results / ip_results
# / if_results / fc_results) are pipeline-only models; they were once surfaced as an
# `evidence` array, which both leaked internal records and let the connector show
# session rows that disagreed with the published verdict.

#: Everything ``core.api_views._serialise_antibody`` returns — the public contract.
_PUBLIC_KEYS = {"antibody_name", "gene", "gene_has_recommendations", "gene_page_url",
                "created_at", "metadata", "recommendations",
                "oga_recommendations", "oga_support", "oga_qualifiers",
                "oga_display", "verdicts", "experiments", "embed_urls"}
# `oga_recommendations` (see core/recommendations.py) is the three-valued form of
# `recommendations`, derived from the same public flags plus whether a figure was
# published: a bare False cannot say whether an antibody was tested and not
# recommended or never tested at all. `verdicts` is its deprecated alias — the
# name this API first shipped under, and the wrong word, since these are
# recommendations from testing under the consensus protocols in one cell line
# rather than verdicts on a product. Both are public data, so both belong in this
# set — and this test is what makes adding a key a decision rather than an
# accident.
# `oga_qualifiers` is the same call: `_serialise_antibody` has returned it since
# the qualified pill shipped, so it is already on the public API and the portal's
# own script reads it (`core/tests_portal_script.py`). It is derived from
# `oga_recommendations` plus the public capability axes and holds a sentence per
# application, never a session row. Naming it here is the decision this test asks
# for; it went in without one, which is why the job was red. It shipped as
# `oga_caveats` and was renamed the day after: the clause belongs on a supportive
# verdict too, and "caveat" is the wrong word for "strongly selective".
# `oga_display` is the same call once more: the site's own words, sentence and
# colour for each verdict, all derived from `oga_recommendations` plus the
# public capability axes and every one of them already printed on a public gene
# page. It is additive — the four keys above are untouched — so a consumer
# switching on the controlled values is unaffected.
# `oga_support` is the same call once more, and the one that closes the gap the
# three keys above left open: the four-rung result as a controlled value, so a
# caller can switch on the middle rung instead of re-deriving it from
# `oga_recommendations` plus a qualifier. Public by construction — it is exactly
# what every gene page prints, and `core/api_views._serialise_antibody` returns
# it on the open feed. Additive: nothing above it moved.
#: What this connector adds on top, all derived from public fields only.
_ADDED_KEYS = {"assessment", "summary", "provenance", "reports"}


def test_no_pipeline_session_results_are_surfaced():
    rows, _ = portal.antibody_validation(catalogue="2642")
    ab = rows[0]
    assert "evidence" not in ab
    blob = repr(ab).lower()
    for internal in ("wb_results", "ip_results", "if_results", "fc_results",
                     "comments", "session"):
        assert internal not in blob, internal


def test_payload_carries_nothing_beyond_public_data_plus_our_layer():
    rows, _ = portal.antibody_validation(catalogue="2642")
    extra = set(rows[0]) - _PUBLIC_KEYS - _ADDED_KEYS
    assert extra == set(), f"non-public keys surfaced: {sorted(extra)}"


def test_assessment_states_which_public_signals_it_rests_on():
    rows, _ = portal.antibody_validation(catalogue="2642")
    for app, a in rows[0]["assessment"].items():
        assert set(a["tested_from"]) <= {"recommendation_flag", "published_figure",
                                         "gene_curated"}
        # tested must never be asserted without a public signal behind it
        assert a["tested"] is bool(a["tested_from"])


def test_cell_lines_not_surfaced():
    # Cell-line models are deliberately not exposed by the connector.
    rep = portal.gene_detail("SNCA")
    assert "cell_lines" not in rep


def test_antibody_rows_have_no_lot_number():
    rep = portal.gene_detail("SNCA")
    for ab in rep["antibodies"]:
        assert "lot_number" not in ab
        assert "lot_number" not in ab["metadata"]


# ── an UNCURATED gene must never read as "tested and failed" ─────────────────
# QPRT is public (its antibody has a published figure) but no antibody on the gene
# carries any recommendation — nobody has curated it. Deriving the verdict from the
# figure alone reported every such antibody as "tested and not recommended", i.e.
# telling users a real commercial product failed independent testing when it had
# simply never been assessed.

def test_uncurated_gene_reports_not_tested_not_failed():
    rows, _ = portal.antibody_validation(catalogue="ab171939")
    ab = rows[0]
    assert ab["gene"] == "QPRT"
    assert ab["gene_has_recommendations"] is False
    for app, a in ab["assessment"].items():
        assert a["status"] == "not_tested", app
        assert a["tested"] is False
        assert a["tested_from"] == []
    assert "not recommended" not in ab["summary"].lower()


def test_curated_gene_without_a_flag_is_a_real_negative():
    rows, _ = portal.antibody_validation(catalogue="2642")     # SNCA is curated
    wb = rows[0]["assessment"]["WB"]
    assert wb["status"] == "not_recommended"
    assert set(wb["tested_from"]) == {"published_figure", "gene_curated"}


def test_no_verdict_source_is_claimed_when_no_verdict_was_reached():
    # Previously emitted on not_tested entries, asserting a source for a verdict
    # that does not exist.
    for catalogue in ("2642", "ab212184", "ab171939"):
        rows, _ = portal.antibody_validation(catalogue=catalogue)
        for app, a in rows[0]["assessment"].items():
            if a["status"] == "not_tested":
                assert "verdict_source" not in a, f"{catalogue} {app}"
            else:
                assert a["verdict_source"]
