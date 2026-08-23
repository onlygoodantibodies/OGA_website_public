# OGA education content pack (v0.1 — DRAFT, needs scientific sign-off)

This folder is the **teaching backbone** for the "OGA Antibody Validation"
interactive-learning connector (the "A2" education track in `../VISION.md` /
`../A2_SCOPING.md`). It is deliberately **decoupled from the website HTML** so the
connector can load clean, versioned content that can't drift silently, and so the
owner (a scientist) can review the science as prose before any code exists.

This is **Phase (b)** of the agreed build order:

- **(b) content pack** — *this folder.* Reviewable prose + the plan schema. No
  user-facing change yet.
- **(c) certificate hardening** — verification code, institutional email, `kind`,
  a `/certificate/verify/<code>/` route, PDF QR — on the existing academy
  certificate (improves today's web certs too). Builds the manual in-person
  issue path.
- **(d) public teaching connector** — modules + quizzes + the planner scaffold,
  consuming read-only Server A for evidence; ends by linking the learner to the
  website certificate-claim page.

## The learning journey this pack drives

The finished experience is an interactive exercise a scientist works through with
an LLM (Claude / ChatGPT) that has the connector enabled. At the end — if they
share the same details an academy learner shares — they can claim an OGA
certificate on the website, recorded in the database like any academy certificate.

The material has **two sources**, joined by this pack:

1. **The academy e-learning** (`Lesson` / `LessonSection` / `Quiz` rows in
   `academy_db`) — read **live** by the connector, delivered conversationally,
   with the real quizzes graded against the real answers. *Not copied here* — it
   lives in the database and is authored in the academy admin.
2. **The validation framework + Champions workshop** — the "plan a validation /
   choose antibodies and controls for *your own* target" walkthrough. Its content
   lives on the website as HTML (`core/templates/core/validation_framework.html`,
   `validation_planner.html`) and in `core/static/core/champions/`. This pack
   **lifts that into reviewable modules** and adds the new content the vision asks
   for but the site doesn't yet have.

## Files

| File | What it is | Source |
|---|---|---|
| `00_overview.md` | The journey, learning outcomes, and the throughline | new framing |
| `10_validation_framework.md` | The 6-section framework + 7-item plan checklist; why-it-matters + antibody types; **Reading the OGA database** (data context) | `validation_framework.html` + OGA Academy M1/M3 |
| `20_controls_that_count.md` | Which controls establish selectivity, and which do **not**; the **Five Pillars** + reading FC/WB/tagged data | reviewed; + OGA Academy M2/M4 |
| `30_acquiring_controls.md` | **NEW** — how to actually find / buy / make positive and negative controls | lifted from the Champions PDF + framework |
| `plan_schema.json` | The validation-plan JSON schema + the grading rubric | new (built on the framework's 7 items) |
| `quizzes.json` | Per-module quizzes (answers + explanations) — the module-certificate gate; + the capstone spec | new |

## Curriculum structure & certificates (owner steer, 2026-07-16)

The exercise should be **broken into a few sub-modules, each with its own quiz +
certificate, plus an overall "capstone" certificate** for completing the whole
track. Proposed sub-modules (map each to a `Certificate`):

1. **Why validation matters + proportionate validation** — framework §1 (+ the
   academy intro lessons). *Cert: module.*
2. **Controls that count** — `20_controls_that_count.md`: which controls establish
   selectivity, endogenous-level positives, and the pseudo-controls that don't.
   *Cert: module.*
3. **Finding evidence & acquiring controls** — framework §4 + `30_acquiring_controls.md`
   (search databases; sourcing KO/siRNA; endogenous-first). *Cert: module.*
4. **The workshop — plan validation for your own target** — the planner
   (`plan_schema.json`), consuming Server A evidence. *Cert: workshop.*
5. **Capstone** — all sub-modules + the workshop plan. *Cert: overall/capstone.*

**Duplicates are allowed:** the owner is happy for a learner who repeats a
sub-module and answers correctly to receive another certificate (so certificate
issuance does **not** dedupe). The certificate `kind` field will expand from
`elearning | workshop` to also represent the individual sub-modules and the
capstone when the claim page is built.

## Status / provenance rules

- **`10_…` and `plan_schema.json` are faithful extractions** of content already
  published and peer-reviewed on the site. Low risk.
- **`20_…` (Controls that count) is reviewed and signed off (2026-07).** Its
  "controls that do **not** establish selectivity" taxonomy and the integrated
  **Five Pillars** treatment are consistent with the OGA Academy e-learning and the
  standard literature (Uhlén 2016; Ayoubi/YCharOS 2023; Fritschy 2008). The
  sign-off banner has been removed.
- **`30_…` still carries a light sign-off note** — its prices/suppliers are
  illustrative and drift; confirm the supplier steer still reflects current advice.

## Versioning + drift

- `PACK_VERSION` below is bumped on any content change; the connector reports it,
  and an issued certificate can record which pack version the learner was taught
  against (useful for the eLife "intervention" write-up).
- To prevent silent drift from the website, a later step should either (a) add a
  test asserting the framework prose here matches the source template, or (b)
  single-source it by rendering the HTML *from* this pack. Not done yet — noted in
  `../A2_SCOPING.md` §4c.

## Change log
- **0.1.0-draft** — initial pack (framework extraction + new modules 20/30 + plan
  schema).
- **0.2.0-draft** — owner scientific review applied: overexpression is **not** a
  good validation control (supra-physiological) — the ideal is the target at
  **endogenous levels** (an expressing cell of interest + a KO/KD in that same
  cell); overexpression demoted to a screening/rule-out tool (with level-controlled
  lentiviral for hard targets); throughline corrected to "controls double as
  research tools" (a knockdown is a useful tool for studying the target anyway);
  molecular-weight caveat expanded (proteins don't always run at predicted MW);
  Horizon Discovery + Abcam added as KO/siRNA sources; Step 0 reframed around
  *finding* an expressing line via RNA-seq/DepMap/HPA; curriculum split into
  sub-modules + a capstone certificate; duplicate certificates allowed.

- **0.2.1-draft** — tutoring flow tightened: every module is **taught
  conversationally first**, and the multiple-choice quiz only comes at the end to
  confirm understanding (00_overview "Teach before you test"; the connector's
  `get_module` / `get_module_quiz` / `learning_overview` guidance). No change to the
  quiz mechanism or the certificates.

- **0.3.0-draft** — reflection-first opening: new `05_choosing_walkthrough.md` +
  `choosing_walkthrough` tool. The exercise now OPENS by asking how the learner
  currently chooses an antibody + controls (unscored reflection), gives grounded
  feedback, then walks the decision step-by-step — **theoretically or on a real
  target**. The real path produces a validation plan and earns the **Antibody
  Validation Planning** certificate (renamed from "Workshop"), linked to the
  website Validation Planner/Recorder. Modules + quizzes follow.

- **0.4.0-draft** — **OGA Academy e-learning integrated** (the website's Modules
  1–4, supplied as the source of truth for the data context). `20_controls` gains
  the full **Five Pillars of validation** treatment (genetic / orthogonal /
  independent-antibody / tagged / IP-MS, with strengths, limitations, a
  strategy-choice guide and summary table — Academy M2) plus a **"reading the
  data"** section of worked FC/WB/tagged examples (Academy M4). `10_framework`
  gains a **"why it matters"** cost/ethics framing and antibody-type trade-offs
  (Academy M1) and a **"Reading the OGA database"** subsection explaining
  **OGA-assessed recommendations vs vendor-listed applications**, RRID/report
  fields, and the consensus protocol (Academy M3). Overview gains a tutor note that
  the tools' `recommended`/`not_recommended` verdicts are OGA's independent
  KO-controlled assessments, not vendor claims. Consensus-protocol citation
  kept as **Ayoubi et al., *Nat Protocols*, 2024 (doi:10.1038/s41596-024-01095-8)**
  (the version linked from the OGA homepage), now with the DOI. `20_controls` **reviewed and signed off** (banner
  removed); one new quiz question added to the framework and controls modules.
  (Server-side: the read-only `targets_without_ko_line` tool was removed — a
  public "no KO line" list is misleading.)

- **0.4.1-draft** — **tutorial figures embedded** from the OGA Academy via their
  public R2 custom-domain URLs (`media.onlygoodantibodies.co.uk/lesson_uploads/…`
  and `media.onlygoodantibodies.co.uk/lesson_images/…` — the public R2 domain, not
  the login-gated `onlygoodantibodies.co.uk/media/` Django route), so they render
  in a connected client even though the academy lesson *pages* need a login:
  - `10_framework`: the prevalence + cost-of-irreproducibility charts and the
    antibody-types comparison (§0); and the two-step approach diagrams in §3
    (step 1 "confirm the antibody detects the target", step 2 "supportive
    evidence in the sample of interest").
  - `20_controls`: the SYT1 flow-cytometry WT-vs-KO figure and the TRPA1
    tagged-expression data panel ("reading the data"); and the full set of Five
    Pillars strategy diagrams — the "validation is key" overview plus the
    genetic, orthogonal, independent-antibody, and tagged-protein schematics.
    (The remaining Academy figures — the HCDM/Tim-1 community-marker examples and
    additional raw TRPA1 orthogonal/tagged panels — were left out as redundant
    with these cleaner schematics.)

  **Specific prices removed** across the pack (they drift — kept qualitative
  "low-cost / check current"); `30_acquiring`'s cost column and banner updated
  accordingly. Consensus-protocol author corrected back to **Ayoubi et al.** (the
  earlier 0.4.0 note mis-attributed it to Bhargava); DOI retained.

- **0.4.2-draft** — **conversational pacing hardened** so the tutor drip-feeds and
  elicits instead of dumping text. Every teaching module (`framework`, `controls`,
  `acquiring`) now opens with a tutor-only **"🧭 How to teach this" beat plan**
  (idea → question → what to watch for), and the overview's tutoring principles plus
  the `get_module` / `learning_overview` guidance enforce: **one idea per turn**
  (~120 words) then ask and wait, **open with a question**, **predict-before-reveal**
  for every figure, let the learner steer the depth, and **never paste a module**.
  No learner-facing prose or quiz changes.

- **0.4.3-draft** — **journey clarity + an honest MC gate** (formative everywhere
  except the multiple-choice quizzes, which stay a real server-graded gate).
  `learning_overview` now returns an explicit **journey map** and a **canonical
  certificate taxonomy** — module certificates (×3), the **Antibody Validation
  Planning certificate**, and the **capstone** — with one name per certificate and
  a **signposting** instruction (show the map, recap after each module, summarise
  before the claim). `certificate_claim_link` now returns a **status** (earned so
  far / still needed for the capstone) so the learner never loses the thread.
  `get_module_quiz` payload + the overview's tutoring principles reinforce that the
  MC quiz is the one real gate: **always grade via `submit_module_quiz`**, never
  reveal the answers or answer on the learner's behalf, and never quiz before
  teaching (a learner looking answers up is fine — the check is still real). The
  "workshop" certificate is now consistently named the **Antibody Validation
  Planning certificate** across tools. No quiz questions or learner-facing module
  prose changed.

```
PACK_VERSION = 0.4.3-draft
```
