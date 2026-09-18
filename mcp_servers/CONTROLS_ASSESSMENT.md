# Controls assessment — design note (Server A)

The reasoning behind `check_manuscript` and `scan_controls`: what counts as a
valid control, why the two reliability axes never merge, and why the first
implementation was torn out.

**For current behaviour, read the code** — the tool docstrings in
`server_a_readonly.py`, the rubric in `common/controls_rubric.py`
(`CONTROL_CLASSES` is the authority), the instructions in
`common/tutor_guidance.py`, and `tests/test_scan_controls.py`.

---

## The failure worth remembering

The first iteration parsed manuscript text **server-side** to work out which
antibodies a paper used and in which figures. `common/antibody_use.py` and a
`detect_antibody_use` tool did the extraction; `common/manuscript.py` carried the
parsing. All of it is gone.

**Every defect ever found in this feature was in the parsing layer and none in
the database layer.** It missed catalogue numbers whose shape it did not
recognise — including the one carrying a paper's whole tissue-expression claim.
It could not distinguish an isotype control from a primary antibody. It truncated
multi-word target names at the first token, read "Kd" as "KD", and reported a
knockout that a paper merely *cited in its Discussion* as a control that paper had
performed. Each was fixed individually. The class of failure was structural.

So the parsing moved to the caller. The tools no longer accept manuscript `text`;
the connected model reads the paper and passes structured `reagents` (identifier,
RRID, target, role, figures) and `controls` (type, target, figures,
`performed_in_paper`).

**What the server still decides**, and still tests: resolution against the
dataset, per-application verdicts, exclusion of non-primary reagents by declared
role, and — the rule a caller cannot override — what each control TYPE counts as.
A peptide block cannot be entered as evidence of selectivity by anyone, model
included.

**Nothing runs unasked.** `check_manuscript` and `scan_controls` are
explicit-request-only; reading a paper prompts an OFFER, not a scan. The rubric
ships inline in every `scan_controls` result rather than being a prompt the user
has to paste in.

---

## The second failure worth remembering: one boolean for three questions

Until rubric v8 a control counted for an antibody only if the two figure lists
shared a string, and `paper_control` was "yes" or "no". Both halves were wrong,
and for the same reason — **three separable questions were collapsed into one**:

1. **Presence** — does the paper show a genetic manipulation of this target?
   Answerable from text.
2. **Linkage** — was *this antibody* read out against that material? Answerable
   only from the figure legend, and frequently not stated anywhere.
3. **Result** — did the signal disappear, at the right size, in the right
   application? **Never answerable from text.**

Figure co-location was a proxy for (2), and a measurably useless one: on 72
scoreable pairs, varying only that rule moved sensitivity from 0.696 (lists must
intersect) to 1.000 (no figure test) at an **identical specificity of 0.959**.
Papers do not co-locate controls with use — establishing a reagent in one figure
and using it in another is normal practice, and the gate marked those papers
uncontrolled.

The concern it stood in for is real: a paper can knock down gene X to study
biology while the anti-X antibody is never tested against that knockdown. That is
now `present_unlinked`, said out loud, rather than a "no" that reads as an
accusation. Version history and the full what-moved / what-did-not list live
beside `CONTROLS_RUBRIC_VERSION` in `common/controls_rubric.py`.

## The two reliability axes — never merged

| | **External** | **In-manuscript** |
|---|---|---|
| Question | Has OGA/YCharOS characterised this antibody against knockout controls? | Did *this paper* control this antibody, in *this figure*? |
| Source | OGA database (ground truth) | The manuscript (model judgement) |
| Nature | Deterministic fact | Fallible judgement (~86% accurate) |
| Tool | `check_manuscript`, `antibody_validation` | `scan_controls` |
| Failure cost | A wrong hit is worse than a miss | A false "well-controlled" is worse than a miss |

They are **orthogonal**. A paper can use a `not_recommended` antibody but present
its own strong KO control, so its figure may still stand; or use a `recommended`
one and show no controls at all. The output presents both axes side by side and
**never blends them into a single score**.

Accuracy figures for the in-manuscript axis come from the OGA AI-extraction work
(the Validation Record proforma and the METASCI bid): **86.1% balanced accuracy,
κ 0.787, sensitivity 0.733, specificity 0.988** across 101 manuscripts — error
rates comparable to expert curators.

## Design rules that still hold

1. **Everything the server returns is a database fact or a deterministic
   derivation** — never a hallucination. Judgement stays with the connected model.
2. **Extraction ≠ classification.** Every controls verdict carries the quoted
   evidence sentence + figure ref it rests on, separately from the verdict, so a
   human can audit the call.
3. **Confidence is first-class.** Judged fields carry `high | medium | low |
   none`; `medium`/`low` are prefixed `[AI — check needed]`. "Route to human" is a
   normal output state, not an error.
4. **The identity→DB match and the controls verdict both favour precision.** A
   wrong "in the dataset" hit, or a false "well-controlled", is the expensive
   error.
5. **The rubric is versioned.** Anything judged records which rubric version
   guided it, so results reproduce and the eval can pin a frozen prompt.
6. **Read-only, no persistence.** The tools never write, and never log full
   manuscript text — `audit.py` records counts and flags only, because pasted text
   may be unpublished or embargoed.

## Risks & non-goals

- **Figure pixels are never seen.** Control detection reads legend and prose. If
  the caller doesn't supply legends, recall drops — the output should say so
  rather than infer.
- **Careful wording about named papers.** "No control **recorded in the text**",
  "untested, **not** unreliable" — never "the paper is wrong".
- **Advisory only.** Tool descriptions and instructions are honoured unevenly
  across bring-your-own-AI clients (`tutor_guidance.py` says as much). The
  structured record shape reduces reliance on the model remembering the workflow
  but cannot enforce it.
- **Not** whole-DB analytics, **not** a write path, **not** a persistence layer.

## Open questions for the owner

- Should the record stay proforma-identical, so it feeds the Validation Recorder,
  or take a leaner MCP-only shape?
- Can the labelled 101-paper corpus be committed (or referenced) for the eval, or
  does it stay external?
