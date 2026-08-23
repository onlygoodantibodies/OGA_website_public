"""The antibody-controls rubric: what a control METHOD can establish, and how a
reader usually finds one in a paper.

Served in two halves. `scan_controls` carries the binds on every call; the whole
document is available from the ``controls_rubric`` TOOL. It was described here
for a long time as an MCP *prompt*, which was never registered — a pointer to a
thing that does not exist, which is the failure this repo names as "a routed
endpoint is not a reachable one".

This is the *classification* half of the two-step design (extraction vs
classification): the connected model applies these decision trees to the paper's
text + figure legends to judge whether each antibody use is supported by a valid
positive / negative / orthogonal control. The server does NOT run this judgement
— it supplies what a reader cannot derive from the paper in front of it: what a
given control METHOD can establish, which is a fixed property of the method and
not a judgement about any paper.

**The purpose is to enable a reader, not to measure one.** Get the most out of a
highly capable model; help a less capable one make fewer common mistakes. Those
are different jobs and only the second wants detail: the rules that BIND are the
floor, and a capable model clears them without being told. Anything here that
reads as an imperative procedure is scaffold, and scaffold that a reader does not
need is a cost — it anchors a strategy in place of one the model might choose
better on a paper nobody anticipated.

Serving one canonical rubric does also make a model-independent benchmark
possible, and that is a by-product rather than the reason. Where the two pull
apart — a wording change that would improve what a reader is told, against a
stamp an old run was scored under — the reader wins and the run is re-scored.

Lifted from ``AI_Antibody_Extraction_Prompt_v2.md`` (the decision trees + the
confidence convention + the primary-antibody scope). Output shape is kept LEAN
here — a controls verdict per antibody × application × figure, NOT the full
Validation Record proforma (that record serves a separate reporting purpose).

Versioned: bump ``CONTROLS_RUBRIC_VERSION`` when what a caller is TOLD changes, so
a result can be reproduced against the text that produced it. The stamp is a
record, not a gate — it exists to make a past run interpretable, never to decide
whether this file may be rewritten.
"""
from __future__ import annotations

