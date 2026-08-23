"""`sections_read`: the declaration, the cross-check, and the withheld verdict.

What is pinned here is the ASYMMETRY. A model asked whether it read something is
free to say yes, so the declaration is allowed to remove an `absent` and never to
grant one. Get that backwards and the feature becomes a way to talk the server
into an accusation, which is worse than not having asked at all.

The second thing pinned is that only `absent` is withheld. `demonstrated` and
`present_unlinked` are findings — a paper that shows a knockout still shows it
however little of it was read — and withholding those would punish a caller for
declaring honestly and teach it to stop.
"""
from mcp_servers.common import portal, sections


# ── the declaration ──────────────────────────────────────────────────────────

#: The legends a fixture's figures live in. `absent` is a claim about the whole
#: paper, so it is earned by saying which legends were read — not by a payload
#: that merely proves one was opened. A fixture wanting a real `absent` has to
#: answer that question like any other caller.
_LEGENDS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"]


def test_nothing_declared_is_none_and_not_an_empty_list():
    """"Did not answer" and "read nothing" are different statements."""
    assert sections.normalise(None) == (None, [])
    assert sections.normalise([]) == ([], [])


def test_free_text_lands_on_the_vocabulary():
    declared, unknown = sections.normalise(
        ["Materials and Methods", "figure legends", "Extended Data"])
    assert declared == ["methods", "figure_legends", "supplementary"]
    assert unknown == []


def test_full_text_means_all_of_them():
    declared, _ = sections.normalise(["full text"])
    assert set(declared) == set(sections.SECTIONS)


def test_an_unrecognised_term_is_named_back_and_not_silently_dropped():
    declared, unknown = sections.normalise(["methods", "the pictures"])
    assert declared == ["methods"]
    assert unknown == ["the pictures"]


# ── what the payload proves, which is the half that cannot be talked into it ──

def test_an_identified_reagent_evidences_the_methods():
    assert sections.evidenced([{"identifier": "ab1"}], []) == {"methods"}


def test_a_figure_on_a_reagent_evidences_the_legends():
    found = sections.evidenced([{"identifier": "ab1", "figures": ["3b"]}], [])
    assert found == {"methods", "figure_legends"}


def test_the_servers_own_scrape_does_not_evidence_the_legends():
    """The load-bearing exclusion.

    A control whose readout the SERVER pulled out of quoted text says only that
    the server read the quote. Counting it would let the server certify its own
    inference as the caller's observation.
    """
    scraped = sections.evidenced([{"identifier": "ab1"}],
                                 [{"readout_source": "evidence_text"}])
    stated = sections.evidenced([{"identifier": "ab1"}],
                                [{"readout_source": "caller"}])
    assert "figure_legends" not in scraped
    assert "figure_legends" in stated


# ── the asymmetry ────────────────────────────────────────────────────────────

def test_declaring_cannot_buy_an_absent_the_payload_has_not_earned():
    assert sections.absent_is_serveable(
        ["methods", "figure_legends", "supplementary"], set()) is False


def test_declaring_narrowly_withholds_an_absent_the_payload_would_allow():
    payload = {"methods", "figure_legends"}
    assert sections.absent_is_serveable(None, payload) is True
    assert sections.absent_is_serveable(["methods"], payload) is False


def test_not_declaring_at_all_leaves_the_payload_to_decide():
    """Every caller that predates this parameter must be unaffected."""
    assert sections.absent_is_serveable(None, {"methods", "figure_legends"}) is True
    assert sections.absent_is_serveable(None, {"methods"}) is False


# ── contradictions, named and not resolved ───────────────────────────────────

def test_claiming_the_legends_while_carrying_nothing_from_them_is_named():
    notes = sections.contradictions(
        ["methods", "figure_legends"], {"methods"},
        {"reagents": 1, "reagents_with_figures": 0,
         "controls_with_figures": 0, "controls_with_quoted_evidence": 0})
    assert any("declared reading the figure legends" in n for n in notes)


