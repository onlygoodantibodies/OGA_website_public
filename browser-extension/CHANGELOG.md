# Changelog

Starts at 0.1.7. Earlier releases predate this file; 0.1.5 and 0.1.6 exist as
signed builds in `signed/` and the repository history is the record for them.

## 0.2.4

Two defects a Europe PMC field test found on 18 August 2026, across 27 records —
241 highlights, one false positive. Both are source changes, so this one needs a
build before anybody sees it.

**A reference list is no longer scanned.** Reference 54 of a PLoS Pathogens paper
ends `Virology. 2012;429(2):136–47`, and `136–47` normalises to `13647` — a real
Cell Signaling antibody with a real green verdict, underlined on somebody else's
page numbers. That is the harmful direction of wrong: an annotation stating
something confidently where the paper says nothing, with nothing on the page to
contradict it. The bibliography is now excluded from the walk, by the classes and
ids the publishers we ship for actually use *and* by a `References` heading with
nothing on it at all, which is most of the long tail. The heading rule takes the
heading's following siblings rather than its parent, because `<h2>References</h2>`
is very often a direct child of the article container and excluding the parent
would silently stop marking the whole paper.

**A page that never goes quiet is now rescanned anyway.** Europe PMC injects the
article body only when *Free full text* is opened, and the rescan waited for the
page to settle first — but a publisher page does not settle: citation counts,
Altmetric badges and lazy images keep the mutation stream running, so a timer that
restarted on every batch never fired. The body landed and was never scanned. Zero
highlights on a paper naming nine antibodies, and nothing on screen saying a scan
had not happened — a reader who opens full text and sees nothing concludes the
tool has nothing to say. The settle timer now has a ceiling: whatever else the
page is doing, the first mutation after a scan is followed by a scan within two
seconds.

Both are pinned in `test/pages.test.mjs`, driven through the real content script,
and both fail on 0.2.3.

## 0.2.3

Cut so that `/extension/download/` produces `oga-extension-0.2.3.zip` — the
artefact that goes to the AMO Developer Hub to be signed. AMO will only accept a
version above every one already uploaded, so the number moves here first and
`EXTENSION_XPI_VERSION` moves last, after the signed `.xpi` is committed.

Unlike 0.2.2, this one genuinely needs the build: the citation layer is new
source (`src/paper.js` and changes to four other files), and an install on 0.2.2
will not fetch `/extension/citations.json` at all. Nothing breaks for them — they
keep exactly the behaviour they have — but none of the below reaches a reader
until the signed build ships.

**The extension can stop guessing what a paper used an antibody for.** New source
files, so this one does need a build: `src/paper.js`, plus changes to
`src/matcher.js`, `src/card.js`, `src/content.js` and `src/background.js`.

Application detection here is proximity inference and it is measured bad — WB 87%
correct, **IF 20%, IP 0 of 3**, IHC and FC never named at all — which is why
`RELIABLE_CUES` has only ever been `["WB"]` and a detected application was never
allowed to narrow a verdict. That limit is real and it is not fixable by reading
the page harder.

So the page is no longer the only source. A new snapshot at
`/extension/citations.json` records, for ~29,000 published papers, which of our
antibodies each one used and what for — from CiteAb's citation record joined to
our own testing. On a paper it covers, the extension now **looks the application
up** instead of inferring it, and a looked-up application *may* narrow the verdict
where a guessed one still may not. The card says which of the two it is doing;
a reader who cannot tell them apart cannot weigh either.

A paper is recognised by its PubMed id, its DOI, or a hash of its title, in that
order — three keys because no one of them is on every publisher's page. A title
match is confirmed against the publication year before it is believed; a PubMed id
is not second-guessed. Everything about this fails *safe*: an unrecognised paper
behaves exactly as it did before, and the one outcome that has no symptom on
screen — another paper's applications attached to this one — is what every rule in
`src/paper.js` is chosen to prevent.

The snapshot is fetched separately from `index.json` and is not bundled. An
install that has never fetched it, or a deploy with no snapshot, degrades to the
previous behaviour rather than breaking.

