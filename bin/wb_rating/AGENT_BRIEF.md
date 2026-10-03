# Testing the western blot measurement on raw scans

OGA rates commercial antibodies from knockout (KO) and knockdown (KD) western
blots made by YCharOS. Each rating is one of three results:
- **M** Supportive
- **O** Supportive, but not selective
- **N** Not supportive

Until now ratings were made by eye. The aim is a measurement that rates blots
reproducibly, in a way that can be explained to a manufacturer. This kit is a
first attempt. It was built and tried only on the published figures (8-bit,
cropped, contrast-adjusted). The owner has now downloaded the raw scans, about
20 GB, and wants three answers:

1. **Does the measurement work on raw scans, and how could it be better?**
2. **Where should the decision lines sit?** Show what each choice does. The
   owner decides.
3. **Does a published figure give the same answer as its raw scan?** If it
   does, figures without a raw scan can be rated from the figure.

The kit is a starting point, not a specification. If the raw data suggest a
better way to measure, or a better rule than the one below, try it and show
the evidence. Examples would be fitting band shapes, reading areas instead of
heights, using the ladder, or different windows. Your work comes back to the
owner's coding session, which will decide what to keep.

## The measurement

For each WT/KO pair the code reads both lanes row by row and removes the
background. The **target** is the strongest WT band the KO loses (KO ≤ half of
WT) that stands above the noise. It then computes two numbers:

- **`local_share`**: of the WT signal near the target, the share that
  disappears in the KO. Near means ±20% of the target's kDa when a ladder is
  given, otherwise ±10% of the lane height.
- **`total_share`**: the same across the whole lane.

The owner's rule (v5):
- No band lost → **N**.
- Otherwise: both shares at or above their thresholds → **M**; one below →
  **O**; both below → **N**.

The near-target window is grounded in the size tolerance people already
accept. The Human Protein Atlas counts a band as the target if it is within
±20% of the predicted kDa, so any other band within ±20% of the target could
be taken for it. The window is centred on where the target actually runs,
because many proteins run away from their predicted size. Getting it right
needs ladder positions, and distance on the gel follows log kDa. The published
figures print ladder marks beside the blot, and the raw data may have a
separate marker image. Placing the ladder is worth the effort. Without it the
code falls back to ±10% of the lane height, which only approximates the
window.

The starting thresholds are 0.90 near the target (≤10% of what you'd measure
there is something else) and 0.50 overall (the target is most of the signal).
`core.py` documents the rest. Read it before changing anything.

On the published figures, total share already orders the owner's 195 decided
blots well:

| Owner's rating | Median total share |
|---|---|
| Supportive | 0.78 |
| Supportive, but not selective | 0.62 |
| Not supportive | 0.36 |

The owner's own total cut-off looks nearer 0.7–0.8. The raw scans should show
whether that holds.

## Tools

`python kit.py --work work --raw "<raw folder>" <step>`. This needs numpy and
Pillow; tifffile is optional. Steps skip work already done; `--redo` repeats
it. `python kit.py selftest` checks synthetic blots with known answers.

| Step | Does |
|---|---|
| `survey` | Lists the raw folder from file headers (`--stats N` reads the pixels of every Nth file) |
| `match` | Pairs raw files with published figures by catalogue number and gene → `manifest_draft.csv`; save the checked version as `manifest.csv` |
| `lanes` | Proposes lanes and writes `annotations/*.json` with overlays and contact sheets. Edit an annotation to correct it and set `"status": "checked"`. Proposals fail when the KO lane is blank. |
| `measure` | Measures raw and published images with the same code → `measurements.jsonl` |
| `compare` | Raw against published: the same-result rate and Bland–Altman limits of agreement |
| `degrade` | Applies each figure-making step to raw scans to see which ones move the numbers |
| `explore` | Agreement with the owner's decisions across threshold pairs |
| `review` | Writes `review.html` of the flagged blots for the owner |

`data/` holds the 1,747 published figures with their image URLs, and the
owner's 212 decisions.

Change, extend or replace any of it. Don't write into the raw folder.

Fitting the decision lines to the owner's 212 ratings is a good starting
point. The aim is to make the owner's judgement repeatable. With so few
parameters, check:
- **held-out agreement:** fit on some blots and test on the rest;
- **stability:** how far the fitted values move when the blots are resampled.

The first 94 ratings (`source`: review page 1) were made under the rules
before version 4. The other 118 were made under the current rules, so fit both
sets and see whether they differ. Say where the fitted values sit relative to
the principled starting ones. A manufacturer will ask what a threshold means,
not just that it fits.

## What to hand back

Write `work/REPORT.md`, keeping your code alongside it. Cover:
- what the raw data turned out to be;
- what you tried, and what worked;
- the raw-against-published result, judged against the owner's acceptance
  criterion (in the prompt);
- where the decision lines could sit;
- anything you think the owner should reconsider.

The owner's decided blots are the natural place to start.
