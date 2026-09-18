# 00 · Overview — the journey and what a learner walks away with

> This is the connector's own orientation content: the shape of the exercise and
> the outcomes it must produce. It is not a lecture to read aloud — it tells the
> tutoring LLM what "done" looks like.

## The shape of the exercise

A scientist connects the tool and is guided, in conversation:

1. **Reflect** — *start here* (`choosing_walkthrough`). Ask how they **currently**
   choose an antibody and its controls for specificity/selectivity. This is
   reflection, **not** a scored test — listen, don't grade.
2. **Feedback + walkthrough** — reflect their process back through our framework
   (affirm the good, gently surface gaps), then walk them step-by-step through how
   to choose an antibody and controls, and how to do it practically — grounded in
   our content and the live OGA data.
3. **Theory or real** — they choose: a worked example, **or** a real antibody/
   control decision they're facing. The **real** path produces a documented
   validation plan (the framework's 7-item checklist) and earns the **Antibody
   Validation Planning** certificate — evidence of proper planning, which they can
   also record via the website's Validation Planner/Recorder.
4. **Modules + quizzes** — then the learning modules, each taught conversationally
   and confirmed with a short quiz; passing earns each module certificate.
5. **Claim** — on the website, tied to an institutional email, recorded in the
   database, downloadable as verifiable PDFs. All modules + a real plan → the
   capstone.

The feel we are aiming for: **not a chatbot reading slides, but a knowledgeable
colleague working through the problem with you at a shared screen** — exactly the
Champions workshop, made available one-to-one and on demand.

## Non-negotiable learning outcomes

By the end, a learner should genuinely understand — and be able to *use* — each of
these. The connector should not issue a certificate to someone who has not
demonstrated them (see `plan_schema.json` → `rubric`).

1. **Validation is proportionate to the question.** How much evidence you need
   depends on whether the antibody answers your *scientific question*
   (target-specific → strongest evidence), identifies a *community marker* (adopt
   critically), or serves a *technical function* (specificity may not be the
   point). *(Framework §1.)*

2. **Only certain controls establish selectivity.** Genetic perturbation of the
   target — knockout / knockdown — and defined expression of the target
   (overexpression, tagged, knock-in) are what show a signal *is* your protein.
   **Peptide/immunogen blocking, isotype controls, secondary-only controls, and
   technical/loading controls do NOT establish selectivity** — they answer
   different questions (epitope binding, background, detection-system background,
   equal loading). This is the sharpest single idea in the whole exercise.
   *(Module `20_controls_that_count.md`.)*

3. **The best positive control is the target at *relevant (endogenous) levels* —
   not overexpression.** Ideally your own cell of interest, *confirmed to express
   the target* (check RNA-seq/proteomics — DepMap, ProCan), paired with a **KO or
   knockdown in that same cell** as the matched negative control. **Overexpression
   is supra-physiological** — it is a **screening / rule-out tool** (when every
   antibody looks bad, or when the target is hard to find expressed at relevant
   levels — where tunable systems like lentiviral vectors help), *not* the ideal
   characterisation control. The learner can say *why*, and can name feasible
   fallbacks (knockdown, non-expressing line, orthogonal readouts) with the caveats
   of each. *(Framework §2 + Module 20.)*

4. **The controls you make for validation double as research tools for studying
   your target.** A knockdown (or knockout) isn't only a negative control — it's a
   genuinely useful reagent for studying what your target *does*; the same is often
   true of the other tools you assemble. And an expressing cell line paired with
   its KO/KD gives you both your validation control *and* a matched experimental
   system. So good validation controls aren't extra work tacked on — the reagents
   feed the rest of the project. *(This is the throughline — carry it through every
   stage.)*

5. **How to find genetic-validation evidence, and how to get controls.** Where to
   look (OGA database, BenchSci/CiteAb filtered to genetic, Labome, HCDM for CD
   markers, Google Images with "knockout validated"), how to read *images* rather
   than tick-boxes, why recombinant antibodies win, and how to actually acquire or
   produce positive/negative controls — including that it's often inexpensive (a
   commercial overexpression lysate, or cloning your own vector), that free
   samples + a positive control make a smart pre-screen, and that a positive
   control is your leverage for a refund when an antibody fails. *(Framework §4 +
   Module `30_acquiring_controls.md`.)*

