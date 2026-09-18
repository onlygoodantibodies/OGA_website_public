"""scan_controls — the controls-assessment surface.

The caller reads the paper and reports what it found; the server resolves the
reagents against the dataset and decides what each control TYPE counts as. There
is no server-side text parsing left, so what is defended here is the division of
responsibility:

  * the caller's ``role`` keeps non-primary reagents out of the antibody table;
  * the caller's ``performed_in_paper`` keeps a merely-cited control out of the
    figures;
  * a selectivity control only counts for the antibody whose target it removes;
  * the CLASS of a control is the server's to decide — a caller cannot enter a
    peptide block as evidence of selectivity, however it labels it.
"""
from __future__ import annotations

from mcp_servers.common import portal


# ── the table: only rows that have something to say ──────────────────────────

#: The legends a fixture's figures live in. `absent` is a claim about the whole
#: paper, so it is earned by saying which legends were read — not by a payload
#: that merely proves one was opened. A fixture wanting a real `absent` has to
#: answer that question like any other caller.
_LEGENDS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"]


def test_table_row_shape_for_tested_antibody():
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "figures": ["1a"]}])
    row = [r for r in res["table"] if r["antibody"] == "ab212184"][0]
    assert row["oga_tested"] == "yes"
    assert row["oga_result"]
    assert row["gene_page"]
    assert set(row) >= {"antibody", "target", "paper_control", "control_figures",
                        "oga_tested", "oga_result", "image", "gene_page"}


def test_untested_uncontrolled_reagent_goes_to_others_not_table():
    res = portal.scan_controls(reagents=[
        {"identifier": "ZZ99999", "target": "NOTAGENE", "role": "primary"}])
    assert [r["antibody"] for r in res["others"]["antibodies"]] == ["ZZ99999"]
    assert "ZZ99999" not in {r["antibody"] for r in res["table"]}


def test_table_keeps_only_rows_that_matter():
    res = portal.scan_controls(
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary"},
                  {"identifier": "ZZ99999", "target": "NOTAGENE", "role": "primary"}],
        controls=[])
    assert {r["antibody"] for r in res["table"]} == {"ab212184"}      # OGA data
    assert {r["antibody"] for r in res["others"]["antibodies"]} == {"ZZ99999"}


def test_not_recommended_antibody_still_surfaced():
    res = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary",
         "figures": ["3a"]}])
    notrec = {h["identifier"].lower()
              for h in res["antibody_hits"]["not_recommended"]}
    assert "2642" in notrec


def test_focus_surfaces_concern_and_is_empty_when_none():
    concern = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary"}])
    assert concern["focus"]["antibodies_of_concern"]
    clean = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary"}])
    assert clean["focus"]["antibodies_of_concern"] == []


# ── role: what a regex could never determine ─────────────────────────────────

def test_isotype_control_never_reaches_the_antibody_results():
    res = portal.scan_controls(reagents=[
        {"identifier": "M5909", "role": "isotype_control"},
        {"identifier": "X0933", "role": "isotype_control"},
        {"identifier": "F0313", "role": "secondary"},
        {"identifier": "ab212184", "target": "SNCA", "role": "primary"},
    ])
    seen = ({r["antibody"] for r in res["table"]}
            | {r["antibody"] for r in res["others"]["antibodies"]})
    assert seen == {"ab212184"}


def test_caller_target_is_kept_whole_for_an_untested_reagent():
    res = portal.scan_controls(reagents=[
        {"identifier": "AV35098", "target": "alpha-smooth muscle actin",
         "role": "primary"}])
    row = [r for r in res["others"]["antibodies"] + res["table"]
           if r["antibody"] == "AV35098"][0]
    assert row["target"] == "alpha-smooth muscle actin"


# ── provenance and target identity ───────────────────────────────────────────

def test_control_only_cited_by_the_paper_is_not_a_control_for_its_figures():
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["5a"]}],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["5a"],
                   "performed_in_paper": False,
                   "evidence": "SNCA knockout mice are viable [13]."}])
    row = [r for r in res["table"] if r["antibody"] == "ab212184"][0]
    assert row["paper_control"] == "no"
    assert res["focus"]["figures_with_controls"] == {}


def test_knockout_counts_only_for_an_antibody_against_that_same_gene():
    controls = [{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                 "performed_in_paper": True,
                 "evidence": "SNCA KO and WT lysates were immunoblotted (Fig 3b)."}]
    same = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["3b"]}], controls=controls)
    assert [r for r in same["table"]][0]["control_status"] == "demonstrated"

    # A knockout of SNCA is a control for anti-SNCA and NOTHING else. For the SYT1
    # antibody it is not merely unlinked — there is no genetic manipulation of SYT1
    # in this paper at all, which is `absent`.
    other = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[{"identifier": "NB120-1234", "target": "SYT1", "role": "primary",
                   "figures": ["3b"]}], controls=controls)
    assert [r for r in other["table"]][0]["control_status"] == "absent"
    assert [r for r in other["table"]][0]["paper_control"] == "no"


def test_selectivity_control_naming_no_gene_validates_nothing():
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["2a"]}],
        controls=[{"type": "knockout", "figures": ["2a"],
                   "performed_in_paper": True}])
    assert [r for r in res["table"]][0]["paper_control"] == "no"


# ── paper_control answers ONE question, and only selectivity can answer it ───
#
# Both false reports in the 100-pair benchmark were this, and there were no
# others: the server classified the control correctly in `controls[]` and then
# contradicted itself a few keys away, in the field the tool tells the reading
# model to render as-is.

def test_detection_control_alone_is_not_a_paper_control():
    """`10.1186/s12885-019-6013-6` — the only control shown is a positive
    control. Classed `detection`; `paper_control` said "yes"."""
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["4c"]}],
        controls=[{"type": "positive_control", "target": "SNCA",
                   "figures": ["4c"], "performed_in_paper": True}])
    row = [r for r in res["table"] if r["antibody"] == "ab212184"][0]
    assert row["paper_control"] == "no"
    assert row["control_figures"] == []
    # …but it is not thrown away: a detection control the paper really ran is
    # reported as what it is.
    assert row["other_controls"] == [
        {"figure": "4c", "type": "positive_control", "class": "detection"}]