# v7: the output/rendering section was cut. It duplicated, word for word, what the
# scan_controls tool description and the `note` on every result already say, and
# the rubric now ships INLINE in each scan_controls reply — so that third copy was
# paid for on every call. Only formatting was removed; every decision tree (scope,
# positive/detection, negative/selectivity, pseudo-controls, orthogonal,
# independent evidence, confidence, point-the-reader-to-the-data) is unchanged.
# v8: the scope statement was added. Of 67 controls extracted in the 100-pair
# benchmark, 8 were Uhlen pillars this rubric does not count as validating — 6
# overexpression (pillar 4, classed `detection`) and 2 orthogonal (pillar 2). The
# position is defensible and was applied consistently; it was simply never stated,
# so a "no" read as "this paper showed no validation" where the rubric meant "no
# GENETIC validation". No decision tree changed — only what the reader is told
# about which pillar the answer covers.
# v9: the reagent<->control LINKAGE rule changed, and the verdict became
# three-valued. Until v8 a control counted for an antibody only if their figure
# lists shared a string. A 99-paper blinded benchmark varied only that rule over 72
# scoreable pairs: intersecting figure lists gave sensitivity 0.696, panel-
# insensitive matching 0.913, and NO figure test at all 1.000 — at an IDENTICAL
# specificity of 0.959 in all three. Co-location bought no discrimination; it only
# destroyed sensitivity, because papers establish a reagent in one figure and use
# it in another as a matter of course. So presence is now scoped to the PAPER, the
# figure is reported rather than required, and the concern co-location stood in for
# — a genetic manipulation that this antibody was never read out against — became
# its own value, `present_unlinked`, instead of being folded into a "no".
#
# For the NEXT benchmark, so it can attribute what it measures. WHAT MOVED:
#   * scope — a control is a candidate for a reagent when it is performed, genetic
#     and target-matched. Same paper is sufficient; supplementary counts fully; a
#     reagent whose figures were never reported now gets its candidates at all,
#     which under the figure gate was structurally impossible.
#   * the verdict — `control_status` is three-valued, and `paper_control` carries
#     it as yes / unlinked / no rather than yes / no.
#   * linkage is decided by EVIDENCE, and there are three routes, reported per
#     candidate as `linkage_basis`: the paper NAMES this reagent as the detector
#     (`named`); the control and the antibody's use are the same PANEL
#     (`same_panel`); or the text names an antibody-based readout of the
#     manipulated material (`stated_readout`). Same-panel co-location is kept as
#     evidence precisely because it is a bad GATE and a good SIGNAL — the benchmark
#     measured the gate, not the signal. And `detected_with` naming a DIFFERENT
#     reported reagent is negative evidence that outranks all three: that is the
#     paper saying the control was read out with something else.
#   * evidence is taken from the Methods AND the legends. An earlier draft told the
#     caller to quote the legend alone; that is wrong about how papers are written.
#     The legend gives the assay and frequently names no reagent; the Methods carry
#     the catalogue number and the application. Requiring one section to hold both
#     halves would refuse most competently-controlled papers.
#   * `coverage` reports what the caller supplied and what its gaps cost. With no
#     controls passed in every row reads `absent`, which is a statement about the
#     input and looks identical to a paper that genuinely shows none.
#   * figure comparison, where it is still used for the ANNOTATION, is
#     panel-insensitive and case-insensitive. `3B` vs `3b` was a silent miss.
#   * `control_figures` now holds where the CONTROL is. Under the gate the
#     control's figure and the antibody's were the same string by construction.
# WHAT DID NOT MOVE: the target match (unchanged, and the benchmark reached
# sensitivity 1.000 with it exactly as it is), the pseudo-control exclusions,
# `performed_in_paper`, "a control with no named target validates nothing",
# "a control for gene X validates only anti-X", the genetic-pillar scope,
# `CONTROL_CLASSES`, and the two axes.
#
# NOTE FOR SCORING: the benchmark's winning rule — a target-matched genetic
# control, performed in the paper — is the UNION of `demonstrated` and
# `present_unlinked`, not `demonstrated` alone. Scoring `demonstrated` as the
# positive class measures a stricter question than v8's boolean did, and one the
# 99-paper run never scored. Say which you scored.
# v10 adds no classification and moves no threshold: `_controls_for` reaches the
# same verdict on the same evidence it did in v9, and the benchmark numbers above
# still describe it. What changed is when the server is willing to SAY `absent`.
# `absent` asserts that no genetic manipulation of this target appears anywhere in
# the paper, and a caller that read only the Methods produced a table of it with
# no basis whatever; such rows now read `not_assessed` (see
# `mcp_servers/common/sections.py`). The version moves because the scope wording a
# caller reads has changed, which is the rule this file states above -- not because
# the rubric decides anything differently.
CONTROLS_RUBRIC_VERSION = "v11"

# What each control TYPE means. The caller reports what it SAW ("this figure shows
# an isotype control"); the server decides what that COUNTS AS. Keeping the map
# here — and refusing a caller-supplied class — is what stops a peptide block being
# reported as evidence of selectivity, whoever or whatever is doing the reading.
# This is a fixed property of the method, not a judgement about a paper.
#
#   selectivity — removes the TARGET. The only class that tests whether the signal
#                 IS the target, and only for that same gene.
#   pseudo      — removes or occupies the ANTIBODY, not the target, so it cannot
#                 establish selectivity: peptide/antigen competition (the Fab is
#                 occupied, abolishing ALL binding), secondary-only (background
#                 only), isotype (non-specific isotype/Fc binding).
#   detection   — shows the antibody CAN detect the target, not that it is
#                 selective (overexpression, recombinant, positive control).
#   orthogonal  — an antibody-independent measurement of the same target.
CONTROL_CLASSES = {
    "knockout": "selectivity",
    "knockdown": "selectivity",
    "crispr": "selectivity",
    "null_line": "selectivity",
    "peptide_competition": "pseudo",
    "secondary_only": "pseudo",
    "isotype": "pseudo",
    "vehicle": "pseudo",
    "overexpression": "detection",
    "positive_control": "detection",
    "tagged_expression": "detection",
    "recombinant": "detection",
    "orthogonal": "orthogonal",
    "mass_spectrometry": "orthogonal",
    "rna_concordance": "orthogonal",
}

#: Only these classes test selectivity. Everything else is reported as what it is.
SELECTIVITY_CLASSES = {"selectivity"}