def test_the_other_direction_is_named_too():
    """Payload richer than the declaration: the declaration is incomplete."""
    notes = sections.contradictions(
        ["methods"], {"methods", "figure_legends"},
        {"reagents": 1, "reagents_with_figures": 1,
         "controls_with_figures": 0, "controls_with_quoted_evidence": 0})
    assert any("does not list it" in n for n in notes)


def test_no_declaration_produces_no_contradictions():
    assert sections.contradictions(None, {"methods"}, {"reagents": 1}) == []


# ── end to end through scan_controls ─────────────────────────────────────────

_REAGENT = {"identifier": "ab212184", "target": "SNCA", "role": "primary"}


def _row(res, antibody="ab212184"):
    rows = res["table"] + res["others"]["antibodies"]
    return [r for r in rows if r["antibody"] == antibody][0]


def test_methods_only_gets_not_assessed_rather_than_absent():
    """The failure this whole surface exists to stop.

    No figures on the reagent, no controls at all: the server has no basis for
    saying the paper shows no genetic control, and must not say it.
    """
    res = portal.scan_controls(reagents=[_REAGENT])
    row = _row(res)
    assert row["control_status"] == "not_assessed"
    assert row["paper_control"] != "no"
    assert row["control_note"], "a withheld row must explain itself"
    assert res["coverage"]["sections"]["absent_withheld"] is True
    assert any("NO ROW IN THIS REPLY CAN REPORT `absent`" in limit
               for limit in res["coverage"]["limits"])


def test_a_figure_on_the_reagent_earns_a_real_absent():
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[dict(_REAGENT, figures=["3b"])],
        sections_read=["methods", "figure_legends", "supplementary"])
    row = _row(res)
    assert row["control_status"] == "absent"
    assert row["paper_control"] == "no"
    assert res["coverage"]["sections"]["absent_withheld"] is False


def test_a_narrow_declaration_withholds_an_absent_the_payload_allows():
    res = portal.scan_controls(reagents=[dict(_REAGENT, figures=["3b"])],
                               sections_read=["methods"])
    assert _row(res)["control_status"] == "not_assessed"


