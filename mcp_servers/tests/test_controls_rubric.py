"""The frozen controls rubric served as an MCP prompt — content + version pin.

The rubric is what makes the benchmark model-independent (every connected model
classifies against the SAME instructions), so its key sections and its version
tag must be present and self-consistent.
"""
from __future__ import annotations

from mcp_servers.common import controls_rubric as cr


def test_version_is_pinned_and_embedded():
    assert cr.CONTROLS_RUBRIC_VERSION
    # The version is rendered into the prompt header so a transcript records it.
    assert cr.CONTROLS_RUBRIC_VERSION in cr.CONTROLS_RUBRIC


def test_rubric_has_the_three_control_axes():
    body = cr.CONTROLS_RUBRIC.lower()
    assert "positive control" in body
    assert "negative control" in body
    assert "orthogonal" in body


def test_rubric_keeps_the_two_axes_separate():
    body = cr.CONTROLS_RUBRIC.lower()
    assert "two independent axes" in body
    # It must tell the model the OGA verdict is NOT its own judgement.
    assert "check_manuscript" in body or "scan_controls" in body


def test_rubric_states_the_knockout_specificity_rule():
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())   # unwrap line breaks
    assert "only for the anti-x antibody" in body


def test_rubric_defines_the_confidence_convention():
    assert "[AI — check needed]" in cr.CONTROLS_RUBRIC


def test_rubric_rejects_peptide_block_as_selectivity_control():
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())      # unwrap line breaks
    # Peptide competition must be named a pseudo-control that is NOT selectivity.
    assert "pseudo-control" in body
    assert "peptide" in body and "occupies the fab" in body
    # And secondary-only / isotype must also be rejected.
    assert "secondary-only" in body and "isotype" in body


def test_rubric_positive_control_is_detection_not_selectivity():
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    assert "detection" in body
    # overexpression/recombinant shows detection, not selectivity
    assert "overexpression" in body or "recombinant" in body
    assert "not that" in body and "selective" in body


def test_rubric_tells_reader_where_to_look_and_presence_is_not_proof():
    body = cr.CONTROLS_RUBRIC.lower()
    assert "presence is not proof" in body
    # the model locates and points; the reader evaluates the figure
    assert "point the reader" in body
    assert "not the final arbiter" in body


def _delivered_output_rules():
    """Everything the model is actually told about RENDERING a controls result.

    As of rubric v7 the output/formatting rules live on the scan_controls tool and
    in the ``note`` returned with every result — not in the rubric, which had been
    carrying a third verbatim copy and is now shipped inline on every call. These
    tests therefore assert the rules reach the model, not which string holds them.
    """
    import re
    from mcp_servers.common import portal
    return re.sub(r"\s+", " ",
                  (cr.CONTROLS_RUBRIC + " " + portal._CONTROLS_NOTE).lower())


def test_output_rules_specify_a_concise_table_and_focus_only():
    body = _delivered_output_rules()
    # renders the pre-assembled table with its columns
    assert "table" in body
    assert "paper_control" in body and "oga_result" in body and "gene_page" in body
    # default (~85% no control) is not narrated; only focus is expanded
    assert "default" in body and "focus" in body
    assert "do not enumerate" in body or "do not analyse" in body


def test_output_rules_keep_the_others_list_and_media_links():
    body = _delivered_output_rules()
    assert "others" in body               # the untested+uncontrolled collapse into a tidy list
    assert "media file" in body           # link media files...
    assert "embed" in body and "do not" in body   # ...NOT embed cards


def test_rubric_v7_keeps_every_decision_tree():
    # The v7 cut removed FORMATTING only. The science must be untouched.
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    for rule in ("pseudo-control", "peptide", "isotype control", "secondary-only",
                 "orthogonal", "knockout", "confidence", "primary antibodies only"):
        assert rule in body, rule
    # the two axes stay the rubric's own responsibility, not the formatter's
    assert "two independent axes" in body


# ── which pillar the answer covers ───────────────────────────────────────────
#
# Of 67 controls extracted in the 100-pair benchmark, 8 were Uhlen pillars this
# rubric does not count as validating. Restricting to the genetic pillar is
# defensible and was applied consistently — it was simply never stated, so a "no"
# read as "this paper showed no validation" where it meant "no GENETIC validation".

def test_rubric_states_which_pillar_it_counts():
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    assert "genetic" in body
    assert "uhlen" in body or "uhlén" in body
    # …and says explicitly what a "no" does NOT mean.
    assert "does not mean the paper showed no validation" in body


