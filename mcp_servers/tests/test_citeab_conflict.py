"""CiteAb's record of a paper, beside what the caller read — and never merged.

The value here is the DISAGREEMENT, so what is pinned is that a disagreement
survives to the reply intact. Two ways it could be lost, both silent:

* **Merging.** A union of the two application sets looks tidier and destroys the
  only signal. A reader seeing `WB, IF` cannot tell whether both sources said
  both, or one said each.
* **Resolving.** Picking CiteAb because it is "the data", or the model because it
  "read the paper", turns a question into an answer nobody checked. Neither is
  authoritative: CiteAb's join is on catalogue number, which is not unique across
  suppliers, and the model read one document once.

Also pinned: the three states of `citeab`. `not_covered` is a fact about CiteAb's
record and `unavailable` is a fact about this server, and neither is a fact about
whether the paper cites the reagent.
"""
import pytest

from core import citations
from mcp_servers.common import citeab, portal


_RRID = "AB_TEST_0001"


def _snapshot(applications, tokens=(), ambiguous=False):
    """A one-paper snapshot, in the artefact's real on-disk encoding.

    Built through the encoding rather than around it, so the flag arithmetic the
    builder writes and the reader parses is exercised here too.
    """
    flags = 0
    for app in applications:
        flags |= citations.APPLICATION_BITS[app]
    if ambiguous:
        flags |= citations.AMBIGUOUS
    packed = f"0:{flags | (citations.UNTESTED_APP if tokens else 0):x}"
    if tokens:
        packed += "." + ".".join(f"{i:x}" for i in range(len(tokens)))
    return {
        "schema": citations.SCHEMA, "source": {"citeab_data_through": "2026-05-05"},
        "rrids": [_RRID], "tokens": list(tokens), "papers": [packed],
        "years": [2023], "by_doi": {"10.1000/x": 0}, "by_pmid": {"111": 0},
        "by_title": {}, "counts": {"papers": 1},
    }


@pytest.fixture
def snapshot(monkeypatch):
    def _install(**kwargs):
        monkeypatch.setattr(citations, "load", lambda: _snapshot(**kwargs))
    return _install


# ── the encoding round-trips ─────────────────────────────────────────────────

def test_the_record_reads_back_as_it_was_written(snapshot):
    snapshot(applications=["WB", "IF"])
    status, records = citations.paper_for(pmid="111")
    assert status == "covered"
    assert records[_RRID]["applications"] == ["WB", "IF"]


def test_a_term_oga_does_not_test_is_carried_by_name(snapshot):
    snapshot(applications=[], tokens=["ChIP"])
    _, records = citations.paper_for(doi="10.1000/X")   # case-insensitive
    assert records[_RRID]["untested_terms"] == ["ChIP"]


# ── the three states ─────────────────────────────────────────────────────────

def test_no_identifier_means_the_record_was_not_consulted():
    state, records = citeab.for_paper()
    assert state["status"] == "not_asked"
    assert records is None


def test_a_paper_the_snapshot_lacks_is_not_covered_and_says_what_that_means(snapshot):
    snapshot(applications=["WB"])
    state, _ = citeab.for_paper(pmid="999")
    assert state["status"] == "not_covered"
    assert "NOT evidence that the paper cites" in state["note"]


def test_no_snapshot_at_all_is_a_fact_about_this_server(monkeypatch):
    monkeypatch.setattr(citations, "load", lambda: None)
    state, _ = citeab.for_paper(pmid="111")
    assert state["status"] == "unavailable"
    assert "says nothing about the paper" in state["note"]


def test_a_broken_snapshot_degrades_instead_of_failing_the_call(monkeypatch):
    def boom():
        raise RuntimeError("corrupt")
    monkeypatch.setattr(citations, "load", boom)
    state, records = citeab.for_paper(pmid="111")
    assert state["status"] == "unavailable"
    assert records is None


# ── the conflict itself ──────────────────────────────────────────────────────

def test_agreement_is_not_a_conflict():
    assert citeab.conflict(["WB"], ["WB"]) is None


def test_silence_on_either_side_is_not_a_conflict():
    """Otherwise every reagent CiteAb has never seen carries a disagreement."""
    assert citeab.conflict(["WB"], []) is None
    assert citeab.conflict([], ["WB"]) is None


def test_a_disagreement_names_both_sides_and_picks_neither():
    clash = citeab.conflict(["WB"], ["IF"])
    assert clash["you_read"] == ["WB"]
    assert clash["citeab_records"] == ["IF"]
    assert clash["only_you"] == ["WB"]
    assert clash["only_citeab"] == ["IF"]
    # No winner, no merged set, no boolean anywhere in it.
    assert not any(isinstance(v, bool) for v in clash.values())
    assert "do not pick a side" in clash["note"]


def test_a_partial_overlap_is_still_a_conflict():
    clash = citeab.conflict(["WB", "IF"], ["WB"])
    assert clash["only_you"] == ["IF"]
    assert clash["only_citeab"] == []


# ── through the tool ─────────────────────────────────────────────────────────

_REAGENT = {"identifier": "ab212184", "target": "SNCA", "role": "primary",
            "figures": ["3b"], "applications": ["western blot"]}


def _row(res, antibody="ab212184"):
    rows = res["table"] + res["others"]["antibodies"]
    return [r for r in rows if r["antibody"] == antibody][0]


def test_without_a_paper_identifier_nothing_citeab_appears(snapshot):
    snapshot(applications=["IF"])
    res = portal.scan_controls(reagents=[_REAGENT])
    assert res["citeab"]["status"] == "not_asked"
    assert "application_conflict" not in _row(res)


