# Western blot rating kit: start here

This folder is everything a Cowork agent needs to test the blot measurement on
the raw scans: the code, the published figures' web addresses, your 212 decided
blots, and its brief (`AGENT_BRIEF.md`). It answers three questions:

1. **Does the measurement work on raw scans?** Are the lanes found, the numbers
   sensible, and the failures understood?
2. **Where should the two thresholds sit?** That is the near-target share and
   the total share. The agent maps what each choice would do and **you choose**.
3. **Does the published figure give the same answer as the raw scan?** If it
   does, figures with no raw scan can be rated from the figure.

The kit only reads the raw folder. It writes only into its own `work` folder,
and never touches the website or the database.

## Before the run

Decide what counts as "the published figure works", and put it in the prompt
before anyone sees the results. A suggestion:
- at least 90% of blots get the same result from raw and published;
- no blot moves between Supportive and Not supportive;
- both shares agree within ±0.10, using 95% limits of agreement.

## Setting up

1. Unzip this folder anywhere, for example `Documents/wb_kit`.
2. In Cowork, give the agent access to this folder and to the raw data folder.

## The prompt

> Read `AGENT_BRIEF.md` in the wb_kit folder and work on it. Use the kit as a
> starting point, and change or replace whatever the raw data call for. The raw scans are in
> `<RAW FOLDER PATH>`. My acceptance criterion for the published figures is
> `<…>`.

## What you get back

All of it is in `wb_kit/work/`:

| File | What it is |
|---|---|
| `REPORT.md` | The agent's findings, with questions for you |
| `review.html` | Open it in a browser: each flagged blot, raw beside published, with its numbers |
| `explore.md` | How the results change with the two thresholds, against your decisions |
| `compare.md` | Raw against published: how often they agree, and by how much the numbers differ |
| `degrade.md` | Which figure-making step changes the numbers |

## Bringing it back to a Claude Code session

Upload these and nothing from the raw folder:
- `work/REPORT.md`, `work/explore.md`, `work/compare.md` and `work/degrade.md`;
- `work/measurements.jsonl`;
- the `work/annotations` folder;
- `work/manifest.csv`;
- `work/CHANGES.md` and any changed `.py` files.

The session can then commit the code changes, rebuild the explainer page from
the raw-scan numbers, and turn your threshold choice into an
`apply_wb_judgements` file.