def test_pseudo_controls_alone_are_not_a_paper_control():
    """`10.1186/s40478-015-0238-7` — peptide competition and secondary-only, plus
    a knockdown the paper only cites. `paper_control` said "yes"."""
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["2a", "2b"]}],
        controls=[
            {"type": "peptide_competition", "target": "SNCA", "figures": ["2a"],
             "performed_in_paper": True},
            {"type": "secondary_only", "figures": ["2b"],
             "performed_in_paper": True},
            {"type": "knockdown", "target": "SNCA", "figures": ["2a"],
             "performed_in_paper": False},
        ])
    row = [r for r in res["table"] if r["antibody"] == "ab212184"][0]
    assert row["paper_control"] == "no"
    assert {c["class"] for c in row["other_controls"]} == {"pseudo"}
    # The cited-only knockdown stays out entirely — it is not a control for any
    # figure in this paper, whatever its class.
    assert "knockdown" not in {c["type"] for c in row["other_controls"]}


def test_a_pseudo_control_earns_a_table_row_rather_than_vanishing():
    """Tightening `paper_control` must not move the classic mistake into
    `others`, whose line reads "no candidate control reported"."""
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[{"identifier": "ZZ99999", "target": "NOTAGENE",
                   "role": "primary", "figures": ["1a"]}],
        controls=[{"type": "peptide_competition", "target": "NOTAGENE",
                   "figures": ["1a"], "performed_in_paper": True}])
    row = [r for r in res["table"] if r["antibody"] == "ZZ99999"][0]
    assert row["paper_control"] == "no"
    assert row["other_controls"][0]["type"] == "peptide_competition"
    assert "ZZ99999" not in {r["antibody"] for r in res["others"]["antibodies"]}


def test_a_knockout_still_reads_as_a_paper_control():
    """The gate must not cost the case it exists to protect."""
    res = portal.scan_controls(
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["3b"]}],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True,
                   "evidence": "Western blot of SNCA-/- and WT lysates (Fig 3b)."},
                  {"type": "secondary_only", "figures": ["3b"],
                   "performed_in_paper": True}])
    row = [r for r in res["table"] if r["antibody"] == "ab212184"][0]
    assert row["control_status"] == "demonstrated"
    assert row["paper_control"] == "yes"
    assert row["control_figures"] == ["3b"]
    # The pseudo-control in the same figure is reported alongside, not merged in.
    assert row["other_controls"] == [
        {"figure": "3b", "type": "secondary_only", "class": "pseudo"}]


# ── linkage, not co-location (rubric v9) ─────────────────────────────────────
#
# Until v8 a control counted for an antibody only if their figure lists shared a
# string. A 99-paper blinded benchmark varied ONLY that rule over 72 scoreable
# pairs: intersecting figure lists gave sensitivity 0.696, panel-insensitive
# matching 0.913, and no figure test at all 1.000 — at an IDENTICAL specificity of
# 0.959 in all three. Co-location bought no discrimination and only destroyed
# sensitivity, because papers establish a reagent in one figure and use it in
# another as a matter of course.
#
# What it stood in for is real, and is a value of its own now: a paper can knock
# down gene X to study biology while the anti-X antibody is never tested against
# that knockdown.

_SNCA = {"identifier": "ab212184", "target": "SNCA", "role": "primary"}


def _status(res, antibody="ab212184"):
    return [r for r in res["table"] if r["antibody"] == antibody][0]


def test_a_control_in_a_different_figure_is_demonstrated_when_linkage_is_stated():
    """The structure the old gate marked as uncontrolled: establish the reagent in
    one figure, use it in another."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3f"])],
        controls=[{"type": "knockdown", "target": "SNCA", "figures": ["1a"],
                   "performed_in_paper": True,
                   "evidence": "Western blot of siSNCA and control lysates (Fig 1a)."}])
    row = _status(res)
    assert row["control_status"] == "demonstrated"
    assert row["paper_control"] == "yes"
    # the CONTROL's figure, not the antibody's — they are different strings now
    assert row["control_figures"] == ["1a"]
    assert "Fig 1a" in row["control_note"] and "Fig 3f" in row["control_note"]
    assert "Different figure" in row["control_note"]


def test_a_supplementary_control_with_use_in_a_main_figure_counts_fully():
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3F"])],
        controls=[{"type": "knockdown", "target": "SNCA", "figures": ["S1"],
                   "performed_in_paper": True,
                   "evidence": "Knockdown of SNCA confirmed by immunoblot (Fig S1)."}])
    row = _status(res)
    assert row["control_status"] == "demonstrated"
    assert row["controls"][0]["supplementary"] is True
    assert "supplementary" in row["control_note"]


def test_a_panel_letter_no_longer_decides_anything():
    """`S7a` vs `S7` was a hard miss under the old gate; so, silently, was `3B` vs
    `3b`, which was compared case-sensitively."""
    for control_fig, reagent_fig in (("S7", "S7a"), ("3b", "3B")):
        res = portal.scan_controls(
            reagents=[dict(_SNCA, figures=[reagent_fig])],
            controls=[{"type": "knockout", "target": "SNCA", "figures": [control_fig],
                       "performed_in_paper": True,
                       "evidence": "SNCA KO lysates were immunoblotted."}])
        row = _status(res)
        assert row["control_status"] == "demonstrated", (control_fig, reagent_fig)
        assert row["controls"][0]["same_figure"] is True, (control_fig, reagent_fig)


def test_a_reagent_whose_figures_were_never_reported_still_gets_its_control():
    """Structurally impossible under the old rule: no reagent figures meant no
    intersection, so `paper_control` could only ever be "no"."""
    res = portal.scan_controls(
        reagents=[_SNCA],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["2a"],
                   "performed_in_paper": True,
                   "evidence": "SNCA-/- cells were immunostained (Fig 2a)."}])
    assert _status(res)["control_status"] == "demonstrated"


def test_a_manipulation_with_no_stated_readout_is_unlinked_not_absent():
    """The honest third state. A knockdown IS in the paper; the text does not say
    this antibody was read out against it — which is what happens when the quoted
    evidence comes from Methods rather than the legend."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["4a"])],
        controls=[{"type": "knockdown", "target": "SNCA", "figures": ["1b"],
                   "performed_in_paper": True,
                   "evidence": "Cells were transfected with 20 nM siSNCA "
                               "(5'-GCAUGGUGUGGCAACAGUG-3') for 72 h."}])
    row = _status(res)
    assert row["control_status"] == "present_unlinked"
    assert row["paper_control"] == "unlinked"          # NOT folded into "no"
    assert "does not state" in row["control_note"] or \
           "does not say" in row["control_note"]
    assert row in [r for r in res["table"]]
    assert "ab212184" not in {r["antibody"] for r in res["others"]["antibodies"]}
    named = {r["antibody"] for r in res["focus"]["controls_present_unlinked"]}
    assert "ab212184" in named