def test_the_scope_reaches_the_model_not_just_the_rubric():
    """A scope stated only in the rubric is stated where a caller may not look:
    the note ships on every reply and the description is read before any call."""
    from mcp_servers.common import portal
    for text in (portal._CONTROLS_NOTE.lower(), _tool_doc("scan_controls").lower()):
        assert "genetic" in text
        assert "other_controls" in text


# ── v9: presence, linkage and result are three questions ─────────────────────
#
# Ship changed matching behaviour under a NEW version, or a benchmark spanning two
# versions cannot be scored as one run.
#
# There WAS a `test_the_rubric_is_v9` here asserting the literal `"v10"` — the
# name already disagreed with the assertion, which is what a lockstep pin does to
# a file it is edited alongside. It is gone, deliberately. It failed on any
# WORDING change, so the eval's need for a stable stamp had become a gate on
# what the rubric was allowed to SAY: design constrained by measurement, which is
# backwards. The connector exists to enable a reader, not to be measured
# conveniently.
#
# Nothing is unpinned by removing it. `test_version_is_pinned_and_embedded`
# asserts a version exists and reaches the model, and every rule a bump is meant
# to signal — the class map, the pseudo-control refusals, the three-valued model,
# the two axes — is pinned by content tests in this file. Those fail on a
# behaviour change; the literal only ever fired on a rewrite.


def test_rubric_describes_the_three_valued_model():
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    for value in ("demonstrated", "present_unlinked", "absent"):
        assert value in body, value
    # …and names the three questions it refuses to collapse.
    assert "presence" in body and "linkage" in body and "result" in body


def test_rubric_refuses_co_location_as_a_requirement():
    """The change itself: a knockout panel in Fig 1 and the staining in Fig 4 is
    competent practice, and requiring them to coincide marked correct papers as
    uncontrolled."""
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    assert "do not require the control and the antibody's use to be in the same figure" in body
    assert "supplementary figures count fully" in body
    # …and the v8 sentence that demanded the opposite is gone.
    assert "ideally the same panel as the experimental data" not in body


def test_rubric_sends_the_reader_to_both_the_methods_and_the_legends():
    """An earlier draft said "quote the legend, not the Methods". That is wrong
    about how papers are written: the Methods carry the catalogue number and the
    application, the legend carries the assay and frequently names no reagent at
    all. Requiring one section to hold both halves refuses competent papers."""
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    assert "read both the methods and the legends" in body
    assert "neither holds all of it" in body
    # the four questions, and which section answers each
    assert "which applications?** — **methods.**" in body
    assert "including the supplementary ones" in body
    # …and that the Methods reveal use no figure shows, which must not be dropped
    assert "use no figure shows" in body
    assert "cannot be located is one whose controls cannot be checked" in body
    # …and it must NOT tell the caller to skip the Methods.
    assert "quote the figure legend as your evidence, not the methods" not in body


def test_rubric_treats_the_same_panel_as_evidence_not_only_orientation():
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    assert "the same panel is evidence, not just orientation" in body
    # …while still refusing it as a requirement, which is what the benchmark measured
    assert "never as a requirement" in body


def test_rubric_never_lets_the_result_be_asserted():
    """Question 3 is not answerable from text under any circumstances, and this
    matters MORE once the co-location gate stops filtering candidates."""
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    assert "not answerable from text under any circumstances" in body
    assert "presence is not proof" in body


def test_the_three_values_are_not_blended_back_into_a_score():
    import re
    body = re.sub(r"\s+", " ", cr.CONTROLS_RUBRIC.lower())
    assert "do not blend presence, linkage and result back into one number" in body


def test_the_three_valued_model_reaches_the_model_not_just_the_rubric():
    """Same rule as the scope statement: a caller may read the note or the tool
    description and never the rubric."""
    from mcp_servers.common import portal
    for text in (portal._CONTROLS_NOTE.lower(), _tool_doc("scan_controls").lower()):
        assert "control_status" in text
        assert "present_unlinked" in text
        assert "demonstrated" in text


def test_the_note_forbids_folding_unlinked_into_no():
    from mcp_servers.common import portal
    note = portal._CONTROLS_NOTE.lower()
    assert "do not" in note and "collapse them to yes/no" in note
    assert "never state that a control worked" in note


def _tool_doc(name):
    """The docstring a connecting client actually receives for one tool.

    Sliced to the END of the function, not to a fixed character count. It was
    ``src[start:start + 4000]``, so a docstring that grew past 4000 characters had
    its tail silently dropped and the assertions below started failing about text
    that was present — a test measuring its own window rather than the tool.
    """
    import inspect
    from mcp_servers import server_a_readonly
    src = inspect.getsource(server_a_readonly.build_server)
    start = src.index("def %s(" % name)
    body = src[start:]
    end = body.find('"""', body.index('"""') + 3)
    return body[:end + 3]
