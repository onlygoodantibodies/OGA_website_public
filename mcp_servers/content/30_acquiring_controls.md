# 30 · Acquiring controls — how to actually find, buy, or make them

> **Note.** The facts here are drawn from the **Champions Workshop Framework PDF**
> (`core/static/core/champions/Champions_Workshop_Framework.pdf`) and the validation
> framework §2/§4 — both already published — gathered into one practical module.
> **Named suppliers are examples, not endorsements, and can change** — the tutor
> should present them as "typical, check current" rather than fixed
> recommendations. (Specific prices have been removed, since they drift.)

> ### 🧭 How to teach this — a beat plan (tutor only; do NOT read aloud)
> One beat at a time — ask and **wait**. This module should feel practical, like
> sorting out *their* next order — not a catalogue read aloud.
> 1. **Start from their sample.** Ask what cell/tissue they work in and whether it
>    expresses the target — steer to checking DepMap/ProCan first (Step 0).
> 2. **Positive control.** Ask what they'd use before offering options; steer to an
>    expressing line at endogenous levels, with an overexpression lysate as the cheap screen.
> 3. **Negative control.** Ask how they'd get a KO/KD if they can't make one; surface
>    the OGA database and commercial options.
> 4. **Economics.** Ask if they've thought about free-sample pre-screening or refund
>    leverage — then fill in.
> 5. **No KO data?** Ask what they'd do if nothing exists for their target; land the
>    fallbacks + nominating the gene.
> 6. **Only then** the quiz (`get_module_quiz`).

This is the module that makes the exercise feel *useful*, not just educational.
Once a learner knows *which* controls count (Module 20), the next question is
always "…so how do I get them?" Good news: it's often cheaper and more achievable
than people assume.

---

## Step 0 — before you buy anything: find a cell that expresses the target

Use expression datasets both to **pick an expressing cell line** to use as your
positive control (and to KO/KD as the matched negative) *and* to confirm your
chosen sample expresses the target — so a negative result means "antibody/absent,"
not "wrong sample":
- **DepMap** RNA-seq expression (browse/rank cell lines by your target), and
  **ProCan-DepMapSanger** mass-spec (949 lines) for protein-level confirmation.
- The **Human Protein Atlas** is also useful for tissue/cell-line expression.
- TPM ≥ 2.5 is a common but non-definitive cutoff; mass-spec confirmation is
  stronger. Picking a line that expresses the target at *relevant* levels is far
  more informative than reaching for overexpression.

## Positive controls — the target at relevant levels

**The best positive control is your target at endogenous levels** — so start by
finding a cell line/tissue that *actually expresses it* (Step 0), and use that.
Overexpression is a **screening / fallback** tool, not the ideal (it's
supra-physiological — Module 20).

| Option | Cost / effort | When to use |
|---|---|---|
| **An expressing cell of interest** (confirmed by RNA-seq/proteomics) | your existing material | **First choice** — the target at relevant levels, in the sample you care about. Pair it with a KO/KD of the *same* cell as the negative. |
| **CRISPR knock-in / endogenous tag** | most effort | When you need tagged endogenous-level confirmation. |
| **Lentiviral / inducible stable line** | moderate effort | When the target isn't reliably expressed anywhere accessible — titrate the level *toward* physiological rather than raw over-expression. |
| **Commercial overexpression lysate** (e.g. OriGene) | low-cost; often discounted via university purchasing | **Screening only** — a fast, cheap way to *rule out* a bad antibody (can't see overexpressed protein → drop it). Not a substitute for endogenous validation. |
| **Clone your own overexpression vector** (e.g. from **Addgene**) | low one-off cost, then unlimited reusable lysate | Best value if you'll need screening lysate repeatedly. |

## Negative controls — target removed or absent

The strongest setup is a **KO or knockdown in the *same* expressing cell** you use
as your positive — same background, relevant level.

| Option | Notes |
|---|---|
| **Knockout cell line in your cell type** | Gold standard. Verify it's a true KO (~30% of commercial KO lines aren't). **Commercial KO lines** are available from **Horizon Discovery** and **Abcam** (among others); repositories and published lines are also sources, and the OGA database catalogues KO-controlled data. |
| **KO in a different cell type** | The YCharOS/OGA approach — searchable characterisation data even when you can't make your own KO. |
| **siRNA / shRNA knockdown** | When KO isn't feasible — and a useful research tool for studying the target anyway. **siRNA/shRNA reagents** are available from **Horizon Discovery (Dharmacon)**, **Abcam**, and others. Confirm efficiency by RT-qPCR; interpret partial reduction cautiously; watch off-target effects. |
| **Non-expressing line / tissue** | Cheapest, weakest — confirm non-expression with expression data. |
| **FFPE KO cell pellets** (for human-tissue IHC) | Stain alongside your sections with the same protocol. |

## Finding existing genetic-validation evidence (before you generate your own)

Search these *first* — someone may already have done the work:
- **OGA Antibody Database** — knockout-controlled data across WB/IP/IF/flow.
- **BenchSci** — filter to **genetic** verification; look at the published images.
- **CiteAb** — filter to **knockout/knockdown** verification.
- **Labome** — curated KO-validation.
- **HCDM** (hcdm.org) — workshop-verified **CD-marker** clones for flow. Being sold
  as "anti-CD-whatever" does **not** mean it went through this process.
- **Google Images** — "[target] knockout validated antibody [application]".

**Read the images, not the tick-boxes.** Check the application and sample type
match yours. And **prefer recombinant antibodies** — highest pass rates (~67% WB,
~48% IF).

## Practical economics (the bits people appreciate most)

- **It's cheaper than failing.** The upfront cost of a validated antibody (and a
  positive control to check it) is almost always less than failed experiments,
  wasted samples, and repeat purchasing.
- **Free samples + a positive control = a smart pre-screen.** Some vendors offer
  free samples. With a good positive control you can screen a free sample *before*
  committing budget.
- **A positive control is your refund leverage.** If you have strong evidence an
  antibody fails against overexpressed protein, vendors are far more likely to
  issue a refund — but only if you have the evidence.
- **Clone names > product names.** The same clone is often sold by several vendors
  under different names/catalogue numbers — sometimes at very different prices.

## When there's no knockout data for your target

Common, and not a dead end *(Champions FAQ)*:
- Use the best available evidence — independent antibody concordance, orthogonal
  data, tagged-expression data (the five pillars; framework §2/§6).
- You may need to **generate your own controls** — start with the cheap
  overexpression-lysate positive control above.
- **Nominate the target** for future OGA characterisation via the Contact page.
- The general principles still hold: prioritise recombinant antibodies, look for
  knockout-validated vendor data, check independent databases.

## The conversation with your supervisor

A frequent real-world blocker: *"use the antibody the lab has always used."* The
framing that works *(Champions FAQ)*: you're not challenging their judgement —
you're applying a standard of evidence that funders and journals increasingly
require. Frame it as **future-proofing the work**. The e-learning and this exercise
give you the language and the evidence to have that conversation constructively.