**The four application chips are now tabs.** They stated a verdict for all four
applications but showed the validation figure for only one — whichever the card
happened to pick — so the data for the other three was in the record and on no
screen. Clicking a chip now switches the figure. On a covered paper the card opens
on the application *that paper* used. An application the paper used that OGA does
not test (IHC above all) is named in words rather than left silent.

**A gene nobody has curated no longer reads as a gene that failed.** No source
file changed and no build is needed — this is `core/extension_index.py`, which
is rebuilt from live data on the server and re-downloaded by every install, so
it reaches everybody on the next refresh. It is in this file because what a
reader sees on a publisher's page changes, and that is what this file records.

`_verdicts` treated a published figure as proof the application had been
assessed, so an antibody with a figure and no recommendation was badged
**tested and not recommended** (code `1`). But figures go up as sessions are
cropped and the recommendations are set later, in one pass on
`/pipeline/recommendations/` — so between those two steps every antibody on the
gene carried that badge, against named commercial products, on a page a
reader is deciding what to trust. It reported a failed knockout-controlled test
that nobody had run.

The disambiguator needs two signals, not one: a published figure **and** the
gene having been curated at all. Without the second, those rows are now
`0` — not tested — which is what they are. A gene that *has* been curated is
unaffected, so real negative results are untouched; that is pinned, because the
same error in reverse would quietly delete the findings this dataset exists to
publish.

`core/verdicts.py` is the single reader for the rule now. The portal API had the
gate and this file did not, and the two disagreed for as long as both existed.

## 0.2.2

Cut so that `/extension/download/` produces `oga-extension-0.2.2.zip` — the
artefact that goes to the AMO Developer Hub to be signed. AMO will only accept a
version above every one already uploaded, so the number has to move here first.

**The word "verdict" is gone from what a reader sees, and the untested cards say
less.** Owner's instruction, 7 Aug 2026: OGA does not publish verdicts, it
publishes recommendations from testing under consensus protocols, and nothing a
reader sees should use the word. The same pass cut the hedging around untested
reagents — "untested is not a verdict on quality", "absence is not a verdict on
quality" — on the grounds that untested is untested and does not need
explaining. An amber card now ends at "This exact antibody has not been tested."
Two settings labels and the popup's grey tooltip say "result" where they said
"verdict".

`recordVerdicts` in `matcher.js` and the `.verdict` CSS class are internal names
and are unchanged. **No matching, colouring or scoring moved** — this release is
words only, which is worth stating because the same commit removed the
`verdicts` key from `/api/v1/`, and the extension does not read that API. It
reads `/extension/index.json`, whose shape is untouched.

Source files changed: `src/card.js`, `src/options.html`, `src/popup.html`.
`npm test --browser` passes all seven suites, including the e2e assertion that
pinned the old sentence and now pins the claim it was carrying.

**Signed, and not yet served.** `signed/oga-extension-0.2.2.xpi`,
Mozilla-signed (`META-INF/mozilla.rsa` and `cose.sig` present, so Firefox will
install it), 197 KB. Checked rather than assumed: all fourteen source files are
byte-identical to **commit `66de7c83`**, `manifest.json` matches key for key
(the byte difference is whitespace from re-serialising), and the bundled index
is the live one — 1,611 antibodies, not the 18-record dev fixture in
`data/index.json`. Pinned to a commit rather than to "this tree", per the note
under 0.2.0: the tree moves and the artefact does not.

**The remaining step is the owner's**, and it is last on purpose:
`EXTENSION_XPI_VERSION=0.2.2` in Render, then deploy. Until that is set,
Firefox is still served 0.2.1 and `core.W002` says so at deploy — a newer build
sitting in `signed/` unserved is the invisible failure, because everything
works and the team is simply on the old release. Moving the variable *before*
the file is deployed is the other direction, and 404s the download while the
page still calls that version current; `core.W001` covers that one.

## 0.2.1

Cut so that `/extension/download/` produces `oga-extension-0.2.1.zip` — the
artefact that goes to the AMO Developer Hub to be signed. AMO will only accept a
version above every one already uploaded, so the number has to move here first.