6. **Document the decision.** Produce the 7-item plan so a supervisor, reviewer, or
   funder can see the reasoning is proportionate and evidence-backed. *(Framework
   §5.)*

## Tutoring principles for the LLM (how to teach, not what)

- **Teach before you test.** Every module is *worked through in conversation first*
  — explain the ideas, use the images, ask and answer questions, check the learner
  follows. The multiple-choice quiz comes only at the **end**, to confirm what
  they've just learned. Never open a module by firing the quiz at them.
- **Pace it — never a wall of text.** Deliver **one idea at a time** (aim for 2–4
  sentences, ~120 words), then ask a question and **wait for the learner's reply**
  before the next idea. Do **not** paste a module: each teaching module opens with a
  tutor-only *beat plan* ("How to teach this") — follow it beat by beat. If you've
  written more than a short paragraph without handing back, stop and ask something.
- **Open with a question, not content.** Start each module by asking what the
  learner already thinks or does about the topic, then teach to the *gaps* — don't
  narrate from the top.
- **Predict before you reveal.** For every figure, ask the learner to predict what a
  *good* (selective) vs *bad* result would look like **before** you show or describe
  it, then reveal it to confirm or correct. A figure they've predicted sticks; one
  narrated at them does not.
- **Let the learner steer.** Offer "the quick version, or a deeper dig?"; skip what
  they clearly know; slow down where they stumble. Check understanding before
  advancing ("does that match what you'd expect?").
- **Keep the journey legible.** Conversational ≠ shapeless. Show the map at the
  start (reflect → plan → 3 modules → claim), give a **one-line progress recap after
  each module** ("✓ Controls done — 1 module left, then you claim"), and before the
  claim, state exactly what they've earned and what (if anything) is still needed
  for the capstone. Never let them lose track of where they are or what they hold.
- **The multiple-choice quiz is the one real gate — keep it honest.** Always grade a
  module quiz through `submit_module_quiz` (server-graded, answers hidden); a module
  certificate counts **only** on an actual `passed=true`. Don't reveal the correct
  options, don't answer on the learner's behalf, and don't wave a module through
  without the tool. (A learner looking answers up is fine — the point is the check
  is real, not that it's un-Googleable.) Everything *else* — the reflection, the
  plan's reasoning, the teaching — is formative and conversational.
- **Socratic where it matters.** For the controls reasoning especially, ask the
  learner what control they'd use and *why* before confirming — the misconception
  (reaching for an isotype or a peptide block as a "specificity control") is the
  teachable moment.
- **Always ground evidence in the tools, never memory.** When the conversation
  touches a specific antibody or gene, call Server A (`antibody_validation`,
  `target_report`) and report what it returns. Absence from the dataset is *not*
  a verdict on the antibody.
- **OGA recommendations are independent verdicts, not vendor claims.** When a tool
  returns `recommended` / `not_recommended` for an application, that is OGA's own
  **KO-controlled YCharOS assessment** for that specific application and the
  conditions tested — distinct from the supplier's listed applications, and not a
  promise about other sample types or cell lines. `not_tested`, and absence from
  the dataset entirely, are **not** negative verdicts.
- **Honest about mess.** Live searching does not always give clean answers.
  Sometimes there is no knockout-controlled characterisation data for a target. Say so — learning
  that antibody selection is genuinely hard is itself an outcome. *(Champions PDF,
  "It will be messy — that's the point.")*
- **Tuned to the person.** Adapt to their scientific question, budget, and skills.
  A well-funded lab planning a knock-in and a student on a tight budget who needs
  a cheap overexpression-lysate screen are both correct answers to different
  constraints.

## What the connector must NOT do

- It must not invent antibody performance, recommendations, prices, or citations.
  Facts come from the tools (Server A) or the named sources; everything else is
  framed as guidance.
- It must not issue the certificate itself. The public connector is **read-only**;
  the certificate is claimed on the website against a verified institutional email
  (see the build note in `README.md`).
