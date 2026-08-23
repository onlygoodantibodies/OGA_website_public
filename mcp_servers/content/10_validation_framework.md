# 10 · A framework for planning antibody validation

> **Provenance:** faithful extraction of the published, peer-informed content on
> `core/templates/core/validation_framework.html`
> (onlygoodantibodies.co.uk/tools/validation-framework/). Built on a Delphi
> consensus study with 32 international experts (Blades, Biddle, Froud et al.,
> 2026) and compatible with the IWGAV five-pillar vocabulary (Uhlén et al., 2016).
> Keep this in sync with that template — see `README.md` → "drift".

> ### 🧭 How to teach this — a beat plan (tutor only; do NOT read aloud)
> One beat at a time — deliver the idea in 2–4 sentences, ask, and **wait** before
> the next. Ground everything in *their* target where you can (`target_evidence`).
> 1. **Diagnose first.** Ask how they currently decide *how much* validation an
>    antibody needs. Listen; teach to the gaps.
> 2. **Proportionate validation (§1).** The evidence bar depends on the question
>    (target-specific vs community marker vs technical). Ask which of the three
>    *their* experiment is.
> 3. **Antibody type.** Predict-before-reveal: ask which format they think is most
>    reproducible *before* confirming recombinant — and why (renewable, defined
>    sequence, highest pass rates).
> 4. **Controls (§2).** Ask what positive/negative they'd use for *their* target
>    before offering options; hand deeper control detail to the controls module.
> 5. **Reading the OGA database (§4).** Make the OGA-vs-vendor distinction concrete
>    on their gene. Ask what "recommended for WB" does and doesn't guarantee.
> 6. **Document it (§5).** Point at the 7-item plan; on the real path, build it here.

Antibodies are among the most widely used tools in biomedical research, but they
do not always bind exclusively to their intended targets. Using an antibody that
doesn't work as assumed can misdirect entire research programmes, waste funding,
and consume irreplaceable biological samples. This framework helps plan
**proportionate** validation — directing the most rigorous scrutiny at the
antibodies where the risk is highest.

**Why this matters — the cost of getting it wrong.** Poorly validated antibodies
are a leading driver of irreproducibility. One widely cited estimate put the cost
of irreproducible preclinical research at ~**US $28 billion per year in the US
alone**, with inadequate antibody validation a significant share (Freedman et al.,
2015). The cost is not only financial: irreproducible results erode trust in
published work, and unreliable reagents **waste irreplaceable human tissue and
animal-derived samples**. Choosing well is a scientific, financial, *and* ethical
decision.