#: Spellings a reader may reasonably use for the same method. The map above is the
#: authority on what a type COUNTS AS; this is the authority on what counts as that
#: type being named. They are separate because they fail differently: an unknown
#: CLASS would be a judgement the server has no business making, while an unknown
#: SPELLING is only the server's vocabulary being narrower than the literature's.
#:
#: The design principle these serve: get the most out of a highly capable model,
#: and help a less capable one make fewer common mistakes. A model that reports
#: `peptide_competition` never meets this map. One that writes what the paper
#: printed — "no primary antibody", "pre-absorption" — is reading perfectly well
#: and would otherwise land in `unclassified`, where the very thing worth telling
#: it goes unsaid.
#:
#: Deliberately NOT here: `kd`. It is knockdown to a reader and the dissociation
#: constant to a biochemist, and this surface has already shipped that defect once.
#: An ambiguous spelling belongs in `unclassified`, which now asks.
CONTROL_TYPE_ALIASES = {
    # pseudo — removes or occupies the ANTIBODY, not the target
    "no_primary": "secondary_only",
    "no_primary_antibody": "secondary_only",
    "primary_omitted": "secondary_only",
    "omit_primary": "secondary_only",
    "secondary_alone": "secondary_only",
    "blocking_peptide": "peptide_competition",
    "peptide_block": "peptide_competition",
    "peptide_blocking": "peptide_competition",
    "peptide_preabsorption": "peptide_competition",
    "preabsorption": "peptide_competition",
    "pre_absorption": "peptide_competition",
    "preadsorption": "peptide_competition",
    "pre_adsorption": "peptide_competition",
    "antigen_competition": "peptide_competition",
    "immunogen_competition": "peptide_competition",
    "igg_control": "isotype",
    "control_igg": "isotype",
    "nonspecific_igg": "isotype",
    "isotype_control": "isotype",
    "isotype_matched": "isotype",
    "isotype_matched_control": "isotype",
    # selectivity — removes the TARGET
    "ko": "knockout",
    "knock_out": "knockout",
    "crispr_ko": "knockout",
    "crispr_knockout": "knockout",
    "knockout_line": "knockout",
    "knock_down": "knockdown",
    "sirna": "knockdown",
    "shrna": "knockdown",
    "rnai": "knockdown",
    "morpholino": "knockdown",
    "null_cell_line": "null_line",
    "target_null": "null_line",
    # detection — shows the antibody CAN bind, not that the signal is selective
    "recombinant_protein": "recombinant",
    "overexpressing": "overexpression",
    "over_expression": "overexpression",
    "transient_overexpression": "overexpression",
    # orthogonal — an antibody-independent measurement of the same target
    "mass_spec": "mass_spectrometry",
    "mrna_concordance": "rna_concordance",
    "qpcr_concordance": "rna_concordance",
}


def canonical_type(raw):
    """The caller's word for a control -> the spelling ``CONTROL_CLASSES`` knows.

    Returns the type unchanged when nothing matches, so an unrecognised method
    stays visible as what was reported rather than being coerced into the nearest
    known class. Widening this map can only ADD a classification; it can never
    reclassify a type the map above already names, because those keys are checked
    first.
    """
    t = (str(raw or "").strip().lower().replace(" ", "_").replace("-", "_"))
    if t in CONTROL_CLASSES:
        return t
    return CONTROL_TYPE_ALIASES.get(t, t)


#: Said out loud when a caller reports a control type the server cannot place.
#: `unclassified` is SAFE — it can never set a verdict — but silence is the wrong
#: kind of safe: the reader is not told the thing that matters, which is that the
#: control was not counted and why nobody can count it.
UNCLASSIFIED_NOTE = (
    "{what} — reported as a control, and this server does not recognise the "
    "type, so it has NOT been counted as a selectivity control and cannot be. "
    "That is a gap in the server's vocabulary, not a verdict on the experiment. "
    "Say what the control REMOVES: if it removes the TARGET (knockout, "
    "knockdown, CRISPR, a null line) it tests selectivity and should be reported "
    "under one of those types; if it removes or occupies the ANTIBODY (no "
    "primary, isotype, peptide/antigen competition) it is a pseudo-control and "
    "tests selectivity for nothing. Report it in the answer either way — a paper "
    "showing something and a paper showing nothing are not the same paper."
)