**Signed, and not yet served.** `signed/oga-extension-0.2.1.xpi`, Mozilla-signed
(`META-INF/` present, so Firefox will install it), all ten source files matching
**commit `8d6a8d5`**, and the live index bundled — built 5 August 2026, 1,611
antibodies across 159 genes. Pinned to a commit rather than to "this tree", per
the note under 0.2.0: the tree moves and the artefact does not.

**The remaining step is the owner's**, and it is the last one on purpose:
`EXTENSION_XPI_VERSION=0.2.1` in Render, then deploy. Until that is set, Firefox
is still served **0.2.0** — everything works and everyone is simply on the old
release, which is the invisible half, so `manage.py check` raises **`core.W002`**
for a newer build sitting in `signed/` unserved. The order is this way round
because setting the variable first makes `extension_firefox_xpi` 404, the install
page silently drop its one-click Firefox button, and its own copy still read
*"version 0.2.1 is current"* — that is **`core.W001`**, and it is what run 12 hit
and could not explain.

### Added — a catalogue number whose own punctuation moved still matches

`matcher.js::collapseIdentifier`. `normaliseIdentifier` handles typesetting a
publisher *adds* — BMJ's `14 060-1-AP`, Springer's `14,060–1-AP` — and cannot
handle one that *moves* the number's own punctuation, which happens too:
Thermo's `MA5-11154` is printed `MA511154`, Proteintech's `11820-1-AP` as
`11820-1AP`. A hyphen the paper dropped cannot be put back, so both sides
collapse to alphanumerics and are compared there, **after** the exact lookup has
failed — so a widening can only ever add a match. Anything under
`MIN_COLLAPSED` characters is refused, because catalogue numbers are not unique
across suppliers. The collapsed map is derived from the shipped index at load
rather than added to it, so the built zip is unchanged in format.

Mirrored in `mcp_servers/common/manuscript.py::collapse_identifier`, and that is
the point of it: the two tools must agree about one printed string, or the same
reagent resolves in the extension and comes back **absent** from the server —
which is the more harmful direction, since the server then states `oga_tested:
"no"` and a documented failure is handed over as unknown.

*This shipped in the repo under an MCP-titled commit (`3991891`), because the
rule is that the two normalisers are widened together. It never reached this
file, so it would have been released as an undocumented behaviour change.*

### Changed — amber says what is available, not what is absent

The headline read
*"Not in the dataset — read as an antibody against TARDBP"*, and the body led
with the same. But the absence is amber's *precondition*, not its message —
the mark exists because knockout-controlled antibodies for that target do.
Leading with the absence made a card whose whole value is the alternative read
as a verdict on a reagent, and on OGA's own gene pages, where *"10 APP
antibodies"* is descriptive prose and no product at all, as an accusation
about an antibody that does not exist:

> **Characterised antibodies available for TARDBP**
> TARDBP has knockout-controlled antibodies you can use instead. Target taken
> from "TDP-43" next to this catalogue number. This exact antibody has not
> been tested, and absence is not a verdict on quality.

Wording only, in the card and the screen-reader label. Nothing changed about
when amber fires or what it is derived from, and both halves 0.2.0 added — the
quoted provenance and the dataset caveat — are still there, in the body.

## 0.2.0

Driven by a re-run of the same 100 papers on 2 August 2026, which measured the
application inference directly for the first time.

**Live.** `signed/oga-extension-0.2.0.xpi`, Mozilla-signed, all ten source files
matching **commit `fcab41a2`**, live 1,611-record index bundled;
`EXTENSION_XPI_VERSION=0.2.0` set and both Render services deployed. Firefox and
Chrome carry the same release as the source.

`signed/oga-extension-0.1.9.xpi` is also kept, and is a real release rather than a
discarded number: it was cut mid-way, from `0662da5`, and carries everything under
"Changed" and "the hand-off" but **not** the caution on non-western-blot readings,
which landed afterwards in `39b62c6`. Anyone still on it sees every application
with none asserted and is not told that an immunofluorescence reading is
unreliable — which is what setting the variable to 0.2.0 fixes.

