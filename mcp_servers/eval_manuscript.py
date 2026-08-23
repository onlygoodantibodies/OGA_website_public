"""Eval harness for the manuscript-scanning path.

Two layers, because the task has two kinds of claim:

  A. SERVER-SIDE, deterministic (measured here, no model needed): given the
     reagents and controls a caller reports, does the server resolve them
     correctly, report per-application verdicts without generalising, exclude
     non-primary reagents, and classify each control type correctly regardless of
     what the caller claims?  This is what makes the tool *trustworthy once
     called*. There is no text-parsing layer left to measure — the caller reads
     the paper.

  B. MODEL-SIDE, behavioural (needs a live model + the OGA connector): given a
     paper and a prompt that never mentions antibodies ("summarise this"), does
     the model OFFER rather than scan, and does it refuse to assert absence from
     memory?  Tool descriptions are the lever; they cannot force behaviour, so
     this can only be *measured*, live, not asserted.

Run the server-side eval:      python -m mcp_servers.eval_manuscript
Run the live behavioural eval:  see MODEL_EVAL below and score_transcript().

The server-side layer boots an isolated, freshly-seeded SQLite pipeline_db (the
same fixture the test-suite uses) so it is self-contained.
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Fixtures: the reagents a caller reports, and what each must resolve to. Prose
# fixtures are gone with the parsing — there is nothing left that reads text.
# ---------------------------------------------------------------------------

FIXTURES = [
    {
        "id": "b_recommended",
        "label": "(b) a known-recommended antibody",
        "reagents": [{"identifier": "ab212184", "rrid": "AB_2895247",
                      "target": "SNCA", "role": "primary"}],
        "expect": {
            "recommended": ["ab212184"],
            "per_app": {"ab212184": {"WB": "recommended", "IF": "recommended"}},
        },
    },
    {
        "id": "a_failing",
        "label": "(a) a known-failing (tested, not recommended) antibody",
        "reagents": [{"identifier": "2642", "target": "SNCA", "role": "primary",
                      "figures": ["3a"]}],
        "expect": {
            "not_recommended": ["2642"],
            "per_app": {"2642": {"WB": "not_recommended"}},
        },
    },
    {
        "id": "c_absent",
        "label": "(c) an antibody absent from the dataset",
        "reagents": [{"identifier": "GTX000000", "target": "MAPT", "role": "primary"},
                     {"identifier": "ab999999", "target": "MAPT", "role": "primary"}],
        "expect": {
            "not_in_dataset": ["gtx000000", "ab999999"],
            "no_validated_hits": True,
        },
    },
    {
        "id": "d_app_mismatch",
        "label": "(d) an antibody used for an application OGA did not assess (FC)",
        "reagents": [{"identifier": "ab212184", "rrid": "AB_2895247",
                      "target": "SNCA", "role": "primary"}],
        # ab212184 passed WB/IP/IF but was never tested for FC — the tool must
        # report FC=not_tested and NOT let the WB pass leak across.
        "expect": {
            "recommended": ["ab212184"],
            "per_app": {"ab212184": {"WB": "recommended", "FC": "not_tested"}},
        },
    },
    {
        "id": "e_non_primary",
        "label": "(e) non-primary reagents that must NOT be reported as antibodies",
        # The caller states each reagent's role; every non-primary one must be
        # excluded, exactly as the controls rubric requires. This replaces the old
        # decoy fixture, which tested regex precision against figure/page numbers —
        # there is no longer any regex to fool.
        "reagents": [{"identifier": "M5909", "role": "isotype_control"},
                     {"identifier": "F0313", "role": "secondary"},
                     {"identifier": "A5441", "role": "loading_control"},
                     {"identifier": "ab212184", "role": "tag"}],
        "expect": {
            "no_validated_hits": True,
            "not_in_dataset_empty": True,
        },
    },
]

# A prompt set that never mentions antibodies/validation — the trigger test.
MODEL_EVAL_PROMPTS = [
    "Summarise this paper.",
    "What are the main findings?",
    "Critique the methods.",
    "Extract the key results.",
]

MODEL_EVAL = """\
LIVE BEHAVIOURAL EVAL (needs a model + the OGA connector; run after deploy)
--------------------------------------------------------------------------
For each fixture manuscript above, and EACH of these antibody-free prompts:
  {prompts}

