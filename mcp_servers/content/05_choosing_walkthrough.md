# 05 · The antibody & control decision walkthrough (START HERE)

> This is the **opening** of the exercise — before any module or quiz. It is
> reflective and supportive, **not** a scored assessment. The tutor listens to how
> the learner already works, gives grounded feedback, then walks them through how
> to choose an antibody and its controls — either on a worked example or on a real
> decision they're facing right now.

## Step 0 — Elicit their current process (DO NOT judge or score)

Open by asking the learner, in their own words, **how they currently choose an
antibody, and how they choose controls for specificity / selectivity.** Prompts:

- "When you need an antibody for a target, how do you decide which one to buy?"
- "How do you check it actually detects your protein — what controls do you run?"

Let them explain fully. **Do not grade, score, correct mid-flow, or make them feel
tested.** This is reflection. Just listen and ask clarifying questions.

## Step 1 — Reflect their process back, with grounded feedback

Now map what they described onto our framework — **affirm what's already good**, and
**gently surface gaps** (never a checklist of failures). Ground every point in the
material (modules `10`/`20`/`30`) and, whenever they mention a specific antibody or
gene, in `target_evidence` (the OGA data) — never invent performance or prices.

Common, high-value things to reflect on if they come up:
- Leaning on the **datasheet, brand, or citation count** → citations reflect
  popularity, not performance; ask what *evidence* would actually convince them.
- Using **pseudo-controls** (isotype, peptide-block, secondary-only, loading) as
  "specificity" checks → acknowledge they're useful for *their own* question, then
  ask what actually shows the signal is the target (Module 20).
- Treating **overexpression** as validation → it's a screening tool; the ideal is
  the target at endogenous levels + a KO/KD (Module 20/30).
- Not **searching existing genetic-validation data** first → show them
  `target_evidence` for their gene, and the other sources (Module 30).

Keep it a conversation between colleagues, not a correction.

## Step 2 — Offer two ways to continue

> "We can do this two ways — whichever is more useful to you:
> **(a) theoretically**, on a worked example, or **(b) on a real decision you're
> facing right now** — a specific target and antibody you actually need to choose.
> The real one is more work but you'll come out with a documented validation plan
> you can keep."

- **Theoretical** → pick a representative target (or one they name) and walk the
  steps below as a worked example. Leads into the modules + quizzes.
- **Real** → their actual target/antibody decision. Walk the steps below **for
  their case**, use `get_plan_template` + `grade_plan` to shape and sense-check the
  plan, and — because they've produced a genuine validation plan — they can claim
  the **Antibody Validation Planning** certificate (evidence of proper planning),
  and document it formally on the website (below). Then continue to the modules.

## Step 3 — Walk the decision, step by step (both modes)

Use this as the backbone (it's the framework + controls + acquiring, applied):

1. **What does your scientific question need from the antibody?** Target-specific
   question → strongest (genetic-controlled) evidence; community marker → adopt
   critically; technical role → specificity may not be the point. (Module 10.)
2. **What evidence already exists?** Search *before* planning your own experiments:
   `target_evidence` (OGA), then BenchSci / CiteAb filtered to **genetic**
   verification, Labome, HCDM for CD markers. **Read the images, not tick-boxes**,
   and **prefer recombinant** antibodies. (Module 10/30.)
3. **Choose candidate antibody(ies):** recombinant where possible, genetic-validated
   in *your* application and sample type; remember the same **clone** is often sold
   under several product names.
4. **Design the controls:** the target at **endogenous levels in an expressing cell
   of interest**, paired with a **KO or knockdown in that same cell** as the matched
   negative. Do **not** rely on isotype / peptide-block / secondary-only / loading
   controls as your selectivity evidence. Name feasible fallbacks + caveats.
   (Module 20.)
5. **How to get the controls:** find an expressing line (DepMap / ProCan / HPA);
   sources — Horizon / Abcam for KO lines & siRNA, OriGene / Addgene for
   overexpression *screening*; free samples + a positive control as a pre-screen;
   a positive control is your evidence for a refund. (Module 30.)
6. **Document the plan** (the 7-item checklist). For a **real** decision, send them
   to the website to generate and keep a structured record:
   - **Validation Planner** — `https://onlygoodantibodies.co.uk/tools/validation-planner/`
   - **Validation Record** (document the outcome) — `https://onlygoodantibodies.co.uk/tools/validation-record/`

**Throughline (say it):** the controls you assemble for validation double as
**research tools** — a knockdown is a genuinely useful reagent for studying your
target anyway — so good validation controls upgrade the whole project, not just one
figure.

## After the walkthrough

Once they've been through this (theoretical or real), move on to the **modules** —
teach each conversationally, then the quiz (`get_module_quiz`) to confirm
understanding — and they collect their module certificates + the capstone. A learner
who did the **real** path also has the Antibody Validation Planning certificate.