Both are pinned to a commit rather than described as "byte-identical to this
tree". That phrasing was used once here and was false within the hour, because the
tree moves and the artefact does not.

### Changed — the verdict no longer depends on guessing the application

**Every assessed application is shown, always, and none is asserted.** The
extension used to read the application out of the surrounding prose and narrow
the verdict to it. The re-run ended that:

| of the papers that used it | the extension named it |
|---|---|
| western blot, 76 | 39 |
| immunohistochemistry, 25 | **0** |
| immunofluorescence, 16 | 3 |
| flow cytometry, 3 | 0 |

Precision runs the other way — of 15 immunofluorescence namings 3 were right, and
immunoprecipitation was named three times on papers that never used it. Overall
**17 of the 54 papers it spoke about were told an application the paper had not
used.** Prose-proximity inference is a western-blot detector; widening the window
had already been tried, for a like-for-like gain of four papers.

So the card lists all four applications with their own results, and states the
page's status in plain words — *"This page mentions western blot. Our results for
every application are shown; the ones mentioned are marked."*, or *"This page
doesn't say which application this antibody was used for — all our results are
shown below."* The detection survives as a **hint that marks its row**, never a
filter that hides the others and never the basis for the colour.

The colour is now the **worst held result** across assessed applications, so a
reagent that fails anywhere is never green and `mixed` covers the 43% of the
dataset whose applications disagree. The cost is the papers where the old
inference happened to be right and the colour was more specific; the correct row
is still on the card, and the seventeen wrong assertions go to zero by
construction.

Two refusals survive, and only two: a page naming an application **nobody has
assessed** (IHC) or one **this antibody has no result for** still greys out,
because nothing we hold speaks to what that page did. Neither is an assertion —
grey claims nothing — and neither hides anything now that the card lists
everything. Both rest on cues the corpus never once saw misfire; the measured
harm was IF and IP, which no longer touch the colour at all.

The screen-reader label carries the same two claims, kept apart: the record, then
what the page was seen to say, with application names spelled out rather than
read as "WB".

### Added — a caution on the namings that earn one, and only those

A reading of anything other than western blot now says so, and carries the way
out in the same sentence: *"This page mentions Immunofluorescence, but that
reading is unreliable — check what the paper actually used. **More accurate
matching via the AI connector**."* A reader told to go and check should not have
to find the route two elements further down, past the results.

Which means the hand-off under the chips would have been a second link to one
page, three lines apart — so when a caution fires it drops to the half that has
not been said: *"Whether a paper controlled its antibodies is also read from the
whole paper by the OGA connector."* One route per card, both facts, no repetition.

The split is measured, per application, on the same re-run:

| | namings correct |
|---|---|
| WB | 39 of 45 (87%) |
| IF | 3 of 15 |
| IP | 0 of 3 — named on papers that never used it |
| IHC | never named, on 25 papers that used it |
| FC | never named, on 3 |

So western blot gets no hedge. Hedging the one reliable signal alongside the
unreliable ones is how a caution stops being read — the same reasoning that kept
cautionary language *off* the amber card in 0.1.7, where the audit found 45 of 45
attributions right. `matcher.js::RELIABLE_CUES` is the one list, and the caution
rides in the screen-reader label too: a reader who never opens the card is exactly
the one who would act on a bad reading.

It is a clause, never a sentence of its own. *"…Immunofluorescence. But that
reading is unreliable"* lets a reader meet the naming, believe it, and only then
be told — so it hangs off the naming instead.

### Added — the hand-off says what the extension cannot do

The card now carries one line under the chips: *"Which application a paper used,
and whether it controlled its antibodies, are read from the whole paper by the
OGA connector."*, with a link to `/tools/ai-tutor/`.

Those are the extension's two measured limits, and they are the same limit — both
need the whole paper, and both were cut back rather than guessed at: controls
removed outright in 0.1.7 (specificity 0.398, κ 0.114; a selectivity control
claimed on 64% of papers where the true rate was 13%), the application demoted to
a hint here. The same pipeline through a model reached κ 0.508 on controls. So
the honest thing is to name the destination at the point the reader meets the
gap, rather than leave a marked chip looking more certain than it is.