def test_a_tissue_use_is_never_scored_against_the_cultured_cell_verdict():
    """The axis is the preparation, not the detection label.

    OGA's IF result is ICC-IF: cultured cells. Whether an epitope survives is a
    property of how the antigen is presented, so tissue is a different question
    and the IF verdict does not carry over to it.
    """
    facts = citeab.facts_for({"applications": ["WB"], "ambiguous": False,
                              "untested_terms": ["IHC"]},
                             portal._normalise_apps, portal._out_of_scope_apps,
                             portal._application_notes)
    assert "IHC" in facts["citeab_applications"], "CiteAb's own spelling is kept"
    assert facts["citeab_applications_as_oga_codes"] == ["WB"]
    assert "IF" not in facts["citeab_applications_as_oga_codes"]
    assert facts["citeab_applications_without_oga_verdict"] == ["IHC"]


def test_ihc_if_is_tissue_despite_ending_in_if():
    """The one that catches people out."""
    facts = citeab.facts_for({"applications": [], "ambiguous": False,
                              "untested_terms": ["IHC-IF"]},
                             portal._normalise_apps, portal._out_of_scope_apps,
                             portal._application_notes)
    assert facts["citeab_applications_as_oga_codes"] == []
    assert facts["citeab_applications_without_oga_verdict"] == ["IHC-IF"]
    assert "IHC-IF is tissue" in facts["citeab_no_verdict_reason"]


def test_icc_if_is_cells_and_does_count_for_the_if_verdict():
    """The other side of the same line: ICC-IF is what OGA actually tests."""
    facts = citeab.facts_for({"applications": ["IF"], "ambiguous": False,
                              "untested_terms": []},
                             portal._normalise_apps, portal._out_of_scope_apps,
                             portal._application_notes)
    assert facts["citeab_applications_as_oga_codes"] == ["IF"]
    assert "citeab_applications_without_oga_verdict" not in facts


def test_a_term_with_no_oga_verdict_anywhere_is_named_as_such():
    facts = citeab.facts_for({"applications": [], "ambiguous": False,
                              "untested_terms": ["ChIP"]},
                             portal._normalise_apps, portal._out_of_scope_apps,
                             portal._application_notes)
    assert facts["citeab_applications_without_oga_verdict"] == ["ChIP"]


def test_a_link_with_no_application_is_its_own_statement():
    """The 23% case: recorded as citing it, with no application recorded."""
    facts = citeab.facts_for({"applications": [], "ambiguous": False,
                              "untested_terms": []},
                             portal._normalise_apps, portal._out_of_scope_apps,
                             portal._application_notes)
    assert facts["citeab_link_recorded_without_application"] is True


def test_an_ambiguous_term_is_reported_and_not_applied():
    facts = citeab.facts_for({"applications": ["IF"], "ambiguous": True,
                              "untested_terms": []},
                             portal._normalise_apps, portal._out_of_scope_apps,
                             portal._application_notes)
    assert "cultured cells only" in facts["citeab_application_ambiguous"]


# ── the two paths must answer one question the same way ──────────────────────

_ONE_RULE = [
    # term            in-scope codes   recognised, no verdict
    ("WB",            ["WB"],          False),
    ("ICC",           ["IF"],          False),
    ("ICC-IF",        ["IF"],          False),   # cells: what OGA tests
    ("IHC",           [],              True),    # tissue
    ("IHC-P",         [],              True),
    ("IHC-Fr",        [],              True),
    ("IHC-IF",        [],              True),    # tissue, despite the name
    ("ChIP",          [],              False),   # never assessed at all
]


def test_a_term_means_the_same_thing_whichever_side_it_arrives_from():
    """The invariant behind "the tools should match".

    An application reported by the model and the same application recorded by
    CiteAb must be placed identically. They used to differ on exactly one term —
    IHC, folded onto IF from the caller and given no verdict from CiteAb — and a
    row could carry both answers in adjacent columns with nothing saying why.

    Both sides now go through `portal._normalise_apps` and
    `portal._out_of_scope_apps`, so this holds by construction; the test is here
    because a future alias added to one path and not the other would be silent.
    """
    for term, codes, recognised_without_verdict in _ONE_RULE:
        # the caller's side
        assert sorted(portal._normalise_apps([term])) == codes, term
        assert bool(portal._out_of_scope_apps([term])) is recognised_without_verdict, term

        # CiteAb's side, for a term its own map could not place
        facts = citeab.facts_for(
            {"applications": [], "ambiguous": False, "untested_terms": [term]},
            portal._normalise_apps, portal._out_of_scope_apps,
            portal._application_notes)
        assert facts["citeab_applications_as_oga_codes"] == codes, term
        if not codes:
            assert facts["citeab_applications_without_oga_verdict"] == [term], term


def test_a_term_with_no_verdict_is_answered_with_context_not_silence():
    facts = citeab.facts_for(
        {"applications": [], "ambiguous": False, "untested_terms": ["IHC"]},
        portal._normalise_apps, portal._out_of_scope_apps,
        portal._application_notes)
    reason = facts["citeab_no_verdict_reason"]
    assert "IHC-IF is tissue despite its name" in reason
    assert "`oga_result`" in reason, "the context has to be pointed at"
    assert "not a result in the application" in reason


def test_an_application_oga_never_assesses_says_so_differently_from_tissue():
    """ChIP is not 'untested'; there was never a result to be had."""
    facts = citeab.facts_for(
        {"applications": [], "ambiguous": False, "untested_terms": ["ChIP"]},
        portal._normalise_apps, portal._out_of_scope_apps,
        portal._application_notes)
    assert "does not assess these applications at all" in facts["citeab_no_verdict_reason"]
    assert "not 'untested'" in facts["citeab_no_verdict_reason"]
