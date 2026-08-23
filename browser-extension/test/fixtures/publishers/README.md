# Publisher house-style fixtures

One file per publisher house style, reproducing **how that publisher typesets
and marks up a methods section** — not a saved copy of any real article. Real
article HTML is copyrighted, several of these publishers sit behind Cloudflare
bot checks (which blocked five papers during the v0.1.6 benchmark outright),
and a saved page is a megabyte of chrome around the two sentences under test.

What is reproduced is the part that has actually broken the matcher:

| Fixture | House style it pins |
| --- | --- |
| `bmj.html` | space as a thousands separator inside a catalogue number |
| `springer.html` | comma separator plus an en-dash where the number has a hyphen |
| `wiley.html` | identifier split across `<span>`s, footnote `<sup>` mid-sentence |
| `frontiers.html` | identifier partly wrapped in an `<a>` |
| `mdpi.html` | reagent table, target and catalogue number in separate cells |
| `nature.html` | recommended-article rail inside `<body>`, above the real methods |
| `sciencedirect.html` | Key Resources Table — one reagent per row, each its own block |
| `dovepress.html` | one long semicolon-separated reagent list under a single lead-in |
| `plos.html` | application named in a heading, reagents listed underneath |
| `spandidos.html` | `&nbsp;` between digit groups |
| `oncotarget.html` | zero-width space and soft hyphen inside identifiers |
| `sage.html` | narrow no-break space, and an IHC section (no verdict may carry over) |

The catalogue numbers are real entries from the bundled `data/index.json`
seed, so the expected verdicts in `test/pages.test.mjs` are the dataset's own.

These run through the **real `content.js`** in jsdom — the same TreeWalker,
skip-tag set, inline-tag joining and multi-node decoration the extension uses
on a live page — so what is asserted is the `<mark>` elements a reader would
actually see, not `findMentions` output on a string. That is the test that
would have caught both v0.1.6 parser bugs.