The claim and the URL live in `src/handoff.js` and nowhere else. The popup's
hand-off — which existed since 0.1.7 but covered controls only and had no link —
reads from the same source and now covers both questions. Pinned by tests that
open the real card in jsdom and refuse a second hard-coded copy of the URL in
either surface, plus one asserting the hand-off never makes a claim about the
page it is shown on.

The popup's tally tooltips were corrected with it: three of the five still
described the per-application colouring this release removes.

### Kept

`inferApplications()` is unchanged and staying — its western-blot output is the
useful part and it is what marks the row. No further effort on its recall.

## 0.1.8

Closes the last item the 0.1.6 benchmark raised: reading the application out of
a reagent **table**. The benchmarked build is kept in
[`benchmarks/2026-08-01-extension-mcp/`](../benchmarks/2026-08-01-extension-mcp/)
with the brief and the triage.

**Signed for Firefox** — `signed/oga-extension-0.1.8.xpi`, all nine source files
byte-identical to this tree, bundling the live 1,611-record index. It is served
once `EXTENSION_XPI_VERSION=0.1.8` is set in Render; until then Firefox is still
offered 0.1.7. The Chrome zip from `/extension/download/` is built from the
manifest and follows on deploy.

That signed artefact is also what closed the last testing gap. Everything below
was developed against the 18-antibody dev fixture in `data/index.json`; the
signed build was then driven in Chromium over the **real index**, and the two
publisher pages that scored zero marks in the benchmark — BMJ's `14 060-1-AP` and
Springer's `14,060–1-AP` — both resolve and report Parkin's real verdicts.

### Fixed

- **A table row is read for its own application, not its neighbour's.** The
  application was inferred from the characters around a mention: the enclosing
  sentence, then a window reaching back and to the end of the block. Both are
  defeated by a reagent table, which is where methods sections put antibodies —
  the application is a *column*, so for the antibody in row 12 the cue sits in
  the header row behind eleven other reagents.

  It did not merely miss it. On a two-row table, `ab109535` came back with **no
  application** (its own row's "Western blot" is a later block, and the window
  only looks back) while `MAB7778` picked up "Western blot" **from the row
  above** and was painted red — an antibody recommended for the application it
  was actually used in, shown as a failure. One reagent lost its verdict; the
  next was given its neighbour's.

  A cell is now given what a reader takes from the layout: its column's header,
  the rest of its own row, and the table's caption. `<figcaption>` too, so a
  legend that wraps several paragraphs is one legend rather than several
  unrelated blocks. It is consulted **after** the enclosing sentence and
  **before** the proximity window, which is the whole ordering: a column header
  is the page *stating* the application, where the window is a guess from
  distance — and in a table that guess reaches the wrong reagent.

  Structure is computed in `content.js`, which is the only place that has the
  DOM, and handed to `matcher.js` as plain `{start, end, context}` ranges. The
  matcher stays DOM-free and the parameter is optional, so nothing changes for a
  page with no tables.

- **A sentence no longer begins in a previous block.** `inferApplications`
  searched backwards for the last full stop *anywhere in the document*.
  `inferTarget` has always scoped itself to its own block; this did not, and it
  went unnoticed while pages were prose, because the fallback window reaches the
  same text legitimately. A table has no full stops in it at all — so "the
  enclosing sentence" became everything from the top of the document, and the
  first reagent's application was handed to every row beneath it. That is what
  made the table case fail closed rather than merely fail.

- **A table row's cells no longer fuse.** `textContent` concatenates them, so a
  row rendered as `…ab109535Western blot…` and the Western blot cue's leading
  `\b` had a digit on its left. Exactly the footnote-marker bug of 0.1.7, one
  level up: the application would have been lost in the very layout this change
  exists to read.

### Unchanged

No new permissions, no new network calls, no change to the index or the privacy
posture. Five page-level checks and three matcher tests added; both new
behaviours were confirmed to fail with the fix reverted.

## 0.1.7

