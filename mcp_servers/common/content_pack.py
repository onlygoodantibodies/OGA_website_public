"""Loader for the education content pack (``mcp_servers/content/``).

The pack is the versioned teaching backbone for the A2 interactive-learning
connector — reviewable markdown modules plus the validation-plan JSON schema and
grading rubric. This module reads them off disk so the connector (and its tests)
have one place to load content and to evaluate a learner's plan against the
rubric's mechanical gates.

Nothing here touches the database; it is pure file IO + plan validation.
"""
from __future__ import annotations

import io
import json
import os
from functools import lru_cache

_HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR = os.path.normpath(os.path.join(_HERE, "..", "content"))

# Module id → (title, filename). "framework"/"controls"/"acquiring" are the
# authored/extracted modules in this pack; the academy Lesson rows (read live
# from academy_db) slot in alongside these in the full build.
#: The reading guidance, served to any connected model that asks for it.
#:
#: ONE SOURCE. This is the same file Claude loads as a skill — read off disk, not
#: copied — because a copy of guidance is guidance that drifts, and the drift is
#: invisible until two readers of one paper disagree. The skill reaches Claude
#: surfaces that support skills; this reaches every other connected model, which
#: is exactly the reader the support was written for.
SKILL_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "..", ".claude", "skills", "antibody-controls",
                 "SKILL.md"))


@lru_cache(maxsize=1)
def reading_guidance():
    """The paper-reading guidance, frontmatter stripped, or ``None``.

    ``None`` is a real answer and the caller must render it as one: the file is
    repo content and a deploy that did not carry it must say so, rather than
    returning an empty string that reads like guidance saying nothing.
    """
    try:
        text = io.open(SKILL_PATH, encoding="utf-8").read()
    except OSError:
        return None
    # YAML frontmatter is for the skill loader; a model asking this tool wants
    # the document.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip() or None


MODULES = {
    "overview": ("How this works", "00_overview.md"),
    "choosing": ("The antibody & control decision walkthrough (start here)",
                 "05_choosing_walkthrough.md"),
    "framework": ("Planning antibody validation (the framework)",
                  "10_validation_framework.md"),
    "controls": ("Controls that count — which controls establish selectivity",
                 "20_controls_that_count.md"),
    "acquiring": ("Acquiring controls — how to find, buy, or make them",
                  "30_acquiring_controls.md"),
}


def pack_version() -> str:
    """The pack version string (from README's PACK_VERSION marker)."""
    text = _read("README.md")
    for line in text.splitlines():
        if line.strip().startswith("PACK_VERSION"):
            # e.g. `PACK_VERSION = 0.1.0-draft`
            return line.split("=", 1)[1].strip()
    return "unknown"


@lru_cache(maxsize=None)
def _read(filename: str) -> str:
    path = os.path.join(CONTENT_DIR, filename)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def list_modules() -> list[dict]:
    return [{"id": mid, "title": title} for mid, (title, _fn) in MODULES.items()]


def get_module(module_id: str) -> dict | None:
    entry = MODULES.get(module_id)
    if not entry:
        return None
    title, filename = entry
    return {"id": module_id, "title": title, "markdown": _read(filename)}


@lru_cache(maxsize=1)
def plan_schema() -> dict:
    return json.loads(_read("plan_schema.json"))


def rubric() -> dict:
    return plan_schema()["rubric"]


# --- per-module quizzes (the module-certificate gate) -----------------------

@lru_cache(maxsize=1)
def _quizzes() -> dict:
    return json.loads(_read("quizzes.json"))


def certifiable_modules() -> list[dict]:
    """Modules that carry a quiz + a certificate, in order."""
    mods = _quizzes()["modules"]
    return [{"id": mid, "title": mods[mid]["title"],
             "num_questions": len(mods[mid]["questions"])}
            for mid in MODULES if mid in mods]


def capstone_spec() -> dict:
    return _quizzes()["capstone"]


def module_quiz(module_id: str) -> dict | None:
    """A module's quiz WITHOUT the correct answers — safe to show a learner."""
    mod = _quizzes()["modules"].get(module_id)
    if not mod:
        return None
    questions = [{"text": q["text"], "options": q["options"]}
                 for q in mod["questions"]]
    return {"module_id": module_id, "title": mod["title"],
            "pass_mark": _quizzes()["pass_mark"], "questions": questions}


def _normalise_choice(a):
    """Accept a chosen option as a 0-based int, a letter ('a'..'z', any case), or
    a digit string, and return a 0-based index (or None). Letting the tool take
    letters removes the error-prone letter->index conversion from the model."""
    if isinstance(a, bool):
        return None
    if isinstance(a, int):
        return a
    if isinstance(a, str):
        s = a.strip().lower()
        if len(s) == 1 and "a" <= s <= "z":
            return ord(s) - ord("a")
        if s.isdigit():
            return int(s)
    return None


