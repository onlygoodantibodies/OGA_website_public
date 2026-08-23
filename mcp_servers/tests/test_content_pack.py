"""The education content pack loader + plan grading (pure logic, no DB).

These lock down the two things the connector leans on: that the modules load, and
that ``grade_plan`` enforces the sharp teaching gate — a plan that leans only on
pseudo-controls (peptide block / isotype / secondary-only / loading) for
selectivity must FAIL, and one with a genuine positive + negative selectivity
control must pass the mechanical gates.
"""
from __future__ import annotations

from mcp_servers.common import content_pack


def test_modules_load():
    ids = {m["id"] for m in content_pack.list_modules()}
    assert {"overview", "framework", "controls", "acquiring"} <= ids
    for mid in ids:
        mod = content_pack.get_module(mid)
        assert mod and mod["markdown"].strip()


def test_get_module_unknown():
    assert content_pack.get_module("nope") is None


def test_pack_version_present():
    assert content_pack.pack_version() not in ("", "unknown")


def test_schema_and_rubric_present():
    schema = content_pack.plan_schema()
    assert schema["title"] == "OGA validation plan"
    r = content_pack.rubric()
    assert "peptide_block" in r["non_selectivity_control_kinds"]
    assert "knockout" in r["selectivity_control_kinds"]


def test_classify_control():
    assert content_pack.classify_control("knockout")["verdict"] == "establishes_selectivity"
    assert content_pack.classify_control("overexpression_lysate")["verdict"] == "establishes_selectivity"
    for bad in ("peptide_block", "isotype", "secondary_only", "loading_control"):
        assert content_pack.classify_control(bad)["verdict"] == "does_not_establish_selectivity"
    assert content_pack.classify_control("banana")["verdict"] == "unknown"


def _good_plan():
    return {
        "target_gene": "SNCA", "application": "WB", "sample_type": "SH-SY5Y lysate",
        "question_type": "target_specific",
        "evidence_search": {"sources_checked": ["OGA"], "found_genetic_validation": True},
        "controls": {
            "positive": [{"kind": "overexpression_lysate", "material": "OriGene SNCA"}],
            "negative": [{"kind": "knockout", "material": "SNCA KO line"}],
        },
        "decision": "proceed",
    }


def test_grade_good_plan_passes_mechanical():
    result = content_pack.grade_plan(_good_plan())
    assert result["passed_mechanical"] is True
    assert all(g["passed"] for g in result["gates"])
    assert result["reasoning_checks"]           # still handed back for the tutor


def test_grade_pseudo_controls_only_fails():
    plan = _good_plan()
    # Learner tries to "validate" selectivity with only isotype + secondary + block.
    plan["controls"] = {
        "positive": [{"kind": "isotype", "material": "IgG1 isotype"}],
        "negative": [{"kind": "secondary_only", "material": "no-primary"},
                     {"kind": "peptide_block", "material": "immunogen peptide"}],
    }
    result = content_pack.grade_plan(plan)
    assert result["passed_mechanical"] is False
    gates = {g["id"]: g["passed"] for g in result["gates"]}
    assert gates["not_fooled_by_pseudo_controls"] is False
    assert gates["meaningful_positive_control"] is False
    assert gates["meaningful_negative_control"] is False


def test_grade_missing_negative_control_fails():
    plan = _good_plan()
    plan["controls"]["negative"] = []
    result = content_pack.grade_plan(plan)
    assert result["passed_mechanical"] is False
    assert {g["id"]: g["passed"] for g in result["gates"]}["meaningful_negative_control"] is False


def test_grade_no_evidence_source_fails():
    plan = _good_plan()
    plan["evidence_search"]["sources_checked"] = []
    result = content_pack.grade_plan(plan)
    assert {g["id"]: g["passed"] for g in result["gates"]}["evidence_grounded"] is False


def test_grade_empty_plan_is_safe():
    result = content_pack.grade_plan({})
    assert result["passed_mechanical"] is False   # no crash on missing keys


# --- per-module quizzes ------------------------------------------------------

def test_certifiable_modules():
    # the reflective 'choosing' walkthrough is NOT a certifiable/quizzed module
    ids = [m["id"] for m in content_pack.certifiable_modules()]
    assert ids == ["framework", "controls", "acquiring"]


def test_choosing_walkthrough_module_loads():
    mod = content_pack.get_module("choosing")
    assert mod and "decision" in mod["markdown"].lower()
    # it must frame the elicitation as reflection, not a scored test
    assert "not" in mod["markdown"].lower() and "score" in mod["markdown"].lower()
    assert "choosing" in {m["id"] for m in content_pack.list_modules()}


def test_module_quiz_hides_answers():
    quiz = content_pack.module_quiz("controls")
    assert quiz["questions"] and "options" in quiz["questions"][0]
    # the correct index must NOT be exposed to the learner
    assert all("correct" not in q for q in quiz["questions"])


def test_module_quiz_unknown():
    assert content_pack.module_quiz("nope") is None


def _answer_key(module_id):
    return [q["correct"] for q in content_pack._quizzes()["modules"][module_id]["questions"]]


def test_grade_all_correct_passes():
    for mid in ("framework", "controls", "acquiring"):
        r = content_pack.grade_module_quiz(mid, _answer_key(mid))
        assert r["passed"] is True and r["score"] == 1.0
        assert all(res["is_correct"] for res in r["results"])


def test_grade_all_wrong_fails_with_explanations():
    key = _answer_key("controls")
    wrong = [(k + 1) % 4 for k in key]   # shift every answer
    r = content_pack.grade_module_quiz("controls", wrong)
    assert r["passed"] is False
    assert all(res["explanation"] for res in r["results"])   # teach the misses


def test_grade_module_quiz_unknown():
    assert content_pack.grade_module_quiz("nope", [])["found"] is False


def test_capstone_spec():
    spec = content_pack.capstone_spec()
    assert set(spec["requires_modules"]) == {"framework", "controls", "acquiring"}
    assert spec["requires_workshop_plan"] is True