def test_a_knockdown_confirmed_by_qpcr_only_is_not_demonstrated():
    """A knockdown confirmed by qPCR is a confirmed knockdown and says nothing
    about this antibody. It is still PRESENT — reporting it as absent would be the
    old false negative back again."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["4a"])],
        controls=[{"type": "knockdown", "target": "SNCA", "figures": ["S2"],
                   "performed_in_paper": True,
                   "evidence": "Knockdown efficiency was confirmed by RT-qPCR "
                               "(Fig S2)."}])
    row = _status(res)
    assert row["control_status"] == "present_unlinked"
    assert row["controls"][0]["non_antibody_readouts"] == ["qPCR"]
    assert row["controls"][0]["readout_applications"] == []
    assert "qPCR" in row["control_note"]


def test_a_knockout_of_a_different_gene_stays_absent_for_this_reagent():
    """Removing the figure gate must not loosen the target match: a knockout of
    gene X validates anti-X and nothing else."""
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[dict(_SNCA, figures=["3b"])],
        controls=[{"type": "knockout", "target": "MAPT", "figures": ["3b"],
                   "performed_in_paper": True,
                   "evidence": "MAPT-/- lysates were immunoblotted (Fig 3b)."}])
    row = _status(res)
    assert row["control_status"] == "absent"
    assert row["controls"] == []
    assert row["control_note"] is None


def test_a_cited_but_not_performed_manipulation_is_still_absent():
    """`performed_in_paper` is untouched — a knockout the paper cites in its
    discussion is not a control for anything in it, in any figure."""
    res = portal.scan_controls(legends_read=_LEGENDS, 
        reagents=[dict(_SNCA, figures=["5a"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["5a"],
                   "performed_in_paper": False,
                   "evidence": "SNCA knockout mice were western blotted in [13]."}])
    assert _status(res)["control_status"] == "absent"


def test_a_caller_supplied_readout_beats_scanning_the_evidence():
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3a"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["S4"],
                   "performed_in_paper": True, "readout": "immunofluorescence",
                   "detected_with": "ab212184",
                   "evidence": "SNCA-/- HAP1 cells (Fig S4)."}])
    row = _status(res)
    assert row["control_status"] == "demonstrated"
    assert row["controls"][0]["readout_applications"] == ["IF"]
    assert row["controls"][0]["readout_source"] == "caller"
    assert "ab212184" in row["control_note"]


def test_a_control_in_another_application_is_a_caveat_not_a_gate():
    """The hazard co-location half-caught: validated by western blot, used for
    IHC. It says so; it does not refuse."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3a"], applications=["IHC"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["S1"],
                   "performed_in_paper": True,
                   "evidence": "SNCA-/- lysates were immunoblotted (Fig S1)."}])
    row = _status(res)
    assert row["control_status"] == "demonstrated"       # not downgraded
    assert row["application_caveat"]
    # Names the paper's OWN term. It used to say "IF", because IHC was folded onto
    # that column -- and a caveat about the wrong application is worse than none,
    # since nothing on the row shows the substitution happened.
    assert "WB" in row["application_caveat"] and "IHC" in row["application_caveat"]


def test_no_caveat_when_the_applications_overlap():
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3a"], applications=["western blot"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["S1"],
                   "performed_in_paper": True,
                   "evidence": "SNCA-/- lysates were immunoblotted (Fig S1)."}])
    assert _status(res)["application_caveat"] is None


def test_a_demonstrated_verdict_never_claims_the_control_worked():
    """The server sees text, not images. Every candidate sends the reader to the
    panel and says what to check there."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3a"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["S1"],
                   "performed_in_paper": True,
                   "evidence": "SNCA-/- lysates were immunoblotted (Fig S1)."}])
    note = _status(res)["control_note"]
    assert "confirm the band disappears at the expected size" in note
    assert "Presence is not proof" in note
    for claim in ("signal was lost", "the antibody is validated", "works",
                  "confirmed specific"):
        assert claim not in note


def test_every_pseudo_control_type_leaves_the_verdict_absent():
    """Peptide competition, secondary-only, isotype and vehicle remove or occupy
    the ANTIBODY, not the target. Dropping the figure gate must not promote them."""
    for ctype in ("peptide_competition", "secondary_only", "isotype", "vehicle"):
        res = portal.scan_controls(legends_read=_LEGENDS, 
            reagents=[dict(_SNCA, figures=["2a"])],
            controls=[{"type": ctype, "target": "SNCA", "figures": ["2a"],
                       "performed_in_paper": True,
                       "evidence": "Immunoblot after pre-adsorption (Fig 2a)."}])
        row = _status(res)
        assert row["control_status"] == "absent", ctype
        assert row["controls"] == [], ctype
        # …and still reported as what it is, in its own column.
        assert row["other_controls"][0]["class"] == "pseudo", ctype


def test_detection_and_orthogonal_controls_never_set_the_verdict_either():
    for ctype, cls in (("overexpression", "detection"),
                       ("positive_control", "detection"),
                       ("recombinant", "detection"),
                       ("orthogonal", "orthogonal"),
                       ("mass_spectrometry", "orthogonal")):
        res = portal.scan_controls(legends_read=_LEGENDS, 
            reagents=[dict(_SNCA, figures=["2a"])],
            controls=[{"type": ctype, "target": "SNCA", "figures": ["2a"],
                       "performed_in_paper": True,
                       "evidence": "Immunoblot of the overexpression lysate."}])
        row = _status(res)
        assert row["control_status"] == "absent", ctype
        assert row["other_controls"][0]["class"] == cls, ctype


def test_presence_and_linkage_are_reported_separately_never_blended():
    """The point of the change is to stop collapsing three questions into one. Both
    states appear in `focus` under their own names, and the row keeps both fields
    rather than a single score."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3a"]),
                  {"identifier": "2642", "target": "SNCA", "role": "primary",
                   "figures": ["4a"]}],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["S1"],
                   "performed_in_paper": True,
                   "evidence": "SNCA-/- lysates were immunoblotted (Fig S1)."}])
    # One control, two anti-SNCA antibodies: both get it, and both the same way.
    assert res["counts"]["controls_demonstrated"] == 2
    assert res["counts"]["controls_present_unlinked"] == 0
    for row in res["table"]:
        assert set(row) >= {"control_status", "paper_control", "control_note",
                            "controls", "application_caveat"}
        assert "confidence" not in row and "score" not in row


def test_the_same_panel_is_linkage_evidence_even_with_nothing_quoted():
    """Co-location is a bad GATE and a good SIGNAL, and those are different uses.
    If the knockout panel and the antibody's use are one panel, that panel IS this
    antibody read out on manipulated material."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True}])
    row = _status(res)
    assert row["control_status"] == "demonstrated"
    assert row["controls"][0]["linkage_basis"] == ["same_panel"]
    assert "Same panel as the antibody's use" in row["control_note"]


def test_a_different_figure_with_nothing_quoted_is_still_unlinked():
    """The other half of the same rule: co-location is evidence when it fires and
    silence otherwise. A control two figures away with no quoted text says
    nothing about which antibody read it."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["S9"],
                   "performed_in_paper": True}])
    row = _status(res)
    assert row["control_status"] == "present_unlinked"
    assert row["controls"][0]["linkage_basis"] == []


