# 20 · Controls that count — which controls establish selectivity, and which do not

> **Reviewed and signed off** (2026-07). Consistent with the OGA validation
> framework, the OGA Academy e-learning, and the standard literature (Uhlén et
> al., 2016; Ayoubi/YCharOS 2023; Fritschy 2008, *Eur J Neurosci*; Bordeaux et
> al., 2010).

> ### 🧭 How to teach this — a beat plan (tutor only; do NOT read aloud)
> Work through these **one at a time**: deliver the idea in 2–4 sentences, ask its
> question, and **wait** for the learner before the next. This is the module where
> Socratic pacing matters most — the misconception *is* the lesson.
> 1. **Diagnose first.** "To prove a Western-blot band really *is* your protein,
>    what control do you reach for?" Don't correct yet — their answer sets up the lesson.
> 2. **The one question.** Land it: a control establishes selectivity *only if
>    changing the amount of target changes the signal*. Ask them to restate it in
>    their own words.
> 3. **The crux.** If they offered isotype / peptide-block / secondary-only /
>    loading as their *selectivity* evidence — dwell here. Ask what question that
>    control actually answers *before* you reveal it's the wrong tool.
> 4. **Endogenous > overexpression.** Ask *why* overexpression is a screening tool,
>    not the ideal positive, before explaining.
> 5. **The five pillars.** Introduce them as complementary, not a checklist.
>    Predict-before-reveal: before each strategy figure, ask what a *pass* looks like.
> 6. **Read real data.** Flow figure — "what should the KO histogram look like if the
>    antibody is selective?" *then* show it. Same for the WB (predict the KO lane) and
>    the tagged figure.
> 7. **Only now** offer the quiz (`get_module_quiz`).

## The one question every selectivity control must answer

> **"Is the signal I am measuring actually my target protein, and not something
> else the antibody also binds?"**

A control **establishes selectivity only if changing the amount of the target
protein changes the signal in the expected direction.** That is the whole idea.
Everything below is sorted by whether it can answer that question.

---

## Controls that DO establish selectivity

These work because they change (or define) the target protein itself, so the
signal has to track it.

**The ideal is the target at *endogenous* (physiological) levels.** The most
informative characterisation uses your **cell of interest, confirmed to express
the target**, paired with a **KO or knockdown in that same cell** as the matched
negative control — same background, relevant expression level, same protocol.
Confirm expression first with RNA-seq / proteomics (DepMap, ProCan) so a negative
result means "antibody/absent," not "wrong sample" (Module 30, Step 0).

| Control | Why it counts | Key caveat |
|---|---|---|
| **Endogenous target in an expressing cell — positive** | Detects the protein at *relevant* levels, in the sample you actually care about. The most meaningful positive control. | Only works if your cell of interest expresses the target — confirm with expression data first. |
| **Genetic knockout (KO) in that cell — negative** | Removes the target. Signal should disappear. The gold-standard negative control, especially paired with the expressing WT. | Not feasible for essential genes or most human tissue; ~30% of commercial KO lines aren't true KOs — verify independently. |
| **Knockdown (siRNA / shRNA) in that cell — negative** | Reduces the target. Signal should drop proportionally. | Partial reduction is harder to read; confirm efficiency by RT-qPCR; off-target effects exist. |
| **CRISPR knock-in / endogenous tag — positive** | Detects/tags the target at physiological levels. | Effort; a tag can perturb behaviour. |
| **Overexpression (transient / stable / lentiviral) or commercial overexpression lysate — screening positive** | Shows the antibody *can* detect the protein where it should appear. | **Supra-physiological — NOT a substitute for endogenous validation.** Use it to *screen or rule out* antibodies (when they all look bad), or when the target is hard to find expressed at relevant levels. Lentiviral/inducible systems let you tune the level closer to physiological. |
| **Non-expressing cell line / tissue — negative** | No target present → no true signal. | Weaker: absence of signal may reflect low expression rather than specificity; confirm with expression data. |

> **When overexpression is the right call.** It earns its place in two scenarios:
> (1) **screening many antibodies** to a hard target — if an antibody can't even
> see overexpressed protein, drop it cheaply; (2) the target isn't reliably
> expressed at relevant levels anywhere you can access — then a **level-controlled**
> system (e.g. a lentiviral/inducible vector titrated toward endogenous levels) is
> more informative than a raw over-expressing lysate. Neither replaces confirming
> the antibody at endogenous levels once you have a candidate.