#: The scaffold is versioned apart from the binds, and this is the whole reason:
#: the binds must be IDENTICAL for every caller or they are not rules, while the
#: scaffold is support and may legitimately differ — sent or not, and one day
#: shaped for a particular model. Anything comparing two runs has to record both,
#: or a difference cannot be attributed. An empty slot today, like `by_doi` was.
SCAFFOLD_VERSION = "s1"

#: ── The two halves ──────────────────────────────────────────────────────────
#:
#: BINDS: what a reader cannot derive from the paper in front of them — a fixed
#: property of a method, a convention of this dataset, or what this reply may
#: assert. Sent on every call. Short, and each rule carries its reason, because a
#: rule that is understood survives the case it does not name.
#:
#: SCAFFOLD: how to find those things in a paper. Sent when the caller's answer
#: to the briefing question, or the payload, shows it is wanted. Never sent as an
#: instruction: a capable reader may have a better way through a paper than ours,
#: and a procedure frozen in a server caps every reader at the day it was written.
#:
#: Written fresh rather than sliced out of the single string that preceded them.
#: The seam does not fall at section headings — nearly every section had a
#: sentence of each kind in it — so cutting would have produced two halves that
#: read as fragments of something else.
CONTROLS_BINDS = """\
# Antibody controls rubric ({version}) — the rules that BIND

These are not advice and they do not scale with how good you are. Each states
something you cannot derive from the paper in front of you: a fixed property of
a method, a convention of this dataset, or what this reply may assert. A reason
comes with each, because a rule you understand survives a case it does not name.

Everything about HOW to find these things in a paper is in the scaffold half,
which is advisory. If you have a better way of reading a paper than it
describes, use yours.

## Work only from the manuscript
Judge only from the manuscript text and figure legends you were given — never
from prior knowledge of the antibody or the gene. What OGA independently found
comes from this server; what the paper did comes from the paper.

## You are not the final arbiter
Your job is to LOCATE and CLASSIFY the controls a paper presents, and to point
the reader at the panel. Whether a figure shows what the paper implies is judged
at the figure, by a person. This holds for EVERY control type — knockout,
knockdown, peptide block, positive, orthogonal alike: surface the control and
its location; never certify from text that it worked.

## Scope — primary antibodies only
Cover only PRIMARY antibodies, directed against the biological target of
interest. Ignore secondaries (anti-mouse, anti-rabbit), loading controls
(β-actin, GAPDH, tubulin, vinculin) unless one is itself the target, isotype
controls, tag antibodies (anti-GFP/FLAG/Myc/V5/HA/His) and chemical dyes (DAPI,
MitoTracker, phalloidin).

## What each kind of control can establish
This is a property of the method, not a judgement about any paper, and it is the
one thing here no reader gets a vote on.

- **selectivity** — removes the TARGET: knockout, knockdown with confirmed loss,
  CRISPR, a naturally null line. The only kind that tests whether the signal IS
  the target.
- **detection** — shows the antibody CAN bind the target where it is abundant:
  overexpression, recombinant protein, a confirmed-expressing positive control.
  Necessary, and not selectivity: an antibody can detect an overexpressed target
  and still bind off-targets that the on-target signal swamps.
- **pseudo-control** — removes or occupies the ANTIBODY rather than the target,
  so it cannot distinguish specific binding from non-specific:
  - **peptide / antigen competition, blocking peptide, pre-absorption.** The
    peptide occupies the Fab and abolishes ALL binding, on-target and off. Loss
    of signal shows the antibody binds its immunogen, nothing more.
  - **secondary-only / no primary.** Detection-system background only.
  - **isotype control.** Non-specific isotype/Fc binding, not target selectivity.
  - **vehicle or untreated arms.** An experimental baseline, not a control for
    the antibody.
- **orthogonal** — the same target measured by a NON-antibody method (mass
  spectrometry, targeted proteomics, mRNA–protein concordance the paper shows).
  A different antibody to the same target is not orthogonal; it is still an
  antibody.

**A knockout or knockdown of gene X is a valid negative control only for the
anti-X antibody.** If losing X also lowers protein Y, that is a finding about Y
regulation, not a control for anti-Y.

## What this rubric counts as validating — the genetic pillar only
Only the genetic strategy sets a control verdict here. This is a scope
statement, not a claim that nothing else is evidence: detection and orthogonal
evidence are classified and reported as what they are, never promoted and never
discarded.

So `absent` means **no genetic selectivity control was found**. It does not mean
the paper showed no validation. Say which you mean — a reader working from Uhlen
et al.'s five pillars will otherwise hear an accusation where a scope was meant.

## Three questions, three answers — never one boolean
1. **Presence** — does the paper show a genetic manipulation of this target?
2. **Linkage** — was THIS antibody read out against that material?
3. **Result** — did the signal actually disappear, at the right size, in the
   right application? **Not answerable from text under any circumstances.**

Never assert the third, for any control type, however clearly a paper implies
it. You have not seen the figure.

So the verdict is three-valued:
- **demonstrated** — the genetic control is present and something in the paper
  ties it to this antibody. Still a candidate: question 3 is the reader's.
- **present_unlinked** — a genetic control for this target is in the paper, but
  nothing establishes that THIS antibody was tested against it. Common and
  legitimate. Reporting it as "no control" is a false accusation against a paper
  that may be perfectly well controlled.
- **absent** — no genetic manipulation of this target anywhere in the paper.

A genetic manipulation existing somewhere in a paper does not by itself validate
a reagent. That is what `present_unlinked` names.

## Two independent axes — never merge them
1. **In-paper controls** — did THIS paper control this antibody? (your judgement)
2. **Independent testing** — has OGA/YCharOS knockout-controlled this antibody?
   (a database fact, from `check_manuscript` / `scan_controls`, never from you)

A paper can be strong on one and weak on the other. Report them side by side and
never blend them into a score.

## A control validates the application it was read out in
A knockout confirmed by western blot says nothing about the same antibody used
for IHC — epitope availability differs between preparations. Report which
application each control was read out in and which the paper used the antibody
for. Where they do not meet, say so. It is a caveat, not a disqualification, and
it is never silent.

## Independent evidence is external, and is not a control
Only validation the paper CITES — a supplier's knockout data, a prior
publication validating the same antibody. Never a finding from the paper itself.

## Confidence convention
- **high** — clear and unambiguous. State it directly.
- **medium** — reasonable, some ambiguity. Prefix "[AI — check needed] ".
- **low** — uncertain. Prefix "[AI — check needed] " and say why.
- **none** — nothing found. Leave empty. NEVER fabricate a control.

## Output — presence is not proof
Render the concise table you are given, with `paper_control` / `control_status`,
`oga_result` and `gene_page` on each row; expand only on `focus`; collapse the
rest into `others` as one line; do not enumerate every antibody. Link media
files, do not embed them as cards.

Two rules that are this rubric's, however you present it:
- Keep the two axes separate. `control_status` (and its legacy `paper_control`
  spelling) is a candidate a reader confirms at the figure; `oga_result` is a
  database fact.
- Keep the three questions separate. Do not blend presence, linkage and result
  back into one number — no combined confidence score for a control.

If `focus` is empty, say so in one line and stop. Do not manufacture concern.
""".format(version=CONTROLS_RUBRIC_VERSION)