def grade_module_quiz(module_id: str, answers: list) -> dict:
    """Grade answers for a module's quiz. Each answer may be a letter ('a'..'d')
    or a 0-based index — both are normalised here, so the caller never has to do
    letter->index arithmetic.

    Returns pass/fail, the score, and per-question correctness + explanations
    (so the tutor can teach the misses). Learners may retry.
    """
    mod = _quizzes()["modules"].get(module_id)
    if not mod:
        return {"found": False, "module_id": module_id}
    questions = mod["questions"]
    answers = list(answers or [])
    results, correct = [], 0
    for i, q in enumerate(questions):
        chosen = _normalise_choice(answers[i]) if i < len(answers) else None
        ok = (chosen == q["correct"])
        if ok:
            correct += 1
        results.append({
            "question": q["text"], "chosen": chosen,
            "correct_option": q["correct"], "is_correct": ok,
            "explanation": q["explanation"],
        })
    total = len(questions)
    score = correct / total if total else 0.0
    passed = score >= _quizzes()["pass_mark"]
    return {"found": True, "module_id": module_id, "title": mod["title"],
            "passed": passed, "score": round(score, 3),
            "correct": correct, "total": total, "results": results}


# --- plan grading (the mechanical hard gates only) --------------------------
# The reasoning checks in the rubric are open-ended and LLM-graded; this function
# evaluates only what can be checked deterministically, so the connector (and the
# eventual certificate issuance) has an objective floor that can't be talked past.

def grade_plan(plan: dict) -> dict:
    """Evaluate a validation plan against the rubric's mechanical hard gates.

    Returns ``{"passed_mechanical": bool, "gates": [...], "reasoning_checks":
    [...]}``. ``passed_mechanical`` is True only if every mechanical gate passes;
    it is a NECESSARY-not-sufficient floor — a certificate also needs the
    open-ended reasoning checks graded by the tutor.
    """
    r = rubric()
    sel = set(r["selectivity_control_kinds"])
    non_sel = set(r["non_selectivity_control_kinds"])
    controls = (plan or {}).get("controls") or {}
    pos = controls.get("positive") or []
    neg = controls.get("negative") or []
    pos_kinds = {c.get("kind") for c in pos if isinstance(c, dict)}
    neg_kinds = {c.get("kind") for c in neg if isinstance(c, dict)}
    all_kinds = pos_kinds | neg_kinds
    ev = (plan or {}).get("evidence_search") or {}

    gates = []

    def add(gid, ok, detail):
        gates.append({"id": gid, "passed": bool(ok), "detail": detail})

    add("question_mapping",
        (plan or {}).get("question_type") in
        {"target_specific", "community_marker", "technical_function"},
        "A question type must be chosen (framework §1).")

    add("meaningful_positive_control",
        bool(pos_kinds & sel),
        "At least one positive control must DEFINE/ADD the target "
        "(overexpression, tagged, knock-in, KO–WT pair).")

    add("meaningful_negative_control",
        bool(neg_kinds & sel),
        "At least one negative control must REMOVE/REDUCE the target "
        "(knockout, knockdown, non-expressing line).")

    # The teaching gate: the learner must not be relying ONLY on pseudo-controls
    # for selectivity. Listing them is fine; leaning on them as the whole strategy
    # (with no genuine selectivity control) is the failure this exercise targets.
    leaning_on_pseudo = bool(all_kinds) and all_kinds.issubset(non_sel)
    add("not_fooled_by_pseudo_controls",
        not leaning_on_pseudo and bool((pos_kinds | neg_kinds) & sel),
        "Peptide-block / isotype / secondary-only / loading controls do not "
        "establish selectivity; they cannot be the whole control strategy.")

    add("evidence_grounded",
        bool(ev.get("sources_checked")),
        "At least one evidence source must be checked (framework §4); ground "
        "OGA lookups via Server A. Absence of OGA data is not a failure.")

    add("documented_decision",
        bool((plan or {}).get("decision")),
        "A documented proceed/reject/test_further decision is required "
        "(framework §5/§7).")

    return {
        "passed_mechanical": all(g["passed"] for g in gates),
        "gates": gates,
        "reasoning_checks": r["reasoning_checks"],
    }


def classify_control(kind: str) -> dict:
    """Is a control kind selectivity-establishing? The core teaching helper."""
    r = rubric()
    sel = set(r["selectivity_control_kinds"])
    non_sel = set(r["non_selectivity_control_kinds"])
    if kind in sel:
        verdict = "establishes_selectivity"
    elif kind in non_sel:
        verdict = "does_not_establish_selectivity"
    else:
        verdict = "unknown"
    return {"kind": kind, "verdict": verdict,
            "selectivity_control_kinds": sorted(sel),
            "non_selectivity_control_kinds": sorted(non_sel)}