**Supportive (not sufficient alone, but genuinely add confidence):**
- **Orthogonal / antibody-independent readouts** — RT-qPCR, RNA-seq, mass spec
  agreeing with the antibody result. One of the five pillars.
- **Independent-antibody concordance** — two antibodies to *different* epitopes
  agreeing. Weaker if both were raised against the same region.
- **Recombinant-expression pillar** — expressing the target and detecting it.

---

## The five pillars — the complementary validation strategies

The controls above map onto the **Five Pillars of Antibody Validation** (Uhlén et
al., 2016) — five *complementary* strategies for showing an antibody binds its
intended target. They are **not a ranked hierarchy or a sequential checklist**;
you combine whichever are feasible. But the evidence is clear that **genetic
strategies give the most direct and reproducible evidence of selectivity**: in
large-scale testing, YCharOS replicated vendor-supplied *genetic* validation data
far more often than data from the other approaches (Ayoubi et al., 2023) — the
effect strongest in Western blot and ICC-IF, and for recombinant antibodies.

> **Characterisation is not the same as validation.**
> *Characterisation* profiles what an antibody recognises across conditions,
> applications, and sample types — *what does this antibody do?* *Validation*
> confirms it works selectively for a **specific target in a specific application
> and sample type** — *does it work for my experiment?* An antibody can be
> well-characterised yet **not validated for the application or sample you need**.
> Validation is always context-dependent.

![The five pillars of antibody validation shown as notches on a key — Genetic, Orthogonal, Independent-antibody, Tagged-protein expression, and Immunoprecipitation–mass spectrometry. Genetic strategies are the most robust when feasible; human-tissue IHC usually needs several pillars together. (OGA Academy.)](https://media.onlygoodantibodies.co.uk/lesson_images/img24.png)

**1 · Genetic strategies (KO / KD) — the strongest evidence.** Signal must track
the target when you remove or reduce it.
- *Strengths:* most direct evidence of selectivity; eliminates off-target concerns,
  especially with **isogenic** WT and KO cells from the same parental line.
- *Limitations:* KO isn't always feasible (essential genes; targets poorly
  expressed in available lines); KD can be variable and may not fully remove the
  signal; needs suitable lines and careful design.

![Genetic strategy — knockout/knockdown of the target should cause loss or reduction of antibody staining: a *specific* antibody loses its band in the KO lane, a *non-specific* one does not. High-specificity and reliable when feasible; pitfalls include alternative translation start sites / splicing, and it is not applicable to human tissue or body fluids. (OGA Academy.)](https://media.onlygoodantibodies.co.uk/lesson_images/img25.png)

**2 · Orthogonal strategies — supporting evidence without genetic modification.**
Compare the antibody signal to an antibody-independent measurement (RNA-seq,
mass-spectrometry proteomics) across samples of varying expression.
- *Strengths:* no genetic manipulation; useful where KO/KD is impractical (primary
  tissue, clinical samples); confirms the target is present.
- *Limitations:* RNA doesn't always track protein; **correlation ≠ selectivity** (a
  different protein with a similar expression pattern can produce a false match);
  needs samples with real expression variation.