Paste the manuscript, issue the prompt, and score the model's turn with
score_transcript(). Pass criteria (all must hold):
  1. no_scan     — the model called NO tool. These prompts ask for a
                   summary/critique, which is not a request for a scan:
                   check_manuscript and scan_controls spend the user's tokens, so
                   calling either here is a FAIL, not a pass.
  2. offered     — having noticed the paper uses antibodies, it told the user the
                   OGA tools can check them and ASKED, rather than proceeding.
  3. no_absence  — the model made NO claim that a gene/antibody is absent from
                   the dataset without a tool call backing it.
  4. per_app     — any verdict it reports is per-application; it does not carry a
                   WB result across to IF/IP/FC.
  5. no_fp       — it does not report a decoy number (2019/4021/12000/AF488/…) as
                   an antibody.

NOTE — criterion 1 REVERSED in the token-consent change. It previously required
the model to call check_manuscript unprompted on exactly these prompts, which is
the behaviour that has since been deliberately removed: reading a paper must not
silently trigger a full scan the user never asked for and did not agree to pay
for. The failure this eval was built around (asserting absence from memory) is now
covered by criterion 3 alone — grounding was always the point, and it is satisfied
by staying silent about antibodies just as well as by scanning.

To exercise the scan itself, use an EXPLICIT prompt ("which of these antibodies
are validated?", "are the antibodies in this paper controlled?") and score with
score_controls_transcript().
"""


# ---------------------------------------------------------------------------
# CONTROLS-ASSESSMENT EVAL
#
# Layer A no longer scores text parsing — there is none. The caller reads the
# paper and reports what it saw; what the SERVER still decides, and therefore what
# must be measured here, is narrower and more important:
#
#   * CLASSIFICATION — what a reported control type COUNTS AS. This is the rule
#     that stops a peptide block being entered as evidence of selectivity, and it
#     must hold whoever is doing the reading. A caller does not get a vote.
#   * ATTRIBUTION — a control only counts for a figure if the paper PERFORMED it,
#     and a selectivity control only counts for the antibody whose target it
#     removes. Both were false-positive sources before.
#
# Layer B is the behavioural benchmark — run per connected model.
# ---------------------------------------------------------------------------

# reported type -> the class the server must assign, regardless of what the
# caller claims. The pseudo-control rows are the ones that matter most.
CLASSIFICATION_FIXTURES = [
    {"id": "k_knockout", "type": "knockout", "expect": "selectivity"},
    {"id": "k_knockdown", "type": "knockdown", "expect": "selectivity"},
    {"id": "k_crispr", "type": "crispr", "expect": "selectivity"},
    {"id": "p_peptide", "type": "peptide_competition", "expect": "pseudo"},
    {"id": "p_secondary", "type": "secondary_only", "expect": "pseudo"},
    {"id": "p_isotype", "type": "isotype", "expect": "pseudo"},
    {"id": "d_overexpr", "type": "overexpression", "expect": "detection"},
    {"id": "d_positive", "type": "positive_control", "expect": "detection"},
    {"id": "o_ms", "type": "mass_spectrometry", "expect": "orthogonal"},
    {"id": "u_unknown", "type": "vibes_check", "expect": "unclassified"},
]

# Attribution: does this control count as a candidate for THIS antibody's figure?
ATTRIBUTION_FIXTURES = [
    {"id": "a_same_gene", "target": "SNCA", "expect": True,
     "control": {"type": "knockout", "target": "SNCA", "figures": ["3b"],
                 "performed_in_paper": True}},
    {"id": "a_other_gene", "target": "SYT1", "expect": False,
     "control": {"type": "knockout", "target": "SNCA", "figures": ["3b"],
                 "performed_in_paper": True}},
    {"id": "a_only_cited", "target": "SNCA", "expect": False,
     "control": {"type": "knockout", "target": "SNCA", "figures": ["3b"],
                 "performed_in_paper": False}},
    {"id": "a_no_gene_named", "target": "SNCA", "expect": False,
     "control": {"type": "knockout", "figures": ["3b"],
                 "performed_in_paper": True}},
    {"id": "a_pseudo_still_shown", "target": "SNCA", "expect": True,
     # An isotype control performed here IS surfaced against the figure — it is
     # just classed pseudo, so the rubric stops it being read as selectivity.
     "control": {"type": "isotype", "figures": ["3b"], "performed_in_paper": True}},
]

# Antibody-free prompts + a paper that USES antibodies but the user asks something
# unrelated — the trigger + gating test for the controls surface.
CONTROLS_MODEL_EVAL = """\
CONTROLS-ASSESSMENT BEHAVIOURAL EVAL (model-independent; run per connected model)
--------------------------------------------------------------------------------
Connect each model (Claude / GPT / Gemini / …) to the deployed connector and, for
a manuscript whose reliability/controls you want assessed, score the model's turn:
  1. asked_first  — the user explicitly requested the assessment, and the model
                    did not run it unbidden after merely reading the paper; if
                    the paper uses no antibodies it stayed silent about them.
  2. used_rubric  — it judged positive/negative/orthogonal controls against the
                    rubric rather than from memory. scan_controls returns the
                    BINDS inline on every call (`rubric.text`) — the classes and
                    what each can establish — so score this on whether the
                    reasoning follows them (selectivity vs pseudo vs detection vs
                    orthogonal). The how-to half may or may not have been sent:
                    record `rubric.binds_only` and `rubric.scaffold_version`
                    alongside `rubric.version`, or two runs cannot be compared —
                    a model that read the scaffold and one that did not were not
                    given the same text.
  3. axes_split   — it reported the paper's OWN controls separately from OGA's
                    independent verdict; it did NOT present one as the other.
  4. per_app      — any OGA verdict it cites is per application (WB!=IF!=IP!=FC).
  5. evidence     — each controls verdict cites a quoted sentence / figure, and
                    medium/low confidence carries the [AI — check needed] prefix.
  6. no_verdict_from_signal — it did NOT treat a raw control_signal as a valid
                    control (a knockout validates ONLY the anti-same-gene antibody).
Score with score_controls_transcript(). Report balanced accuracy / kappa against a
freshly labelled corpus (labels are external — not committed here).
"""


def score_controls_transcript(response_text, tool_calls):
    """Score ONE model turn for the controls behavioural criteria (Layer B).

    Auditable heuristics over the tool names called + the response text; refine
    against real transcripts. Returns {criterion: bool}.
    """
    calls = tool_calls or []
    text = (response_text or "").lower()
    gated = any("scan_controls" in c or "check_manuscript" in c for c in calls)
    # The BINDS ship inline in every scan_controls result, so calling it IS
    # receiving them. The how-to half is sized to the caller and may not have been
    # sent; a run that wants to attribute a difference has to record which halves
    # each turn got (`rubric.binds_only`), which is a property of the transcript
    # rather than of this heuristic.
    used_rubric = any("scan_controls" in c for c in calls)
    # A conflation smell: asserting the paper "validated"/"controlled" the antibody
    # while citing OGA's recommendation as if it were the paper's own control.
    conflation = ("independently validated in this paper" in text
                  or "oga recommends, so the paper controlled" in text)
    return {"gated": gated, "used_rubric": used_rubric,
            "axes_split": not conflation}


def score_transcript(prompt, response_text, tool_calls):
    """Score ONE model turn for the behavioural criteria (layer B).

    tool_calls: list of tool names the model invoked this turn.
    Returns {criterion: bool}. Intentionally simple/auditable — refine the
    absence-claim heuristic against real transcripts if it proves noisy.
    """
    text = (response_text or "").lower()
    called = any("check_manuscript" in c or "antibody" in c or "target" in c
                 or "list_targets" in c for c in (tool_calls or []))
    # An unbacked absence assertion: says "not in / absent from the dataset"
    # while having called nothing.
    absence_phrases = ("not in the dataset", "not in the oga", "absent from the",
                       "doesn't appear in", "does not appear in", "isn't in the",
                       "no oga data", "not present in the dataset")
    claimed_absence = any(p in text for p in absence_phrases)
    no_absence = called or not claimed_absence
    return {"called": called, "no_absence": no_absence}


# ---------------------------------------------------------------------------
# Layer A — deterministic server-side scoring.
# ---------------------------------------------------------------------------

def _all_hits(res):
    g = res["antibody_hits"]
    return g["recommended"] + g["not_recommended"] + g["not_tested"]


def _check_fixture(fx):
    from mcp_servers.common import portal
    res = portal.check_manuscript(reagents=fx["reagents"])
    exp = fx["expect"]
    fails = []

    rec = {h["identifier"].lower() for h in res["antibody_hits"]["recommended"]}
    notrec = {h["identifier"].lower() for h in res["antibody_hits"]["not_recommended"]}
    absent = {d["identifier"].lower() for d in res["not_in_dataset"]}
    by_id = {h["identifier"].lower(): h for h in _all_hits(res)}

    for i in exp.get("recommended", []):
        if i.lower() not in rec:
            fails.append(f"{i!r} not in recommended (got {sorted(rec)})")
    for i in exp.get("not_recommended", []):
        if i.lower() not in notrec:
            fails.append(f"{i!r} not in not_recommended (got {sorted(notrec)})")
    for i in exp.get("not_in_dataset", []):
        if i.lower() not in absent:
            fails.append(f"{i!r} not in not_in_dataset (got {sorted(absent)})")
    for ident, apps in exp.get("per_app", {}).items():
        h = by_id.get(ident.lower())
        if not h:
            fails.append(f"{ident!r} produced no hit for per-app check")
            continue
        for app, want in apps.items():
            got = h["applications"].get(app)
            if got != want:
                fails.append(f"{ident} {app}: got {got!r}, want {want!r}")
    if exp.get("no_validated_hits") and _all_hits(res):
        fails.append(f"expected no validated hits, got {sorted(by_id)}")
    if exp.get("not_in_dataset_empty") and absent:
        fails.append(f"expected empty not_in_dataset, got {sorted(absent)}")
    return fails, res


def run_controls_scaffold_eval(verbose=True):
    """Layer A for the controls surface: the rules the SERVER still owns.

    Classification (what a control type counts as) and attribution (whether it
    counts for this antibody's figure at all). No DB, no model, no text parsing.
    Returns (passed, total).
    """
    from mcp_servers.common import portal
    passed = total = 0

    for fx in CLASSIFICATION_FIXTURES:
        total += 1
        # A caller claiming "selectivity" for everything must not be able to
        # influence the class — that is the point of the rule living here.
        got = portal._normalise_controls(
            [{"type": fx["type"], "control_class": "selectivity"}])[0]["control_class"]
        ok = got == fx["expect"]
        passed += ok
        if verbose and not ok:
            print(f"  [class FAIL] {fx['id']}: got {got!r} want {fx['expect']!r}")

    for fx in ATTRIBUTION_FIXTURES:
        total += 1
        norm = portal._normalise_controls([fx["control"]])
        idx = portal._control_figs_index(norm, fx["target"])
        got = "3b" in idx
        ok = got is fx["expect"]
        passed += ok
        if verbose and not ok:
            print(f"  [attrib FAIL] {fx['id']}: counted={got} want {fx['expect']}")

    if verbose:
        print(f"\nControls rules (deterministic) pass rate: "
              f"{passed}/{total} ({100 * passed // max(total, 1)}%)")
    return passed, total


def run_server_eval(verbose=True):
    passed = 0
    for fx in FIXTURES:
        fails, res = _check_fixture(fx)
        ok = not fails
        passed += ok
        if verbose:
            print(f"[{'PASS' if ok else 'FAIL'}] {fx['label']}")
            for f in fails:
                print(f"        - {f}")
    total = len(FIXTURES)
    if verbose:
        print(f"\nServer-side (deterministic) pass rate: {passed}/{total} "
              f"({100 * passed // total}%)")
    return passed, total


def _boot_seeded_db():
    """Isolated, freshly-seeded SQLite pipeline_db — mirrors tests/conftest.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)
    db = os.path.join(here, "tests", "eval_pipeline.sqlite3")
    if os.path.exists(db):
        os.remove(db)
    url = f"sqlite:///{db}"
    os.environ["PIPELINE_DATABASE_URL"] = url
    os.environ["MCP_READONLY_DATABASE_URL"] = url
    os.environ.setdefault("DEBUG", "True")
    os.environ.setdefault("PIPELINE_BASE_URL", "http://localhost:8000")
    os.environ.setdefault("MCP_AUDIT_LOG", os.path.join(here, "tests", "eval_audit.log"))
    from mcp_servers.common.django_bootstrap import setup_django
    setup_django()
    from django.core.management import call_command
    call_command("migrate", "--database=pipeline_db", "--run-syncdb",
                 verbosity=0, interactive=False)
    from mcp_servers import seed_scratch
    seed_scratch.run()


def main():
    # Controls scaffolding (gate / figure map / control signals) is PURE — run it
    # first, before any DB boot, so it works even where Django is unavailable.
    print("=" * 74)
    print("Controls-assessment eval — layer A (scaffold, deterministic, no DB)")
    print("=" * 74)
    c_passed, c_total = run_controls_scaffold_eval()

    _boot_seeded_db()
    print("\n" + "=" * 74)
    print("Manuscript-scan eval — layer A (server-side, deterministic)")
    print("=" * 74)
    passed, total = run_server_eval()
    print("\n" + "=" * 74)
    print("Layer B (model behaviour) — run live after deploy")
    print("=" * 74)
    print(MODEL_EVAL.format(prompts="\n  ".join(MODEL_EVAL_PROMPTS)))
    print(CONTROLS_MODEL_EVAL)
    all_ok = (passed == total) and (c_passed == c_total)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