![The prevalence of irreproducibility across five landmark replication studies — 51–89% of findings could not be reproduced (Freedman et al., 2015).](https://media.onlygoodantibodies.co.uk/lesson_uploads/2026/02/14/freedman-et-al.png)

![Breakdown of the estimated ~US $56 billion/year cost of irreproducible preclinical research in the US — roughly half is irreproducible, with biological reagents and reference materials the single largest contributor (Freedman et al., 2015).](https://media.onlygoodantibodies.co.uk/lesson_uploads/2026/02/14/freedmanpart2.png)

**Anchoring facts (from large-scale YCharOS testing):**
- **>50%** of commercial antibodies fail rigorous independent testing.
- **Recombinant antibodies have the highest pass rates** (~67% WB, ~48% IF) vs
  monoclonals (~41% WB, ~31% IF) and polyclonals (~27% WB, ~22% IF).
- **Genetic controls (knockout/knockdown) are far more predictive** of real-world
  performance than orthogonal vendor data alone.
- **Citation count reflects popularity, not performance** — highly cited
  antibodies fail at similar rates to less cited ones.

**Know your antibody type — it drives renewability and consistency:**
- **Recombinant** — made from a defined DNA sequence, reproduced consistently
  batch-to-batch; renewable; highest independent pass rates. **Prefer where
  available.**
- **Monoclonal (hybridoma)** — a single clone, renewable from the cell line; more
  consistent than polyclonal.
- **Polyclonal** — a mixture of antibodies from serum; **non-renewable** — once the
  serum runs out, each new batch differs. Batch variation is a major source of
  inconsistency, especially in long-term or collaborative studies.

![The three antibody formats — polyclonal, monoclonal (hybridoma), and recombinant — and their trade-offs in renewability and batch-to-batch consistency.](https://media.onlygoodantibodies.co.uk/lesson_uploads/2026/02/14/types-of-ab.png)

A good antibody is **well-characterised, renewable, fit-for-purpose, and used with
a clear understanding of its limitations** — not merely one that produces a signal.
Note that **selectivity is application-specific**: an antibody that works in one
application (e.g. WB) may fail in another (e.g. flow), so it must be confirmed in
the application *you* plan to use.

Three core principles run through everything: validation is **question-driven**,
**context-matched**, and **assay-specific**.

---

## §1 · What does your scientific question require from the antibody?

Not all experiments need the same level of validation. The critical factor is what
your question demands in terms of specificity. Three situations:

- **🎯 Target-specific question** — your question is about a particular protein
  (its role, expression, location). If the antibody detects something else, your
  conclusion is wrong. → **Strongest evidence required**: ideally genetic controls
  (knockout or knockdown) in the application and sample type you are using.
- **🏷️ Community-adopted marker** — the antibody identifies a cell population or
  phenotype; it defines context, not the subject of the experiment. → **Adopt
  critically**: existing community tools (e.g. HCDM workshop-verified CD-marker
  clones, OMAP-validated panels) may suffice *provided you confirm you are using
  the same clone as characterised by the consortium*. Watch for groupthink where
  the evidence base is thinner than assumed; if phenotyping is critical to the
  hypothesis, apply the target-specific standard.
- **⚙️ Technical function** — the antibody serves a process role (e.g. loading
  control) where specificity for the stated target isn't what matters. → **Consider
  alternatives**: total-protein staining (Ponceau S, Stain-Free gels) is often more
  reliable than antibody-based housekeeping controls. Document what the antibody is
  actually for.

## §2 · Selecting controls

Off-target binding depends on the expression levels of cross-reactive proteins in
*your specific sample*. Controls must match your experimental context. These are
the **five pillars** (Uhlén et al., 2016); genetic strategies are the most robust
when feasible, and for human-tissue IHC several strategies are usually needed.

**Positive controls (strongest → most accessible):**
- **Knockout–wild-type pair** — excellent, but verify the knockout independently
  (~30% of commercial KO lines may not be true knockouts).
- **CRISPR knock-in** — gene/tag inserted at endogenous levels; the most
  physiologically relevant positive control.
- **Tagged construct / transient transfection** — quick screening, but
  overexpression is supra-physiological and can't confirm detection at endogenous
  levels.
- **Commercial overexpression lysate** — shows where the protein *should* appear
  on a blot; a relatively low-cost way to rule out bad antibodies (e.g. OriGene).
- **Lentiviral stable expression** — stable exogenous expression with some control
  over level; less physiological than knock-in but more accessible.

> **Confirm expression first.** Use ProCan-DepMapSanger (mass-spec, 949 cell
> lines) or DepMap RNA data to confirm your line expresses the target. TPM ≥ 2.5
> is a commonly used but *non-definitive* threshold; mass-spec confirmation is
> stronger.

**Negative controls:**
- **Genetic knockout in your cell type** — gold standard; not feasible for human
  tissue or essential genes; verify independently.
- **KO in a different cell type** — the YCharOS approach; open data searchable via
  the OGA Antibody Database.
- **siRNA / shRNA knockdown** — useful when KO isn't feasible; confirm knockdown
  efficiency by RT-qPCR; partial reduction is harder to interpret.
- **Non-expressing cell line / tissue** — check proteomic/transcriptomic datasets;
  less conclusive (absence of signal may reflect low expression rather than
  antibody specificity).

> For **IHC on human tissue**: consider staining FFPE cell pellets from knockout
> cell lines alongside your tissue sections, using the same protocol.
> For **quantitative work**: you may need controls spanning a range of expression
> levels, or recombinant-protein spike-ins for ELISA-type assays.

*(The distinction between these meaningful controls and the controls that do NOT
establish selectivity is developed in `20_controls_that_count.md`.)*

## §3 · Carrying out the validation

Run your controls in the **exact assay system** you are using. An antibody that
works in one application does not necessarily work in another (denatured vs native,
fixed vs unfixed, intracellular vs surface).

A two-step approach (OGA Champions Workshop): **first** confirm the antibody can
detect the target protein (knockout, knockdown, or tagged expression); **then**
gather supportive evidence in your actual sample of interest.

![Two-step approach, step 1 — confirm the antibody can detect the target protein, via a knockout, knockdown, or tagged-expression approach. Each has caveats (a line that expresses the target and isn't dependent on it; efficient, confounder-aware knockdown; tagged expression that may not reflect endogenous levels). (OGA Champions Workshop.)](https://media.onlygoodantibodies.co.uk/lesson_images/img30.png)

![Two-step approach, step 2 — gather supportive evidence in your actual sample of interest, combining approaches: genetic manipulation of the target cell (ideally the first step, if feasible), independent antibodies, orthogonal antibody-independent methods, and cell treatments that should change the target — correlating the antibody signal with the expected change. (OGA Champions Workshop.)](https://media.onlygoodantibodies.co.uk/lesson_images/img31.png)

> **Decision rule:** *If you cannot demonstrate a clear difference between your
> positive and negative controls in the assay you intend to use — do not use that
> antibody in that assay.*

Protocol details matter — e.g. for flow cytometry, fixation/permeabilisation
(PFA-saponin, PFA-Triton, methanol) can fundamentally change performance.
**Corroborate with antibody-independent readouts** where possible (does RT-qPCR
agree with a protein-level change? does scRNA-seq support a flow shift?).

## §4 · Searching for existing evidence

Search for independent characterisation data *before* planning your own
experiments — and **prioritise recombinant antibodies** where available.

**Recommended sources:**
- **OGA Antibody Database** — curated, searchable characterisation with knockout
  controls across WB, IP, IF, and flow.
- **BenchSci** — AI-indexed published images; filter by *genetic* verification.
- **CiteAb** — citation-ranked; filter by knockdown/knockout verification.
- **Labome** — manually curated knockout-validation data.
- **HCDM** — workshop-verified clones for CD markers in flow cytometry.
- **Google Images** — "[target] knockout validated antibody [application]".

**Read the actual images, not the tick-boxes or claims.** Check the data is from
the same application and sample type as your planned experiment. Manufacturer data
*with genetic controls* replicates >80% of the time — but verify the specifics.

> **Clone names matter more than product names.** Multiple vendors often sell the
> same clone under different names/catalogue numbers. *(Champions workshop.)*

### Reading the OGA database (what the data means)

The OGA Antibody Database is a free, open-access resource. Search by **gene name**
to reach a gene page; if the target isn't in the database yet, use **"Nominate a
Target"** (via the Contact page) to request it for future validation. What to
understand on a gene / antibody detail page:

- **OGA-assessed recommendations ≠ vendor-listed applications.** An OGA
  recommendation means the antibody **passed standardised YCharOS testing with
  genetic (knockout) controls** for that application — an *independent* verdict. A
  vendor's "recommended applications" are the supplier's own claims. Trust the
  former; treat the latter as a starting point.
- **Each application (WB / IP / ICC-IF / FC) carries its own verdict** — a
  recommendation for one application says nothing about another.
- **Identity fields** — the **RRID** (a unique, citable identifier — put it in your
  methods), host species, clone ID, and clonality (recombinant / monoclonal /
  polyclonal).
- **The full validation report** for each gene is linked (hosted on **Zenodo** or
  **F1000Research**), with a **Copy Citation** button; testing follows the YCharOS
  consensus characterisation protocol (Ayoubi et al., *Nat Protocols*, 2024;
  doi:10.1038/s41596-024-01095-8).
- **A per-gene schematic** at the top of each gene page shows what *excellent*
  performance looks like in each application, to help you interpret the real data;
  some applications add further guidance behind the application filter.

> **What OGA recommendations do and don't tell you.** A recommendation reflects
> selectivity **in the application and conditions YCharOS tested** — it does not
> guarantee performance in a different sample type or cell line (which may express a
> cross-reacting protein). And **absence from the database is not a verdict**: it
> usually means the target simply hasn't been characterised yet.
>
> *(This is exactly the data the connector's `target_report` / `antibody_validation`
> tools return — per-application `recommended` / `not_recommended` / `not_tested`,
> RRIDs, and report DOIs — so ground every antibody claim in those tools.)*

## §5 · Documenting your plan — the 7-item checklist

For each antibody-dependent experiment, record:

1. **What you are trying to show**, and which application and sample type you use.
2. **What your scientific question requires** from the antibody — high specificity,
   comparability with community tools, or a technical function *(§1)*.
3. **What existing evidence you found (or did not find)** and where you searched
   *(§4)*.
4. **Your positive and negative control strategy** — the specific materials you
   will use and how closely they match your experimental sample *(§2)*.
5. **The antibody identity** — vendor, catalogue number, lot number, clone name,
   RRID, host species, and dilution/concentration.
6. **Any antibody-independent readouts** you will use to corroborate findings.
7. **The outcome of validation** and your decision to *proceed, reject, or test
   further*.

*(Items 1–6 are planned before you run anything — the Validation Planner covers
these. Item 7 is completed after validation — the Validation Recorder covers it.
This checklist is encoded as `plan_schema.json`.)*

## §6 · How this fits with other initiatives

Built on the Delphi consensus (32 experts) and compatible with the **IWGAV
five-pillar framework** (genetic, orthogonal, independent antibody, recombinant
expression, capture mass spectrometry). The panel reached consensus that
researchers should be *trained* in antibody validation, institutions should embed
validation expectations into research-integrity frameworks, and funders should
require validation plans in grant applications.

Further resources: IWGAV five-pillar framework (Uhlén et al., 2016, *Nat Methods*
13:823–827); YCharOS consensus characterization protocol (Ayoubi et al.,
*Nat Protocols*, 2024; doi:10.1038/s41596-024-01095-8); the reproducibility of
genetically-validated antibody data (Ayoubi et al., 2023); EuroMAbNet practical
guide.