def test_the_row_says_WHY_a_control_counts():
    """The route is what tells a reader how strong the claim is and where to look
    — the thing a person actually needs from a controls scan."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True, "detected_with": "ab212184",
                   "evidence": "Fig 3b. Immunoblot of SNCA-/- and WT lysates."}])
    assert _status(res)["controls"][0]["linkage_basis"] == [
        "named", "same_panel", "stated_readout"]


def test_a_control_read_out_with_a_DIFFERENT_antibody_is_not_linkage():
    """The hazard co-location stood in for, stated outright by the paper: the
    knockdown was blotted, but with somebody else's antibody. Negative evidence
    outranks every route, co-location included."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"]),
                  {"identifier": "2642", "target": "SNCA", "role": "primary",
                   "figures": ["3b"]}],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True, "detected_with": "2642",
                   "evidence": "Fig 3b. Immunoblot of SNCA-/- lysates with 2642."}])
    mine = _status(res, "ab212184")
    theirs = _status(res, "2642")
    assert mine["control_status"] == "present_unlinked"
    assert "detected with 2642" in mine["control_note"]
    # …and the antibody the paper DID name still gets it.
    assert theirs["control_status"] == "demonstrated"
    assert "named" in theirs["controls"][0]["linkage_basis"]


def test_an_unrecognised_detected_with_does_not_demote():
    """`detected_with: "an anti-SNCA antibody"` names no reagent we were told
    about, so it is neither evidence for nor against. Demoting on it would punish
    a caller for reporting more."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True,
                   "detected_with": "an anti-SNCA antibody"}])
    assert _status(res)["control_status"] == "demonstrated"


# ── the Methods name antibodies the figures never account for ────────────────

def test_an_antibody_no_figure_places_is_reported_as_unlocated():
    """A Methods section routinely lists reagents the figures do not account for —
    a supplementary panel naming no antibody, an assay with no image, a 'data not
    shown'. The antibody WAS used and nobody can say where, so its controls cannot
    be checked. That is a finding, not the paper showing no control."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"]),
                  {"identifier": "ZZ-METHODS-ONLY", "target": "SNCA",
                   "role": "primary"}],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True,
                   "evidence": "Fig 3b. Immunoblot of SNCA-/- and WT lysates."}])
    unlocated = {r["antibody"] for r in res["focus"]["uses_not_located"]}
    assert unlocated == {"ZZ-METHODS-ONLY"}
    assert res["counts"]["uses_not_located"] == 1
    limits = " ".join(res["coverage"]["limits"])
    assert "1 of 2 antibodies are named with no figure placing them" in limits


def test_nothing_is_called_unlocated_when_no_reagent_carried_figures():
    """The server cannot tell "the paper does not say" from "nobody looked", and
    guessing puts an accusation on whichever is wrong. With no figures anywhere,
    `coverage.limits` says the figures may not have been read — and nothing is
    singled out."""
    res = portal.scan_controls(reagents=[_SNCA, {"identifier": "2642",
                                                 "target": "SNCA", "role": "primary"}])
    assert res["focus"]["uses_not_located"] == []
    assert "only you can tell them apart" in " ".join(res["coverage"]["limits"])