![Orthogonal strategy — correlate the antibody signal with an antibody-independent measure (e.g. RNA vs antibody staining across brain/liver/lung). Good correlation implies specificity, but requires several samples and RNA may not track protein. (OGA Academy.)](https://media.onlygoodantibodies.co.uk/lesson_images/img26.png)

**3 · Independent antibodies — cross-checking different clones.** Two or more
antibodies to *different epitopes* on the same protein giving the same result.
Common in IHC, ICC-IF, flow, and **ChIP**.
- *Strengths:* independent binding to separate epitopes raises confidence; useful
  when genetic controls are unavailable; broadly applicable.
- *Limitations:* epitope information is often proprietary, so "independence" is hard
  to confirm; both could **share** the same off-target; needs a variable-expression
  sample set to compare meaningfully.

![Independent-antibody strategy — two or more antibodies to different epitopes on the same protein should give a concordant pattern; misleading if both share the same non-specific staining, and epitope sequences are often proprietary. (OGA Academy.)](https://media.onlygoodantibodies.co.uk/lesson_images/img27.png)

**4 · Tagged expression systems — epitope or fluorescent tags.** A FLAG/HA/GFP tag
gives an independent reference; antibody signal co-localising with the tag supports
(doesn't prove) correct detection.
- *Strengths:* enables detection where endogenous expression is low/absent; the tag
  is a direct comparison signal; good for transient/stable models.
- *Limitations:* overexpression usually **exceeds physiological levels** and can
  mask off-target binding or create artefacts; the tag can perturb folding /
  localisation; it does **not** confirm the antibody detects the *endogenous,
  untagged* protein at normal levels.

![Tagged-protein strategy — a tagged construct lets you correlate antibody staining with the epitope-tag signal; useful for assessing selectivity against related family members, but overexpression does not prove specificity at endogenous levels and the tag may alter localisation. (OGA Academy.)](https://media.onlygoodantibodies.co.uk/lesson_images/img28.png)

**5 · Immunocapture + mass spectrometry (IP-MS) — identify what's pulled down.**
- *Strengths:* directly identifies the proteins the antibody binds rather than
  inferring selectivity; ideal for validating **IP** workflows; can reveal
  off-target or co-precipitating partners.
- *Limitations:* can't always separate the direct target from complex partners;
  false positives need careful interpretation; needs an MS facility; most
  informative for IP.

### Choosing a strategy
- **If KO/KD is feasible:** start with genetic validation, then add orthogonal data
  or independent antibodies for extra confidence.
- **If KO/KD is not feasible** (essential genes, rare tissue, poor expression):
  combine several lower-tier pillars (tagged expression, orthogonal, independent
  antibodies, IP-MS) into converging evidence — and be transparent about the
  limitations in your reporting.
- **For human-tissue IHC** (e.g. post-mortem brain): KO tissue is rarely available;
  consider **KO cell pellets processed identically** to the tissue as a
  complementary control. Expect to need multiple strategies.
- **Always:** interpret in the context of the specific application *and* sample
  type, remember performance in one application does not guarantee another, and
  document antibodies with **RRIDs and lot numbers** for reproducibility.

| Pillar | What it tests | Best for | Key limitation |
|---|---|---|---|
| **Genetic (KO/KD)** | Target dependence | Any application where KO/KD is possible | Not always feasible |
| **Orthogonal** | Correlation with independent data | Primary tissues, clinical samples | Correlation ≠ selectivity |
| **Independent antibodies** | Concordance across clones | IHC, ICC-IF, FC, ChIP | Both could share off-target binding |
| **Tagged expression** | Co-localisation with a tag | Low-expression targets | Overexpression artefacts |
| **IP-MS** | Direct target identification | IP workflows | Specialist equipment; co-precipitation |

---

## Controls that do NOT establish selectivity

Each of these is a *real, useful* control — but for a **different question**. Using
one as your evidence that a signal is your target is the single most common
validation mistake. The teaching point is not "these are bad"; it's "these do not
answer the selectivity question, so they cannot stand in for a genetic/expression
control."

### ✗ Peptide / immunogen blocking (pre-adsorption)
Pre-incubating the antibody with its immunising peptide and seeing the signal
disappear. **What it actually shows:** the antibody binds the immunising peptide —
nothing more. A *different* protein that shares or mimics that epitope would be
blocked in exactly the same way, so the signal vanishing tells you nothing about
whether the band/stain is your target. It confirms **epitope binding**, not
**target identity**. *(Fritschy 2008 is the classic critique.)*

### ✗ Isotype control
An irrelevant antibody of the same isotype, host, and concentration. **What it
actually shows:** the level of **non-specific / Fc-mediated background** for that
antibody class in your assay (useful in flow, IF, IHC). It says nothing about
whether *your* antibody binds the right protein — a perfectly matched isotype
control is silent on target identity.

### ✗ Secondary-only (no-primary) control
Omitting the primary antibody. **What it actually shows:** background from the
**secondary antibody and detection system**, plus autofluorescence. Essential for
interpreting your images, but it controls the *detection reagents*, not the
primary's specificity.

### ✗ Technical / loading controls (housekeeping proteins)
β-actin, GAPDH, tubulin, etc. **What they actually show:** equal **loading and
transfer** — a process control. They can even mislead, because housekeeping levels
vary with conditions; total-protein stains (Ponceau S, Stain-Free) are often more
reliable (framework §1). Nothing to do with your target antibody's selectivity.

### Also not sufficient on their own
- **"Band at the expected molecular weight."** Neither necessary nor sufficient.
  **Proteins do not always migrate at their predicted molecular weight** —
  post-translational modifications, proteolytic processing, differential SDS
  binding, and incomplete denaturation all shift apparent size. So a band at an
  *unexpected* size may still be your target, and a band at the *"right"* size may
  be off-target. MW is a weak corroborating clue, never a selectivity control.
- **Vendor "validated" / a datasheet tick-box / high citation count.** Not a
  control at all. Citations reflect popularity, not performance (Champions FAQ).

---

## Why this matters twice over — the throughline

Reaching for an isotype control or a peptide block *feels* rigorous, which is
exactly why the mistake persists. Naming the question each control answers fixes
it: only perturbing or defining the target answers "is this my protein?"

And here is the payoff to carry through the whole exercise:

> **The controls you make for validation double as research tools.** A knockdown or
> knockout isn't only a negative control — it's a genuinely useful reagent for
> studying what your target *does* (its phenotype, interactors, regulation), and an
> expressing cell line paired with its KO/KD gives you both your validation control
> *and* a matched experimental system for the rest of the project. The same is
> often true of the other tools you assemble. Isotype / secondary-only / loading
> controls, by contrast, only ever service the one blot or stain they sit on — they
> add nothing to the rest of the work. Investing in genuine selectivity controls
> pays off twice.

## Reading the data — what selectivity actually looks like

A validated antibody can still produce misleading data if you read it carelessly.
The point of the controls above is only realised when you can **read the images,
not the tick-boxes**. Three worked patterns (from the OGA Academy "Interpreting
validation data" module):

- **Flow cytometry — look for peak separation.** Stain WT and KO cells and overlay
  the histograms (with a secondary-only background control). *Good selectivity =
  clear separation between the WT and KO peaks, with the KO peak sitting close to
  the secondary-only control.* Overlapping WT/KO histograms mean the signal is
  **not** target-dependent. Watch the middle case: clear WT–KO separation but a KO
  peak still **above** secondary-only signals residual non-specific binding — usable
  only with careful extra controls. *(Academy example: SYT1 antibodies on
  HCT116 WT vs SYT1-KO, Biddle et al., 2024.)*

  ![Flow-cytometry validation of Synaptotagmin-1 (SYT1) antibodies: HCT116 WT (green) vs SYT1-KO (pink), with a secondary-only background control. Selective antibodies show clear WT–KO peak separation with the KO peak close to background; overlapping peaks indicate the signal is not target-dependent (Biddle et al., 2024).](https://media.onlygoodantibodies.co.uk/lesson_uploads/2026/02/14/syt_LE6VUrv.png)
- **Western blot — the band must depend on the target.** A *passing* WB shows a
  clear, specific band in WT that is **absent or markedly reduced in the KO lane**.
  A crisp band that is still present in the KO lane is off-target, no matter how
  clean it looks — and remember a band at the "expected" MW is neither necessary nor
  sufficient. An antibody that detects the target *plus* extra bands may still be
  usable **with additional controls**. *(Academy example: SERPINA1, a secreted
  protein — WT/KO lysates and supernatants.)*
- **Tagged expression — co-localisation, with the overexpression caveat.** With a
  target-GFP transfection (and a different-protein-GFP control), the most promising
  antibody is the one whose staining **matches the GFP** and is **absent in the
  control transfection**. But this alone does **not** prove the antibody detects the
  *endogenous* protein in a different cell — further pillars are needed. *(Academy
  example: TRPA1-GFP vs TRPM2-GFP in HEK293T.)*

  ![Tagged-expression readout — TRPA1-GFP (top rows) and a TRPM2-GFP control (bottom rows), probed with five antibodies. The GFP-reporter row marks transfected cells; a selective antibody's stain matches the GFP pattern for TRPA1 and is absent in the TRPM2 control (here mAb C-5). This supports — but does not prove — detection of the endogenous protein. (Academy example.)](https://media.onlygoodantibodies.co.uk/lesson_images/img35.png)

This is exactly what the OGA database's per-application images and per-session
evidence let a learner do — see "Reading the OGA database" in the framework (§4).

## How the exercise tests this (for the tutor)

Ask the learner, for their own target/assay, **which control tells them the signal
is their protein — and why**. If they offer a peptide block, isotype, secondary-
only, or loading control *as their selectivity evidence*, that's the teachable
moment: acknowledge the control is useful, then ask what question it actually
answers, and steer them to a genetic/expression control (or the best feasible
fallback). Getting this right is a hard gate in the grading rubric
(`plan_schema.json`).