Driven by a benchmark of 0.1.6 against 100 (paper × antibody) pairs with
expert-arbitrated ground truth, run in live Chrome across 34 publisher domains.
95 of 100 papers scanned successfully; five were unreachable behind Cloudflare.

### Removed — page-level control detection

**The popup no longer reports whether a paper shows antibody controls.** This
is a deliberate feature removal, not a regression.

Measured against per-antibody ground truth, the scanner reached sensitivity
0.917 but specificity 0.398, PPV 0.18 and Cohen's κ 0.114 — barely above
chance. It showed "Selectivity control: knockout, knockdown" on **64% of
papers when the true rate was 13%**. Of the 1,017 cues it fired, only 26% sat
within 250 characters of the target antibody's own gene; the rest were
knockouts of *other* genes in the same pathway (72 of the test papers concern
PRKN, and mitophagy papers routinely knock down PINK1, CaMKK2, ATAD3A),
publisher recommended-article tiles, and reference-list titles.

The defect was structural rather than a matter of tuning: the scan had no
notion of *which antibody* a control belonged to, and the popup rendered its
page-level answer directly beneath a list of named antibodies, where a reader
will inevitably bind the two. The best deterministic rule tested — cue within
±250 characters of the target's gene, outside the references — reached only
κ 0.236.

**The analysis moved to the OGA MCP server**, where a model reads the paper and
can link a cue to the specific antibody; on the same 95 papers that pipeline
reached κ 0.508. In the space freed, the popup carries a hand-off that makes no
claim about the page: a line pointing at the MCP server and a button that
copies the paper's DOI to the clipboard.

### Fixed

- **An antibody that fails some applications is never painted green.**
  `resolveStatus()` tested whether the record passed anything before it looked
  at what it failed, so on the branch that runs when a page does not say which
  application a reagent was used for, an antibody that passes some assessed
  applications and fails others came back **green** — with the failures present
  only in a field the green card does not lead with. The `mixed` level was used
  correctly one branch above and was unreachable here.

  Two measurements make that the most consequential defect found so far:
  **43.1%** of the 1,611 antibodies in the shipped index pass some assessed
  applications and fail others, and a mark-level audit found this
  no-application branch is taken on **78% of marks**, because the extension
  usually cannot tell from the page what a reagent was used for. It hid in the
  first benchmark because 17 of that corpus's 18 antibodies fail every assessed
  application, so the fail-only path always returned red.

  A verdict that spans all four assessed applications now says so in the
  headline — *"Across the applications assessed: recommended for WB, not
  recommended for IP, IF"* — the way the red branch has always said "in any
  application assessed".

- **Catalogue numbers are read through publisher typesetting.** BMJ sets
  Proteintech's `14060-1-AP` as `14 060-1-AP`, using a space as a thousands
  separator; Springer sets it `14,060–1-AP`, with a comma and an en-dash. Both
  were verified live in the DOM, and both pages scored **zero marks** while
  listing three characterised antibodies each. Dash variants, digit-group
  separators (comma, space, thin/hair/no-break/narrow-no-break) and zero-width
  characters are now normalised before lookup — never the full stop, which
  between digits is a decimal point.

- **Grey no longer claims there is no data when there is a failure.** On 7 of
  95 pages the target antibody was headed *"No independent data"* — with the
  screen-reader label *"Untested, not a verdict on quality"* — for antibodies
  that are in the dataset and **failed** knockout-controlled testing (`ab4193`,
  `ab15954`, `14060-1-AP`, `870502`). It happened when the page used the
  antibody for IHC, or for an assessed application with no result. The headline
  and the label now carry the real result forward: *"Not assessed for IHC — but
  not recommended for WB, IF"*. Where a record genuinely holds no verdict
  anywhere, the original wording is kept.

- **A highlight no longer runs past what it matched.** The four-word target
  capture allowed a full stop inside a word, so it ran through sentence
  boundaries, and the mark was drawn over the whole regex match however much of
  it had actually resolved. On a Cell Death & Disease figure legend the
  highlight ran 38 characters past its target, through a panel label, and ended
  on a cell line — printing *"antibodies against PINK1 and Parkin. i"* as the
  card headline. The gene was right; the span was garbage. Captures now stop at
  sentence punctuation, and the mark is shrunk to the words that resolved.