def test_one_blot_does_not_credit_three_antibodies_silently():
    """The Methods name three anti-SNCA reagents; the control panel names none.
    One of them was on that blot. Reporting all three as demonstrated with nothing
    said is how a reader comes away believing three reagents were controlled by
    one experiment."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"]),
                  {"identifier": "2642", "target": "SNCA", "role": "primary"},
                  {"identifier": "ZZ-THIRD", "target": "SNCA", "role": "primary"}],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True,
                   "evidence": "Fig 3b. Immunoblot of SNCA-/- and WT lysates."}])
    # The one IN the control's panel is distinguished from its neighbours…
    assert _status(res)["linkage_ambiguity"] is None
    # …the two that rest on the stated readout alone are not.
    for other in ("2642", "ZZ-THIRD"):
        row = _status(res, other)
        assert row["control_status"] == "demonstrated"
        assert "3 antibodies in this paper target SNCA" in row["linkage_ambiguity"]


def test_a_named_reagent_needs_no_ambiguity_caveat():
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["4a"]),
                  {"identifier": "2642", "target": "SNCA", "role": "primary"}],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["S1"],
                   "performed_in_paper": True, "detected_with": "ab212184",
                   "evidence": "Fig S1. Immunoblot of SNCA-/- lysates."}])
    assert _status(res)["linkage_ambiguity"] is None


# ── which antibody, for what, and what does OGA say about THAT ────────────────

def test_the_row_says_what_the_paper_used_the_antibody_for():
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "applications": ["western blot", "immunofluorescence"]}])
    row = _status(res)
    assert row["applications_in_paper"] == ["IF", "WB"]


def test_a_tissue_use_gets_no_verdict_and_is_answered_with_context():
    """OGA tests ICC-IF, so every IF verdict is a cultured-cell result.

    Tissue used to be FOLDED onto that column with a note underneath. That traded
    one wrong answer for another: an epitope can be available in one preparation
    and not the other, so the number in the column was about a different
    experiment and a caveat does not make it right.

    The axis is antigen presentation, not the detection label — IHC and IHC-IF are
    both tissue and group together, and IHC-IF is the one that catches people out.
    """
    for term in ("IHC", "IHC-IF", "IHC-P", "immunohistofluorescence"):
        res = portal.scan_controls(reagents=[
            {"identifier": "ab212184", "target": "SNCA", "role": "primary",
             "applications": [term]}])
        row = _status(res)
        # Not scored against the cultured-cell verdict...
        assert row["applications_in_paper"] == [], term
        assert row["oga_result_for_paper_applications"] is None, term
        # ...and not dropped either: the row still says what the paper did.
        assert row["applications_without_oga_verdict"] == [term], term
        note = " ".join(row["application_notes"])
        assert "TISSUE" in note and "no verdict for tissue" in note, term
        assert "ICC-IF" in note and "cultured cells" in note, term
        # The context the owner asked for: what OGA DID find, clearly not a result
        # in this preparation.
        assert row["oga_result"], term
        assert "no verdict for" in row["oga_result_context"], term
        assert "NOT a result in the preparation" in row["oga_result_context"], term


def test_a_tissue_application_is_never_silently_dropped():
    """`IHC-IF`, `IHC-P` and `IHC-Fr` once matched no alias and were discarded, so
    the row could not say what the paper did — and the OGA verdict was then
    reported across all four applications, with a concern judged against
    applications the paper never used.

    They resolve to no VERDICT now, which is the correct answer, but they must
    still be RECOGNISED. Those are different states and this pins both.
    """
    for term in ("IHC-IF", "IHC-P", "IHC-Fr", "IF-IHC"):
        assert portal._normalise_apps([term]) == set(), term
        assert portal._out_of_scope_apps([term]) == [term], term
        # Recognised, so it must not be reported as a term we failed to read.
        notes = " ".join(portal._application_notes([term]))
        assert "not application terms this connector recognises" not in notes, term


def test_the_cell_side_of_the_line_still_scores(  ):
    """ICC-IF is what OGA tests, and `IHC-IF` ending in `IF` must not change that
    for either of them."""
    assert portal._normalise_apps(["ICC-IF"]) == {"IF"}
    assert portal._normalise_apps(["ICC"]) == {"IF"}
    assert portal._out_of_scope_apps(["ICC-IF"]) == []


def test_immunostaining_is_counted_but_the_preparation_is_asked_for():
    """"Immunostaining" names the assay and not the preparation, and preparation is
    the whole distinction. Both sides land on IF, so it is counted rather than
    dropped — and the row asks which it was rather than picking one. Same rule as a
    gene alias shared between two proteins: a resolver that cannot pick correctly
    must disclose, not guess."""
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "applications": ["immunostaining"]}])
    row = _status(res)
    assert row["applications_in_paper"] == ["IF"]
    assert row["oga_result_for_paper_applications"] == "recommended: IF"
    note = " ".join(row["application_notes"])
    assert "not the preparation" in note
    assert "tissue sections and on cultured cells alike" in note


def test_a_named_preparation_settles_it_and_the_question_is_not_asked_twice():
    for apps, expect_note in (
            (["immunostaining", "ICC-IF"], False),   # cells, settled
            (["immunostaining", "IHC"], True),       # tissue — the TISSUE note
            (["immunostaining"], True)):             # unstated — the ambiguity note
        res = portal.scan_controls(reagents=[
            {"identifier": "ab212184", "target": "SNCA", "role": "primary",
             "applications": apps}])
        note = " ".join(_status(res).get("application_notes") or [])
        assert bool(note) is expect_note, apps
        if apps == ["immunostaining", "IHC"]:
            assert "TISSUE" in note          # the specific term wins
            assert "not the preparation" not in note


def test_an_application_term_that_resolves_to_nothing_says_so():
    """A term this connector does not know must not vanish."""
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "applications": ["proximity ligation"]}])
    row = _status(res)
    assert row["applications_in_paper"] == []
    note = " ".join(row["application_notes"])
    assert '"proximity ligation"' in note
    assert "not counted in `applications_in_paper`" in note.lower()


def test_an_ordinary_application_carries_no_note():
    """A field that is present on every row reads as one that never works."""
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "applications": ["western blot", "immunofluorescence", "ICC-IF"]}])
    row = _status(res)
    assert row["applications_in_paper"] == ["IF", "WB"]
    assert "application_notes" not in row


def test_the_oga_verdict_is_reported_for_the_application_the_paper_used():
    """`oga_result` covers all four applications, which is the wrong denominator
    for judging the paper in hand."""
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "applications": ["FC"]}])              # ab212184 is WB/IP/IF, not FC
    row = _status(res)
    assert "recommended: WB/IP/IF" in row["oga_result"]
    assert row["oga_result_for_paper_applications"] == "not tested: FC"


def test_a_concern_is_scoped_to_the_application_the_paper_used():
    """The note has promised this scoping since the list was written; the code
    asked about any application, so an antibody the paper blotted with — where OGA
    recommends it for WB — was reported as undercutting the paper because of a
    verdict in an application the paper never used."""
    # ab212184: recommended WB/IP/IF, not tested FC. Used here for WB only.
    fine = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "applications": ["WB"]}])
    assert fine["focus"]["antibodies_of_concern"] == []

    # 2642: a published WB figure on a curated gene and no recommendation —
    # not_recommended for WB. Used for WB, so it IS a concern.
    real = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary",
         "applications": ["WB"]}])
    assert [r["antibody"] for r in real["focus"]["antibodies_of_concern"]] == ["2642"]
    assert real["table"][0]["oga_result_for_paper_applications"] == \
        "not recommended: WB"


def test_an_unscoped_concern_still_reports_any_application():
    """With no applications supplied there is nothing to scope to, and the old
    any-application reading is the honest fallback."""
    res = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary"}])
    assert res["focus"]["antibodies_of_concern"]
    assert res["table"][0]["oga_result_for_paper_applications"] is None


# ── the scan reports what it was GIVEN, not just what it concluded ───────────

def test_no_controls_reported_says_so_rather_than_asserting_the_paper_has_none():
    """`absent` on every row means "no genetic manipulation of this target
    anywhere in the paper". With nothing passed in, the server has no basis for
    that — and it looks identical to a paper that genuinely shows none."""
    res = portal.scan_controls(legends_read=_LEGENDS, reagents=[dict(_SNCA, figures=["3b"])])
    assert _status(res)["control_status"] == "absent"
    limits = " ".join(res["coverage"]["limits"])
    assert "NO controls were reported" in limits
    assert "not evidence the paper shows none" in limits
    assert res["coverage"]["controls"] == 0


def test_coverage_names_the_sections_that_were_not_read():
    res = portal.scan_controls(
        reagents=[_SNCA],                       # no figures, no applications
        controls=[{"type": "knockout", "target": "SNCA",
                   "performed_in_paper": True}])   # no figures, nothing quoted
    cov = res["coverage"]
    assert cov["reagents"] == 1 and cov["reagents_with_figures"] == 0
    assert cov["reagents_with_applications"] == 0
    assert cov["controls_with_quoted_evidence"] == 0
    limits = " ".join(cov["limits"])
    assert "No reagent carried `figures`" in limits
    assert "Methods" in limits                  # where applications are stated
    assert "Quote the panel's legend" in limits


#: What a caller that has answered the question looks like. Named rather than
#: inlined, because "well supplied" now includes having stated the principle, and
#: a fixture is the only place that definition is written down.
_BRIEFED = ("A selectivity control removes the target — a knockout, a knockdown "
            "with confirmed loss, a naturally null line — and shows the signal "
            "goes with it. A blocking peptide or an isotype control removes or "
            "occupies the antibody instead, so it tests nothing about the target.")


def test_a_well_supplied_scan_reports_no_limits():
    res = portal.scan_controls(what_shows_selectivity=_BRIEFED,
                               legends_read=_LEGENDS, 
        reagents=[dict(_SNCA, figures=["3b"], applications=["WB"])],
        controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                   "performed_in_paper": True,
                   "evidence": "Fig 3b. Immunoblot of SNCA-/- and WT lysates."}])
    assert res["coverage"]["limits"] == []


def test_only_a_performed_target_matched_genetic_control_can_leave_absent():
    """The specificity invariant, enumerated rather than sampled.

    Dropping the figure gate bought sensitivity; the brief's condition is that it
    costs no specificity, and specificity rests on ONE rule — a row may leave
    `absent` only when a performed, target-matched, genetic-pillar control exists.
    So walk the whole control vocabulary against a fixed reagent (every type ×
    right/wrong/absent target × performed or cited × same/different/no figure ×
    three evidence strings) and check nothing else can move it. A per-case test
    catches the case it names; this catches the ones nobody thought of.
    """
    from mcp_servers.common.controls_rubric import (CONTROL_CLASSES,
                                                    SELECTIVITY_CLASSES)
    reagent = {"identifier": "ab212184", "target": "SNCA", "role": "primary",
               "figures": ["3b"]}
    seen = {"absent": 0, "present_unlinked": 0, "demonstrated": 0}
    for ctype in list(CONTROL_CLASSES) + ["vibes_check"]:
        genetic = CONTROL_CLASSES.get(ctype) in SELECTIVITY_CLASSES
        for target in ("SNCA", "MAPT", None):
            for performed in (True, False):
                for figs in (["3b"], ["S9"], []):
                    for ev in (None, "Western blot of the lysates.",
                               "Confirmed by qPCR."):
                        control = {"type": ctype, "figures": figs,
                                   "performed_in_paper": performed, "evidence": ev}
                        if target:
                            control["target"] = target
                        res = portal.scan_controls(legends_read=_LEGENDS, reagents=[reagent],
                                                   controls=[control])
                        status = res["table"][0]["control_status"]
                        seen[status] += 1
                        case = (ctype, target, performed, tuple(figs), ev)
                        may_move = genetic and performed and target == "SNCA"
                        if not may_move:
                            assert status == "absent", case
                        if status == "demonstrated":
                            # …and only ever with a linkage ROUTE behind it: the
                            # panel the antibody is used in, or a stated readout.
                            linked = figs == ["3b"] or (ev and "blot" in ev)
                            assert may_move and linked, case
    # …and the enumeration really did exercise all three, so a bug that made
    # everything `absent` could not pass this test by doing nothing.
    assert all(seen.values()), seen


# ── the class of a control is the server's call, not the caller's ────────────

def test_server_classifies_control_types_and_ignores_a_caller_supplied_class():
    res = portal.scan_controls(
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary"}],
        controls=[
            # A caller insisting a peptide block proves selectivity must not win.
            {"type": "peptide_competition", "control_class": "selectivity",
             "target": "SNCA", "figures": ["1a"], "performed_in_paper": True},
            {"type": "isotype", "figures": ["1b"], "performed_in_paper": True},
            {"type": "knockout", "target": "SNCA", "figures": ["3b"],
             "performed_in_paper": True},
        ])
    by_type = {c["type"]: c["control_class"] for c in res["controls"]}
    assert by_type["peptide_competition"] == "pseudo"
    assert by_type["isotype"] == "pseudo"
    assert by_type["knockout"] == "selectivity"


def test_unknown_control_type_is_kept_but_not_promoted():
    res = portal.scan_controls(
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary"}],
        controls=[{"type": "vibes_check", "figures": ["1a"],
                   "performed_in_paper": True}])
    assert res["controls"][0]["control_class"] == "unclassified"


# ── a spelling the server does not know is ITS gap, not the reader's ─────────
#
# The classes are a fixed property of the method and stay non-negotiable. What
# the reader calls them is not: a model writing what the paper printed -- "no
# primary antibody", "pre-absorption" -- is reading correctly, and landing that
# in `unclassified` loses the one sentence worth saying about it.

def test_the_words_papers_actually_print_are_classified():
    res = portal.scan_controls(
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["3a"]}],
        controls=[
            {"type": "no primary antibody", "figures": ["1a"]},
            {"type": "pre-absorption", "target": "SNCA", "figures": ["1b"]},
            {"type": "siRNA", "target": "SNCA", "figures": ["2a"]},
        ])
    filed = {c["type"]: c["control_class"] for c in res["controls"]}
    assert filed["secondary_only"] == "pseudo"
    assert filed["peptide_competition"] == "pseudo"
    # …and a knockdown reported as `siRNA` still tests selectivity, which is the
    # direction that would have COST the reader something.
    assert filed["knockdown"] == "selectivity"
    reported = {c["type"]: c["type_as_reported"] for c in res["controls"]}
    assert reported["secondary_only"] == "no_primary_antibody"


def test_an_unplaceable_type_is_said_out_loud_rather_than_dropped():
    """`unclassified` is safe -- it can never set a verdict -- but silence is the
    wrong kind of safe. Two halves: the caller is TOLD, and the control survives
    even though it names no target and shares no figure with the antibody, which
    is exactly the shape a pseudo-control arrives in."""
    res = portal.scan_controls(
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary",
                   "figures": ["3a"]}],
        controls=[{"type": "vibes_check", "figures": ["1a"]}])
    limits = " ".join(res["coverage"]["limits"]).lower()
    assert "vibes_check" in limits
    assert "not been counted as a selectivity control" in limits
    others = [o for r in res["table"] for o in r.get("other_controls", [])]
    assert any(o["type"] == "vibes_check" for o in others), others


def test_an_ambiguous_spelling_is_asked_about_rather_than_guessed():
    """`kd` is knockdown to a reader and the dissociation constant to a
    biochemist, and this surface has shipped that defect once already."""
    res = portal.scan_controls(
        reagents=[{"identifier": "ab212184", "target": "SNCA", "role": "primary"}],
        controls=[{"type": "kd", "target": "SNCA", "figures": ["1a"]}])
    assert res["controls"][0]["control_class"] == "unclassified"
    assert "kd" in " ".join(res["coverage"]["limits"]).lower()


# ── the rubric ships with the data it applies to ─────────────────────────────

def test_rubric_is_returned_inline_so_no_prompt_must_be_pasted():
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary"}])
    assert "pseudo-control" in res["rubric"]["text"].lower()
    assert res["rubric"]["version"]


def test_two_axes_note_and_untested_framing():
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary"}])
    note = res["note"].lower()
    assert "two independent axes" in note
    assert "untested" in note


def test_empty_call_is_safe():
    res = portal.scan_controls(reagents=[])
    assert res["table"] == [] and res["others"]["count"] == 0


# ── a damaging verdict must always point at the evidence ─────────────────────
# If a row says an antibody was tested and NOT recommended, that can undercut a
# claim in the paper being read. The reader has to be able to check it: the
# characterisation figure, the peer-reviewed report, and the gene page must travel with
# the row, not sit in a nested array the caller was told not to render.

def test_not_recommended_row_carries_image_report_doi_and_gene_page():
    res = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary",
         "figures": ["3a"]}])
    row = [r for r in res["table"] if r["antibody"] == "2642"][0]
    assert "not recommended" in row["oga_result"]
    assert row["image"] and row["image"].startswith("http")
    assert row["images"]
    assert row["gene_page"] and row["gene_page"].startswith("http")
    assert row["report_dois"], "a failing verdict must cite its published report"


def test_antibodies_of_concern_carry_the_same_evidence_links():
    res = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary"}])
    concern = res["focus"]["antibodies_of_concern"][0]
    assert concern["image"] and concern["gene_page"] and concern["report_dois"]


def test_recommended_row_points_at_the_evidence_too():
    # Not only negatives — a positive verdict is also a claim the reader may want
    # to check.
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary"}])
    row = res["table"][0]
    assert row["image"] and row["gene_page"] and row["report_dois"]


# ── untested reagent with tested alternatives earns a table row ──────────────

def test_untested_reagent_with_characterised_target_is_promoted_out_of_others():
    res = portal.scan_controls(reagents=[
        {"identifier": "ZZ-UNTESTED-1", "target": "SNCA", "role": "primary",
         "figures": ["2a"]}])
    row = [r for r in res["table"] if r["antibody"] == "ZZ-UNTESTED-1"]
    assert row, "should be in the table, not collapsed into others"
    assert row[0]["gene_page"].endswith("/antibodies/SNCA/")
    assert row[0]["target_is_characterised"] is True
    assert res["others"]["count"] == 0
    assert res["focus"]["untested_with_characterised_alternatives"]
    assert res["characterised_alternatives"]["SNCA"]


def test_a_table_row_can_key_into_characterised_alternatives():
    # The row is what the caller is told to render, and it is told to name the
    # alternatives for that gene. Its `target` is the caller's raw label, which is
    # not a key into `characterised_alternatives` — so the row carries the resolved
    # `gene` that is.
    res = portal.scan_controls(reagents=[
        {"identifier": "ZZ-UNTESTED-1", "target": "SQSTM1/p62", "role": "primary"}])
    row = [r for r in res["table"] if r["antibody"] == "ZZ-UNTESTED-1"][0]
    assert row["target"] == "SQSTM1/p62"                  # what the paper printed
    assert row["gene"] == "SQSTM1"                        # what it keys into
    assert res["characterised_alternatives"][row["gene"]]


def test_a_table_row_says_when_its_alternatives_were_shortened():
    res = portal.scan_controls(reagents=[
        {"identifier": "ZZ-UNTESTED-1", "target": "SQSTM1", "role": "primary"}])
    row = [r for r in res["table"] if r["antibody"] == "ZZ-UNTESTED-1"][0]
    assert row["recommended_alternatives"] == 7
    assert row["alternatives_listed"] == 5
    assert row["alternatives_truncated"] is True


def test_nothing_for_this_application_is_a_finding_not_an_offer_of_alternatives():
    # SYT1 is characterised for IF/FC and has nothing recommended for WB. The row
    # must still surface — "OGA worked on this gene, but has no WB antibody to
    # recommend" is what that reader needs — while staying OUT of the focus list the
    # note tells the caller to name alternatives from, because there are none.
    res = portal.scan_controls(reagents=[
        {"identifier": "ZZ-UNTESTED-1", "target": "SYT1", "role": "primary",
         "applications": ["WB"]}])
    row = [r for r in res["table"] if r["antibody"] == "ZZ-UNTESTED-1"]
    assert row, "a characterised target with no match is still a finding"
    assert row[0]["recommended_for_requested_applications"] == 0
    assert row[0]["alternatives_listed"] == 0
    named = {r["antibody"]
             for r in res["focus"]["untested_with_characterised_alternatives"]}
    assert "ZZ-UNTESTED-1" not in named
    assert "SYT1" not in res["characterised_alternatives"]


def test_untested_reagent_on_an_uncurated_target_stays_in_others():
    res = portal.scan_controls(reagents=[
        {"identifier": "ZZ-UNTESTED-3", "target": "QPRT", "role": "primary"}])
    assert [r["antibody"] for r in res["others"]["antibodies"]] == ["ZZ-UNTESTED-3"]
    assert res["focus"]["untested_with_characterised_alternatives"] == []


def test_image_application_codes_match_the_assessment_keys():
    # images were labelled "ICC-IF" while every verdict is keyed "IF", so anything
    # joining a figure to its verdict had to know the quirk.
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary"}])
    row = res["table"][0]
    hit = res["antibody_hits"]["recommended"][0]
    for img in row["images"]:
        assert img["application"] in hit["applications"], img
    assert any(i["application"] == "IF" and i["stored_as"] == "ICC-IF"
               for i in row["images"])


# ── the question asked before the scan ───────────────────────────────────────
#
# It primes and it sizes. It may only ever ADD support, so every assertion here
# is about what a poor answer GAINS, never about what a good one loses.

def test_the_answer_is_echoed_and_not_scored():
    res = portal.scan_controls(reagents=[dict(_SNCA, figures=["3b"])],
                               legends_read=_LEGENDS,
                               what_shows_selectivity=_BRIEFED)
    assert res["briefing"]["answer"] == _BRIEFED
    assert res["briefing"]["covers_selectivity"] is True
    assert "score" not in res["briefing"]


def test_no_answer_is_told_why_it_was_asked():
    res = portal.scan_controls(reagents=[dict(_SNCA, figures=["3b"])],
                               legends_read=_LEGENDS)
    assert res["briefing"]["answer"] is None
    assert res["briefing"]["covers_selectivity"] is False
    assert any("did not answer" in limit
               for limit in res["coverage"]["limits"])


def test_an_answer_naming_only_pseudo_controls_gets_more_help_not_less():
    """The commonest wrong model of a specificity control, and the reader the
    scaffold was written for."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"])], legends_read=_LEGENDS,
        what_shows_selectivity="Run a no-primary control and an isotype control.")
    assert res["briefing"]["covers_selectivity"] is False
    assert "not the target" in res["briefing"]["why"]