CONTROLS_SCAFFOLD = """\

---

# How people usually find these things ({scaffold_version})

Everything below is advisory. It describes how a competent reader gets through a
paper and where each answer tends to hide; it is not a procedure you owe anyone.
If you have a better way of reading the paper in front of you, use yours — the
rules above still bind either way.

## Read both the Methods and the legends — neither holds all of it
The four questions live in different sections, and a scan that reads one section
produces a confident-looking answer with a hole in it.

1. **Do the figures use antibodies, and where?** — figures and figure legends.
2. **Which antibodies, and which applications?** — **Methods.** That is where
   catalogue numbers, suppliers and applications are; a legend routinely says
   only "anti-SNCA", or names no reagent at all. The Methods also reveal **use no
   figure shows**: a reagent listed for an assay that produces no image, an "as
   previously described", a "data not shown". Report those too, with no figures.
   An antibody whose use cannot be located is one whose controls cannot be
   checked — which is NOT the same as the paper showing no control.
3. **Which controls, and of what kind?** — Methods, figures and legends. Sort
   them: selectivity / detection / pseudo / orthogonal. Most readers skip this
   step, and the classification above is what it turns on.
4. **Was this antibody read out against the control material?** — figures and
   legends, **including the supplementary ones.** This is the step that decides
   whether a control validates the reagent, and the one most often missed.

So do not quote only one section. A legend gives what a panel shows and how it
was detected; the Methods give which reagent that was. A quoted siRNA sequence
from the Methods, on its own, answers none of the four.

## Where the control sits is orientation — except when it is the same panel
**Do not require the control and the antibody's use to be in the same figure.**
Papers do not co-locate them: the standard structure is to establish the reagent
and its controls in one figure or the supplement, then use the antibody to
measure something in another — disease versus control, a time course, treatment
arms. A knockout panel in Fig 1 and the disease staining in Fig 4 is normal,
competent practice, and a rule requiring co-location marks it uncontrolled.

**Supplementary figures count fully.** Report the location as an annotation:

> Knockdown of SDC4 shown in Fig S1 (western blot); antibody used in Fig 3F.
> Different figure — confirm the band disappears at the expected size.

**But the same panel is evidence, not just orientation.** If the knockout panel
and the antibody's use are one panel, that panel IS this antibody read out on
manipulated material. Use it as one route to linkage, never as a requirement.

Three routes to linkage, and which one you used is worth reporting: the paper
NAMES this reagent as the detector; the control and the use are the same panel;
or the text names an antibody-based readout of the manipulated material (a
western blot, immunostaining, IHC/ICC, an IP, flow cytometry). If the paper
names a DIFFERENT antibody as what detected the manipulated material, say so —
that is the paper telling you this control does not validate the reagent in
hand, and it outranks any inference.

## Why `present_unlinked` is so common
A knockdown can be confirmed by qPCR, or blotted with a different antibody.
Methods sections describe how a knockdown was MADE; legends describe what was
DETECTED. Neither half is obliged to mention the other. So say it in those
words rather than reaching for "no control".

**One control, several antibodies.** If the paper uses more than one antibody
against the target and the control panel names none of them, one of them was on
that panel and the text does not say which. Say that rather than crediting them
all — and check the Methods, which is usually where a paper does name it.

## Working the control types
- Recombinant protein or an overexpression lysate → detection evidence, with the
  note that detecting abundant target shows the antibody CAN bind it, not that
  the signal in the real sample is selective.
- A cell line or tissue with independently confirmed expression, cited by the
  authors as a positive control → detection evidence, same caveat.
- A drug, disease or untreated group where the target merely changes or is
  "expected" → not a control. An experimental observation.
- siRNA/shRNA/RNAi knockdown → selectivity, and the paper should show the target
  is actually reduced.
- A line or tissue that genuinely lacks the target, cited as such → selectivity.
- Mass spectrometry or targeted proteomics on comparable samples, correlating →
  orthogonal.
- RT-qPCR or RNA-seq for the same gene, AND the paper explicitly shows
  mRNA–protein concordance → orthogonal, medium confidence.
- A functional or phenotypic assay → neither. It measures function, not the
  protein.

## Point the reader to the data
For every control, give its location AND what to check there:
- A knockout or knockdown: point at the panel and say the reader should confirm
  the signal DISAPPEARS, or clearly drops, at the target's expected size. Name
  the control's figure and the antibody's when they differ, and say whether the
  control's readout application matches the one the paper used.
- A positive or detection control: point at the panel showing the band or signal
  at the expected size.
- A control that is only asserted, or whose figure is not shown: say so —
  claimed, not demonstrated. Never upgrade "a control was mentioned" to "the
  antibody is validated here", and never write a sentence that says or implies
  the signal was lost.

## The other pillars, and what became of them
Of Uhlen et al.'s five validation pillars, this rubric counts the genetic
strategy alone as establishing selectivity. Orthogonal methods (pillar 2) and
overexpression or recombinant protein (pillar 4) are classified and reported as
what they are. They are not promoted, because neither shows the signal in the
sample is the target; they are not discarded, because a paper showing one and a
paper showing nothing are not the same paper, and only `other_controls` tells
them apart.
""".format(scaffold_version=SCAFFOLD_VERSION)

#: The whole artefact, for the MCP prompt and for anything that wants to read the
#: rubric as one document. What a given REPLY carries is decided per call.
CONTROLS_RUBRIC = CONTROLS_BINDS + CONTROLS_SCAFFOLD