- **Amber shows its working.** Amber is the only verdict read out of the page
  rather than looked up — every other colour is a hash lookup on a catalogue
  number, RRID or clone. The card and the screen-reader label now quote the
  literal text the target was taken from, so a reader can confirm it at a
  glance:

  > **Not in the dataset — read as an antibody against TARDBP**
  > Target taken from "TDP-43" next to this catalogue number. TARDBP has
  > independently knockout-tested antibodies. This exact antibody has not been
  > tested, and absence is not a verdict on quality.

  This is transparency, not a caveat. A mark-level audit measured the path
  directly: **0 of 45** amber marks had a wrong gene, and 43 of 45 took the
  target from an explicit *anti-X* phrase inside the marked span. So the
  provenance is stated and the claim is made outright — no "check it matches",
  and no hedge in front of the alternatives. Telling a reader to distrust a
  signal that reliable only teaches them to discount it.

- **A footnote marker no longer hides an antibody.** Inline tags are joined
  with no separator, deliberately, so a catalogue number split across `<span>`s
  stays one string. Publishers hang reference markers off words with the same
  markup, and the join made one token out of two things: `Western
  blotting<sup>1</sup>` became `blotting1`, so the word boundary in the Western
  blot cue could not match and the application was lost; worse,
  `ab109535<sup>1</sup>` became `ab1095351`, which is in no index, so **nothing
  was marked at all** — the same silent miss the BMJ and Springer separators
  produced. Superscripts and in-page reference links whose text is digits are
  now held off the word before them. Bracketed markers (`[14]`) already worked;
  bare ones did not. `<sub>` is untouched, so `IP<sub>3</sub>R` stays a name,
  and a marker must start with a digit, so `AKT<sup>S473</sup>` does too.

- **An application named later in the same paragraph is no longer lost to line
  wrapping.** Newlines that a publisher's own source formatting puts inside a
  paragraph were being read as block boundaries, so a Springer methods
  paragraph naming "immunoblotting" three source lines below the reagent fell
  back to a whole-antibody verdict — painting an antibody green on a page where
  it fails Western blot. Found by the new page-level fixtures.

### Added

- **23 content-script match patterns**, covering the eleven publisher hostnames
  the benchmark corpus resolves to that were missing: `ejh.it`,
  `cellmolbiol.org`, `medscimonit.com`, `tcr.amegroups.org` (listed at
  publisher level as `*.amegroups.org`), `cytojournal.com`, `jmb.or.kr`,
  `balkanmedicaljournal.org`, `thno.org`, `journal-jop.org`, `ovid.com`,
  `jacc.org`. On a default install roughly **one paper in three** from that
  corpus was previously a silent no-op.

- **The popup says when it is not scanning a tab**, with a link to settings.
  Detected by the tab failing to answer, never by a mark count of zero — zero
  marks is the correct answer on a page that cites no characterised antibodies,
  and conflating the two reports "nothing found" about a page nobody read.

- **A regression harness.** `npm test` runs 78 matcher unit tests and 55
  page-level checks over 12 publisher house styles, the latter through the real
  content script in jsdom, asserting on the `<mark>` elements a reader sees.
  `npm test -- --browser` adds five Playwright suites that load the real
  unpacked extension into Chromium. CI runs both plus the Mozilla add-on linter.

### Changed

- The **Run on any site** toggle moves to the top of settings and explains what
  it buys — institutional proxy hostnames, society journals, the long tail of
  smaller publishers — and states that no page content ever leaves the browser,
  so the broad permission carries no data cost.
- The grey checkbox's description was corrected: it read "no data either way",
  but a reagent we hold nothing on is never marked at all, so every grey mark
  is an antibody that *is* in the dataset and lacks a result in the application
  the page used it for.

### Unchanged

No new network calls, no new permissions beyond the content-script match
additions, and no change to the bundled index, the service worker's refresh
logic, or the privacy posture. All matching still happens in the browser and no
page text is ever transmitted.