def test_the_briefing_never_touches_a_verdict():
    """It is the model's view of controls in general. Letting it reach a verdict
    about THIS paper's controls would be opinion in the fact layer."""
    good, bad = {}, {}
    for name, answer in (("good", _BRIEFED), ("bad", "No idea, honestly.")):
        res = portal.scan_controls(
            reagents=[dict(_SNCA, figures=["3b"])], legends_read=_LEGENDS,
            what_shows_selectivity=answer,
            controls=[{"type": "knockout", "target": "SNCA", "figures": ["3b"],
                       "performed_in_paper": True}])
        (good if name == "good" else bad)["status"] = res["table"][0]["control_status"]
    assert good["status"] == bad["status"]


# ── the two halves of the rubric ─────────────────────────────────────────────
#
# The binds are every caller's, always: they are not rules if some callers get
# them and others do not. The scaffold is support, and support is sized to the
# reader — which can only ever mean sending MORE, never less than the binds.

def test_a_briefed_caller_gets_the_binds_and_is_told_where_the_rest_is():
    res = portal.scan_controls(reagents=[dict(_SNCA, figures=["3b"])],
                               legends_read=_LEGENDS,
                               what_shows_selectivity=_BRIEFED)
    rubric = res["rubric"]
    assert rubric["binds_only"] is True
    assert "peptide" in rubric["text"].lower()          # a bind, always present
    assert "how people usually find" not in rubric["text"].lower()
    # Never withheld — only not pushed.
    assert rubric["scaffold_tool"] == "controls_rubric"