def test_a_demonstrated_control_is_never_withheld():
    """Findings are served whatever was read; only the accusation is withheld."""
    res = portal.scan_controls(
        reagents=[dict(_REAGENT, figures=["5a"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["5a"],
                   "performed_in_paper": True,
                   "evidence": "SNCA KO and WT lysates blotted in 5a."}],
        sections_read=["methods"])          # deliberately narrow
    assert _row(res)["control_status"] == "demonstrated"


def test_a_withheld_row_earns_no_place_in_the_table_on_its_own():
    """`not_assessed` asserts nothing, so it is not a finding to report.

    Uses a reagent OGA has nothing on, against a target nobody has characterised:
    a row like this is in `others` when the status is `absent`, and must stay
    there when it is `not_assessed`. (An antibody OGA HAS tested still earns its
    row — on the strength of the verdict, which is a database fact and nothing to
    do with what was read.)
    """
    unknown = {"identifier": "ZZ-UNTESTED-4", "target": "NOTAGENE",
               "role": "primary"}
    withheld = portal.scan_controls(legends_read=_LEGENDS, reagents=[unknown])
    served = portal.scan_controls(legends_read=_LEGENDS, reagents=[dict(unknown, figures=["3b"])])

    # Both land in `others`, which carries no verdict at all -- that IS the
    # assertion: withholding did not promote a non-finding into the table.
    assert withheld["coverage"]["sections"]["absent_withheld"] is True
    assert served["coverage"]["sections"]["absent_withheld"] is False
    for result in (withheld, served):
        assert "ZZ-UNTESTED-4" not in [r["antibody"] for r in result["table"]]
        assert "ZZ-UNTESTED-4" in [r["antibody"]
                                   for r in result["others"]["antibodies"]]
    assert withheld["counts"]["controls_not_assessed"] == 1
    assert withheld["counts"]["controls_absent"] == 0


def test_withheld_rows_are_counted_apart_from_absent_ones():
    res = portal.scan_controls(reagents=[_REAGENT])
    assert res["counts"]["controls_absent"] == 0
    assert res["counts"]["controls_not_assessed"] >= 1


def test_the_supplement_being_skipped_caveats_an_absent_without_withholding_it():
    res = portal.scan_controls(legends_read=_LEGENDS, reagents=[dict(_REAGENT, figures=["3b"])],
                               sections_read=["methods", "figure_legends"])
    assert _row(res)["control_status"] == "absent"
    assert any("supplement was not among the sections read" in limit
               for limit in res["coverage"]["limits"])


def test_the_reply_echoes_what_the_server_parsed():
    """A mistyped declaration is discarded without error by the tool layer, so
    the caller needs to see what actually arrived."""
    res = portal.scan_controls(reagents=[dict(_REAGENT, figures=["3b"])],
                               sections_read=["methods", "legends", "nonsense"])
    block = res["coverage"]["sections"]
    assert block["declared"] == ["methods", "figure_legends"]
    assert block["unrecognised"] == ["nonsense"]
    assert any("not recognised" in limit for limit in res["coverage"]["limits"])


def test_a_caller_that_does_not_enumerate_loses_absent_and_nothing_else():
    """The compatibility promise, restated where `legends_read` moved it.

    `sections_read` was purely additive: a caller that ignored it was unaffected.
    `legends_read` is not, and deliberately. `absent` says no genetic manipulation
    of this target appears ANYWHERE in the paper, and the old bar for it — one
    reagent carrying one figure — shows a legend was opened, never that the
    legends were finished. A caller that will not say which legends it read has
    not answered the question the verdict rests on, so it gets `not_assessed`:
    softer, true, and already a rendered state.

    What it does NOT lose is the point. Findings are served exactly as before —
    `demonstrated` and `present_unlinked` are things the paper shows, and a paper
    that shows a knockout shows it however little of it was read. Only the
    accusation is withheld, and the reply says in one line how to earn it back.
    """
    res = portal.scan_controls(reagents=[dict(_REAGENT, figures=["3b"])])
    assert _row(res)["control_status"] == "not_assessed"
    assert res["coverage"]["sections"]["declared"] is None
    assert res["coverage"]["legends"]["enumerated"] is False
    assert any("figure_legends_read" in limit for limit in res["coverage"]["limits"])


def test_enumerating_the_legends_earns_the_absent_back():
    res = portal.scan_controls(reagents=[dict(_REAGENT, figures=["3b"])],
                               legends_read=["1", "2", "3"])
    assert _row(res)["control_status"] == "absent"
    assert res["coverage"]["legends"]["complete"] is True


def test_a_finding_is_served_however_little_was_read():
    """The asymmetry that keeps a caller honest: withholding a FINDING would
    punish a caller for admitting it read three legends, and teach it to stop."""
    res = portal.scan_controls(
        reagents=[dict(_REAGENT, figures=["3b"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True}])
    assert _row(res)["control_status"] in ("demonstrated", "present_unlinked")


def test_a_figure_cited_whose_legend_was_not_read_is_named_and_withholds():
    """The check a yes/no cannot do: the caller placed something in Fig 4 while
    listing legends 1-3, which is visible without reading the paper."""
    res = portal.scan_controls(reagents=[dict(_REAGENT, figures=["4a"])],
                               legends_read=["1", "2", "3"])
    assert _row(res)["control_status"] == "not_assessed"
    assert res["coverage"]["legends"]["cited_but_not_read"] == ["4"]
    assert any("legends are not among the ones you listed" in limit
               for limit in res["coverage"]["limits"])


def test_a_gap_in_the_sequence_reads_as_a_truncated_source():
    res = portal.scan_controls(reagents=[dict(_REAGENT, figures=["1a"])],
                               legends_read=["1", "2", "5"])
    assert res["coverage"]["legends"]["gaps_in_sequence"] == [3, 4]
    assert _row(res)["control_status"] == "not_assessed"


def test_supplementary_numbering_is_never_read_as_a_gap():
    """S1, S2, S5 with no S3 is ordinary publishing, not a truncated read."""
    res = portal.scan_controls(reagents=[dict(_REAGENT, figures=["1a"])],
                               legends_read=["1", "S1", "S2", "S5"])
    assert res["coverage"]["legends"]["gaps_in_sequence"] == []
    assert _row(res)["control_status"] == "absent"