def test_an_unbriefed_caller_gets_the_how_to_half_too():
    res = portal.scan_controls(reagents=[dict(_SNCA, figures=["3b"])],
                               legends_read=_LEGENDS)
    rubric = res["rubric"]
    assert rubric["binds_only"] is False
    assert "how people usually find" in rubric["text"].lower()


def test_a_control_the_server_could_not_place_pulls_the_scaffold_back_in():
    """The late signal that survives the briefing: said the right thing, then
    reported something the classifier could not file."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["3b"])], legends_read=_LEGENDS,
        what_shows_selectivity=_BRIEFED,
        controls=[{"type": "vibes_check", "figures": ["3b"]}])
    assert res["rubric"]["binds_only"] is False
    assert "could not place" in res["rubric"]["why"]


def test_the_binds_are_the_same_text_for_everyone():
    """A rule some callers get and others do not is not a rule."""
    from mcp_servers.common.controls_rubric import CONTROLS_BINDS
    for kwargs in ({}, {"what_shows_selectivity": _BRIEFED},
                   {"what_shows_selectivity": "no idea"}):
        res = portal.scan_controls(reagents=[dict(_SNCA, figures=["3b"])],
                                   legends_read=_LEGENDS, **kwargs)
        assert res["rubric"]["text"].startswith(CONTROLS_BINDS), kwargs


# ── "go and check here" must mean one thing ──────────────────────────────────
#
# Found on two real papers within an hour of each other, both `demonstrated`:
# an RNAi knockdown scored by counting dendrites, and a knockout-mouse IHC,
# each listed in `control_figures` beside the western blot that actually
# carried the linkage. The detail was right in `controls[]` all along; the
# column the table renders was what sent a reader to a panel with no band in it.

def test_control_figures_lists_only_where_this_antibody_can_be_checked():
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["2c"], applications=["WB"])],
        legends_read=_LEGENDS, what_shows_selectivity=_BRIEFED,
        controls=[
            # Linked: same panel as the antibody's use, read out by WB.
            {"type": "knockout", "target": "SNCA", "figures": ["2c"],
             "performed_in_paper": True, "readout": "WB",
             "detected_with": "ab212184",
             "evidence": "Fig 2c. Immunoblot of SNCA-/- and WT lysates."},
            # Unlinked: the same gene manipulated elsewhere, scored by something
            # that is not an antibody at all.
            {"type": "knockdown", "target": "SNCA", "figures": ["3", "4"],
             "performed_in_paper": True,
             "evidence": "siRNA against SNCA; the readout was dendrite number."},
        ])
    row = res["table"][0]
    assert row["control_status"] == "demonstrated"
    assert row["control_figures"] == ["2c"], row["control_figures"]
    # Not dropped — a manipulation elsewhere in the paper is real information,
    # and a count with no list under it invents the noun.
    assert row["control_figures_unlinked"] == ["3", "4"]
    assert "different experiment" in row["control_note"]
    assert "Fig 3, Fig 4" in row["control_note"]


def test_an_unlinked_row_keeps_the_only_figures_it_has():
    """`present_unlinked` has nothing linked by definition, so the same split
    would empty the one column that row exists to point at."""
    res = portal.scan_controls(
        reagents=[dict(_SNCA, figures=["2c"], applications=["WB"])],
        legends_read=_LEGENDS, what_shows_selectivity=_BRIEFED,
        controls=[{"type": "knockdown", "target": "SNCA", "figures": ["6a"],
                   "performed_in_paper": True,
                   "evidence": "SNCA mRNA is reduced by 91% in the stable line."}])
    row = res["table"][0]
    assert row["control_status"] == "present_unlinked"
    assert row["control_figures"] == ["6a"]
    assert row["control_figures_unlinked"] == []


# ── the swap for a reagent the data does not support ─────────────────────────

def test_a_concern_carries_the_alternatives_on_its_row_and_in_focus():
    """`antibodies_of_concern` named the problem and offered no way out of it.

    The reader here is not waiting on evidence — they have it, and it is against
    the reagent in their hands. That is the sharpest case in the reply and it was
    the one with nowhere to go next: the pointer reached untested reagents only.
    """
    res = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary",
         "applications": ["WB"], "figures": ["1a"]}])
    assert [r["antibody"] for r in res["focus"]["antibodies_of_concern"]] == ["2642"]
    swap = res["focus"]["unsupported_with_characterised_alternatives"]
    assert [r["antibody"] for r in swap] == ["2642"]
    # The table is the only part the caller renders as-is, so the fields have to be
    # ON the row — not only in `antibody_hits`, where nothing would show them.
    row = [r for r in res["table"] if r["antibody"] == "2642"][0]
    assert row["alternatives_listed"] >= 1
    assert row["gene_page"]
    assert "ab212184" in {a["antibody"]
                          for a in res["characterised_alternatives"]["SNCA"]}


def test_a_supported_antibody_gets_no_swap_offered():
    res = portal.scan_controls(reagents=[
        {"identifier": "ab212184", "target": "SNCA", "role": "primary",
         "applications": ["WB"]}])
    assert res["focus"]["unsupported_with_characterised_alternatives"] == []
    assert res["characterised_alternatives"] == {}


def test_the_note_tells_the_caller_to_render_the_swap():
    """A field computed and never drawn is the same bug as one never computed."""
    res = portal.scan_controls(reagents=[
        {"identifier": "2642", "target": "SNCA", "role": "primary",
         "applications": ["WB"]}])
    assert "unsupported_with_characterised_alternatives" in res["note"]
