# OGA browser extension

Marks every antibody cited on a page with what independent, knockout-controlled
testing found — **for the application it was actually used in** — and shows the
underlying data on hover.

<!-- Load the unpacked extension and open test/fixtures/paper.html to see it. -->

## Why per-application matters

This is the design constraint everything else follows from. Of the 18 published
TARDBP antibodies, only 5 are recommended in every application tested and only 1
fails in all of them. **The other 12 pass in one application and fail in
another.** `ab109535` is recommended for Western blot and not recommended for
IP or IF.

A single verdict per antibody would therefore be wrong about two thirds of the
time — so the card shows **every assessed application with its own result**, and
never collapses them.

**What it does not do is guess which one the page meant.** It used to: the
application was read out of the surrounding prose and the verdict narrowed to it.
Re-measuring that against 100 papers on 2 August 2026 ended it. Of 76 papers that
used western blot the extension named it for 39; of 25 that used
immunohistochemistry it named it for **none**; of 15 immunofluorescence namings
only 3 were right. Seventeen of the 54 papers it spoke about were told an
application the paper had not used. Prose-proximity inference is a western-blot
detector, and a verdict filtered through it is wrong about a third of the time it
speaks at all.

So the detection was demoted rather than deleted. It still marks the row it names
and the card says, in words, what the page was seen to say — *"This page mentions
western blot. Our results for every application are shown; the ones mentioned are
marked."*, or *"This page doesn't say which application this antibody was used
for — all our results are shown below."* Nothing is asserted, so nothing can be
asserted wrongly.

| Colour | Meaning |
| --- | --- |
| green | Recommended in every assessed application that has a result |
| red | Tested and not recommended in every assessed application that has a result |
| split underline | Passes some assessed applications and fails others — 43% of the dataset. The card says which |
| amber | This antibody is untested, but its target has been characterised, so alternatives with supportive characterisation data exist |
| grey | Nothing we hold applies: the target is not characterised, or the page names an application we have no result for |

The colour is the **worst held result**, so a reagent with a failure anywhere is
never painted green. That costs a little scannability on the papers where the old
inference happened to be right, and the correct row is still on the card — it
buys the seventeen wrong assertions going to zero, by construction.

Two rules are load-bearing and must survive any change:

1. **A verdict never transfers between applications.** A paper using an antibody
   for IHC gets grey, not the Western blot verdict — IHC is not one of the four
   applications assessed under the consensus protocols.
2. **Untested is not a verdict on quality.** Grey is deliberately muted and its
   card says so in words. It must never read as a warning.

## Architecture

Local-first. The whole index ships with the extension and is refreshed daily;
**all matching happens in the browser and no page text is ever transmitted.**
That is a hard requirement, not a nicety — people read unpublished manuscripts
and papers under review in these tabs.

```
manifest.json          MV3
src/paper.js           WHICH PAPER is this — title/DOI/PubMed id, and the lookup
src/matcher.js         identifier extraction + verdict resolution (no I/O, no model)
src/content.js         DOM walk, decoration, mutation handling
src/card.js            hover card, in a shadow root
src/background.js      index + citation storage, daily refresh, paper lookup
src/popup.js           per-page summary
data/index.json        the bundled snapshot
```

`src/paper.js` answers a different question from the rest: not *which antibody is
this* but *which PAPER is this*. If the citation record covers the paper on
screen, the extension **looks up** what it was used for instead of inferring it
from where the words sit — see "Guessing versus looking up" below.

The citation table is held in the **service worker** and never crossed into the
page; what goes over that boundary is three resolved keys out and at most one
paper's record back. Nothing about what is being read leaves the browser either
way — the lookup is local, like everything else here.

Nothing here calls a language model. Matching is regexes plus hash lookups, so
scanning a page is free and instant, and there is no per-user inference cost.

### Matching

- **RRIDs** (`RRID:AB_10859634`) are unambiguous and win any overlap.
- **Catalogue numbers** are found by tokenising generously and testing each
  token against the index. Weak identifiers (bare numbers, short codes) also
  need antibody vocabulary nearby, so `89718` in "89718 participants" is
  ignored while `Cat# 89718` is not.
- **Targets named without a catalogue number** ("anti-TDP-43 antibody") can only
  ever be amber or grey. A target phrase immediately followed by its own
  catalogue number is suppressed — that is one reagent named twice, not two.
- **Amber is the only inferred verdict, and says so.** Every other colour comes
  from a hash lookup; amber reads a target name out of the surrounding text.
  `hit.targetVia` records how that happened — `record` (the dataset row said
  so), `stated` (the page put the name against this identifier) or `nearby`
  (the closest resolvable name in the block). The card and the aria label quote
  the literal phrase back, so a reader can confirm it at a glance.
  **Keep that copy factual.** A mark-level audit over 123 marks found 0 of 45
  amber marks with a wrong gene, and 43 of 45 taking the target from an
  explicit `anti-X` phrase inside the marked span. Warning language on a signal
  that reliable teaches readers to discount it; a test asserts the caveats stay
  out.
- **A verdict must never be green while the record holds a failure.** 43.1% of
  the production index passes some assessed applications and fails others, and
  the audit found the no-application branch of `resolveStatus` is taken on 78%
  of marks — so the ordering of the `mixed` check before the `green` check in
  that branch is load-bearing, not tidiness. A test sweeps every record in the
  bundled index for it.
- **A reference marker is held off the word before it.** Inline tags are joined
  with no separator so a split identifier stays one string, which also glued
  publishers' footnote markers to whatever preceded them: `Western
  blotting<sup>1</sup>` became `blotting1` and lost the application, and
  `ab109535<sup>1</sup>` became `ab1095351` and lost the antibody outright.
  `content.js::isFootnoteMarker` separates `<sup>` and in-page `<a href="#…">`
  whose text is digits. `<sub>` is deliberately excluded — subscript digits are
  chemical and protein numbering, so separating `IP<sub>3</sub>R` would break a
  name rather than repair one — and a marker has to *start* with a digit, which
  keeps phospho-sites like `AKT<sup>S473</sup>` joined.
- **A mark covers what resolved, not what the regex matched.**
  `resolveGenePhrase` reports how many words it consumed and the target pass
  shrinks the span to them, keeping whatever the pattern matched after the
  capture. Two tests pin the invariant that no mark contains a full stop
  followed by a space.
- **Application inference** takes every cue in the enclosing sentence, and
  otherwise the single nearest cue, searching back across block boundaries to
  reach a section heading but never forward past the end of the current block.

Catalogue numbers that map to more than one RRID are dropped at build time
rather than guessed at.

## Where the data comes from

`core/extension_index.py` builds the snapshot straight from the live pipeline
models (PostgreSQL) — the same source as the public antibody pages. It is used
in two places:

- **`/extension/index.json`** builds it on demand and caches it for an hour, so
  the served data tracks the database with no build step or redeploy.
- **`python manage.py build_extension_index`** writes it to
  `data/index.json`, the copy that ships *inside* the extension zip and is used
  by a fresh install before its first refresh.

**`/extension/citations.json` is a second, separate snapshot** — which published
papers used which of our antibodies, and for what, from CiteAb's citation record
joined to our own testing. It is built by `manage.py build_citation_index` from a
database that never enters this repo, read by `core/citations.py`, and refreshed
by the **`citation-snapshot` skill**.

It is served apart from `index.json` and **not bundled**, for three reasons: it is
larger than the index, every installed extension downloads `index.json` daily
including versions that know nothing about citations, and `background.js` checks
`schema === 1` in two places so that file cannot be version-bumped without
stranding the field on its bundled fixture. A fresh install with no copy behaves
exactly as the extension did before the feature existed.

The public `/api/v1/` endpoints are deliberately **not** used: they need an
`X-API-Key` and are rate-limited to one request per hour per consumer, which
suits partners and not an extension.

### Guessing versus looking up

Application detection from the page is proximity inference, and it is measured
bad away from western blot — WB 87% correct, **IF 20%, IP 0 of 3**, IHC and FC
never named at all on papers that used them. That is why `RELIABLE_CUES` is
`["WB"]` and why a detected application has never been allowed to narrow a
verdict.

On a paper the citation record covers, that stops being a guess. CiteAb recorded
what the paper used each reagent for, independently, so the extension looks it up
— and a **looked-up** application may narrow a verdict where a **guessed** one may
not. The card says which of the two it is showing; a reader who cannot tell them
apart cannot weigh either.

A paper is recognised by its DOI, its PubMed id, or a hash of its title, in that
order, because no single one of those is on every publisher's page. A title match
is confirmed against the publication year before it is believed; a DOI or PubMed
id is not second-guessed.

**Everything here fails safe.** An unrecognised paper behaves exactly as it did
before — nothing is lost, only the improvement. The one outcome with no symptom on
screen is a *wrongly* recognised paper, which would attach another paper's
applications to this one, so every rule in `src/paper.js` is chosen in that
direction: a title two papers share is dropped as a key rather than pointed at one
of them.

Tissue is not cells. `IHC`, `IHC-P`, `IHC-Fr` and `IHC-IF` carry no OGA verdict —
the last despite its name, because the axis is how the antigen is presented and
not what the detection label says. The card names the application and reports what
OGA *did* find in the applications it tests, rather than colouring a tissue use
with a cultured-cell result.

### The one subtlety worth knowing

`Antibody.wb_recommended` and friends are plain booleans, so `False` on its own
is ambiguous — it means *tested and did not meet the bar* **or** *never tested*.
Those must not look the same to a reader: one is red, the other is grey.

The disambiguator is `PublicationImage`. A published image for an application is
the record that the application was assessed; no image means untested. This is
the same rule the public pages and the MCP server use, and it reproduces the
published verdicts exactly — `ab109535` comes out WB recommended, IP and IF not
recommended, FC not tested.

If that ever stops holding — say recommendations start being set before images
are published — the index will silently start calling untested antibodies "not
recommended". Adding an explicit per-application `tested` flag to the model
would remove the inference entirely, and is worth doing if the pipeline grows
that far.

The checked-in `data/index.json` is a **seed**, not the full dataset: all 159
genes, but antibody records for TARDBP only, which is enough to exercise every
colour state. Rebuild it against production before publishing.

Size is not a concern: 18 antibodies plus 159 genes is 14 kB, so the full
dataset lands around 1.5 MB raw and a few hundred kB gzipped.

`data/aliases.json` maps the names papers actually use to official gene symbols
(`TDP-43` → `TARDBP`, `p62` → `SQSTM1`). It is hand-maintained and the weakest
part of the pipeline — generating it from HGNC and UniProt synonyms would
meaningfully improve amber recall.

## Trying it yourself

No store account needed — both browsers load an unpacked extension directly.

```bash
python tools/dev_extension.py
```

That stages a dev copy that also matches localhost, serves the test paper, and
prints what to click. The shipped manifest is left alone. Then:

- **Chrome / Edge / Brave / Arc** — `chrome://extensions` → **Developer mode** →
  **Load unpacked** → the printed `dist/extension-dev` directory
- **Firefox** — `about:debugging#/runtime/this-firefox` → **Load Temporary
  Add-on** → `dist/extension-dev/manifest.json` (unloads when Firefox closes)

The test paper exercises every state: an antibody that is green in the Western
blot section and red in the immunofluorescence section, one that fails
everywhere, one used for IHC (grey — not an assessed application), an untested
reagent against a characterised target (amber), and a paragraph of ordinary
prose containing a real catalogue number that must *not* light up.

To try it on real papers you need the real dataset, which the bundled seed is
not:

```bash
python tools/dev_extension.py --index https://onlygoodantibodies.co.uk/extension/index.json
```

Then open any bioRxiv or PMC paper. Reload the extension **and** the page after
any code change — content-script edits need both.

## Automated tests

```bash
cd browser-extension
npm install
npm test                                   # unit + page fixtures, ~3s, no browser
CHROMIUM_PATH=/path/to/chromium npm test   # …and the real extension in Chromium
```

Two tiers, and the split is deliberate.

**Fast tier** (`matcher.test.mjs`, `pages.test.mjs`) needs only Node and jsdom,
so it runs on every commit and gates CI on every push.

- `matcher.test.mjs` — 78 checks over `matcher.js` against the bundled index:
  verdict resolution per application, the guards that keep concentrations and
  years from being read as identifiers, every publisher separator and dash
  variant, and how amber attributes its target.
- `pages.test.mjs` — 55 checks over 12 publisher house styles in
  `test/fixtures/publishers/`, run through the **real `content.js`** in jsdom
  and asserted on the `<mark>` elements it paints. This is the tier that would
  have caught both v0.1.6 parser bugs: they were typesetting, invisible to any
  test written against strings someone typed by hand. See that directory's
  README for what each fixture reproduces and why they are reconstructions of
  house style rather than saved pages.

**Browser tier** (`e2e`, `focus`, `modal`, `settings`, `vendor`) loads the real
unpacked extension into Chromium. Opt in with `--browser` or by setting
`CHROMIUM_PATH`; `run.mjs` reaches for `xvfb-run` itself when `DISPLAY` is
unset, because Chrome will not load an unpacked extension headlessly. These
catch the class the fast tier cannot see — the page renders and the script
never wires up.

CI is `.github/workflows/browser-extension.yml`: both tiers plus the Mozilla
linter, scoped by path so a change to the Django site does not run them.

**Chromium only.** Playwright can load extensions in Chromium and nowhere else,
so Firefox has no automated coverage at all. Firefox support is currently
*inferred* from the manifest (`background.scripts` + a gecko id) and has never
been run. `web-ext run` is the tool for that when someone has a Firefox to hand.

`package.json` is a dev harness and is excluded from the packaged zip
(`core/extension_index.py::build_zip_bytes`), along with `test/` and
`node_modules/`.

## Testing across browsers

Chrome, Edge, Brave and Arc are all Chromium: testing one covers all four.
Firefox is the only second engine, so this is two targets, not five.

Most of what needs human judgement is **not** browser-specific. Which antibodies
get found, what colour each gets, whether the application was read correctly off
the page — that is all `matcher.js`, pure string logic that behaves identically
everywhere. Do that testing on real papers in whichever browser you prefer; it
transfers.

What genuinely differs, and needs a look in each:

| Area | Why it differs |
| --- | --- |
| Background lifecycle | Chrome runs a service worker, Firefox an event page. Different termination and restart behaviour, so the daily index refresh is the thing most likely to behave differently. Highest risk. |
| Vendor-sites toggle | Optional host permissions have different UI and grant semantics; `chrome.scripting.registerContentScripts` has had behavioural differences. |
| Card rendering | Shadow DOM, `box-decoration-break` on wrapped highlights, `all: unset` on `<mark>`. |
| Staying installed | A Firefox temporary add-on unloads when the browser closes, so anything spanning a restart is awkward to test there. |

A short per-browser smoke pass covers the table: load it, open the test paper,
confirm highlights appear and are the right colours, hover one and check the
card renders with its four application chips and image, open the options page,
toggle supplier sites and confirm the permission prompt appears and is accepted.

Note the asymmetry when deciding where to spend time: Chromium has 78 unit
tests, 55 page-level checks and 32 end-to-end checks run against it, and
Firefox has none. Chrome is also where most users are and where store review is
strictest, so it should stay the primary target — but Firefox is currently the
*less* verified of the two.

The e2e test loads the extension into Chromium, opens a methods section, and
asserts on the colours a reader sees, the hover card contents, and stability
across a DOM mutation. The only thing it changes is the content-script match
list, so the fixture can be served from localhost.

## Checking it against Mozilla's linter

Run this before any submission — it is the same validator AMO runs, so it
catches manifest problems while they are still cheap to fix:

```bash
npm install web-ext
npx web-ext lint --source-dir=browser-extension --ignore-files='test/**' --self-hosted
```

Lint the *packaged* extension rather than the source tree when it matters:
`test/` and `icons/make_icons.py` are excluded from the zip, and linting the
source flags them.

Or `npm run lint:ext`, which is the same command with the ignore list already
set. Currently 0 errors and 4 warnings, all understood:

- `BACKGROUND_SERVICE_WORKER_IGNORED` — deliberate. `background.service_worker`
  is Chrome's key and Firefox reads `background.scripts`; both are declared so
  one zip serves both browsers.
- `UNSAFE_VAR_ASSIGNMENT` ×3 — `innerHTML` in `card.js` and `popup.js`. Every
  interpolated value goes through `esc()` first, which the linter cannot see.
  Worth converting to DOM construction if a listed AMO submission ever draws
  human review; not worth it for unlisted signing. (It was ×4 before 0.1.7;
  removing the controls section took one with it.)

The ignore list mirrors the packaging excludes in
`core/extension_index.py::build_zip_bytes`, which is the point of linting the
*packaged* extension rather than the source tree. Drop `icons/make_icons.py`
from it and you get a fifth warning, `FLAGGED_FILE_TYPE`, for a build script
that never ships.

## Distribution

Chrome blocks self-hosted installs for ordinary users and Firefox requires
signing, so the store listings are the only realistic route. The install page at
`/extension/` links out to them and shows each as "in review" until
`EXTENSION_CHROME_URL` / `EXTENSION_FIREFOX_URL` are set in the environment.

**The zip download lives on the pipeline hub**, in the Release row at the foot
of `/pipeline/start/`. It was removed outright on 31 Aug 2026, when the Chrome
Web Store listing went live — the link then sat in the *hero* of `/extension/`,
where a zip beside two store buttons invites a stranger to side-load — and
restored on 3 Sep 2026 for the 0.3.3 submission. **It is not taken out again
after a submission**, which the 31 Aug version of these steps called for: a
release cannot be cut without it, and a row on a members-only hub is not the
public offer that was worth removing.

**Not on `/extension/`, and that is not a preference.** It was put in that
page's footer first and was invisible to the member it was for. `/extension/`
is public, so `OGA_website/cache_headers.py` stamps an anonymous 200 with
`s-maxage` and takes `Cookie` out of `Vary`; Cloudflare keeps one copy and
serves it to everybody, so a member-conditional panel there is either never
shown or shown to strangers. A cache purge does not fix it — what repopulates
the edge is another anonymous copy. `/pipeline/` is in
`NEVER_CACHED_PREFIXES`, so the hub renders per visitor.

**`core/extension_index.py::build_zip_bytes` is the only packager**, tested by
`core/tests_extension_package.py`. `core/tests_extension_scope.py::
TheTeamCanStillReachTheBuildTests` pins the link three ways — a stranger and a
signed-in non-member are not offered it, a pipeline member is — plus the route
itself refusing an anonymous request, since every page-reading assertion would
pass on a view that had lost its decorator.

**Firefox is unaffected.** `/extension/firefox.xpi` serves the Mozilla-signed
build and is how Firefox installs; it was never the zip.

**Deploying the website does not put anything in a store**, and submission is a
separate, manual job. The zip should carry real data and only the server can
produce it, so a submission runs:

1. **Bump `manifest.json` and `package.json`, and write the CHANGELOG entry.**
   Both stores refuse a version they have already seen, and `build_zip_bytes`
   names the artefact from the manifest — so the number moves here first. A bump
   precedes a submission and never follows an approval; there is no way to
   re-upload over a published version.
2. **Deploy the site, and only then press Download the extension zip** on the
   pipeline hub — the row names the version it will hand you, so a number a
   store already has is visible before you spend the submission.

   The order is the half that bites: the packager swaps the repository's 18-record dev fixture for
   an index built from the **live database at the moment of the request**, so the
   artefact cannot come from a checkout, a CI job or a web session — and
   downloading before the deploy packages the *previous* code with nothing on the
   page to say so.
3. **Submit the same zip.** It is the store artefact as well as
   the team build — there is deliberately only one way to package this, so what
   the team tests is byte-for-byte what the stores receive. Chrome, Edge and
   Firefox all take it.

   Packaging refuses outright if the manifest would be rejected or would
   misbehave: missing icons, an over-long description, no gecko id, or a broad
   host pattern like `*://*/*`. Those are all static properties, so a clean
   manifest stays clean; the check exists because one of them (the broad
   pattern) did ship once.

`EXTENSION_CHROME_URL`, `EXTENSION_FIREFOX_URL` and `EXTENSION_PAGE_PUBLIC=True`
are set in Render when a listing is approved. The page goes public and appears on
the Tools hub, the store buttons draw, and the pipeline hub's preview row
disappears — no redeploy of code.

Until step 4 the only publicly reachable part is `/extension/index.json`, which
is the same data the gene pages and the MCP server already publish.

### Side-loading is a stopgap, not a distribution channel

Chrome has blocked installing extensions from anywhere but the Web Store since
2018, so before a listing exists the only route is developer-mode **Load
unpacked**. It works, but Chrome nags about developer-mode extensions on every
restart, and Firefox's **Load Temporary Add-on** unloads when the browser
closes. Fine for testing; not something to ask the wider community to do.

If team testing needs to run for weeks rather than days, Mozilla's **unlisted**
AMO channel signs a build you can host and install permanently without any
public listing, and Chrome's Web Store has **Unlisted** and **trusted testers**
visibility — a real listing that only people with the link can find. Either
gives durable installs before launch.

Budget for the listing copy, not just the code: each store wants a description,
screenshots, a privacy policy URL, and — because this requests host permissions
— a written justification for each permission. "Matching happens locally and no
page content is transmitted" is the whole justification, and it is true, which
makes review easier. Chrome charges a one-off $5 developer fee.

### Permissions

Content scripts list ~100 publisher, preprint and repository domains explicitly
(196 patterns — most domains are listed both bare and with a `*.` wildcard).
Nothing broad is granted at install time.

Two things sit in `optional_host_permissions`, both off by default and both
requested only when the user ticks them in settings:

- **15 supplier catalogue hosts** — verdicts at the point of purchase.
- **`*://*/*`** — the only way to cover **institutional proxies**. EZproxy and
  friends rewrite every journal hostname (`nature.com` becomes
  `nature-com.ezproxy.<institution>`), so no list of publisher domains can ever
  match how most academics actually read papers. It also picks up journals not
  on the list.

`src/options.js` reads the supplier list back out of the manifest so what we ask
for cannot drift from what we declare — and filters the all-sites pattern out of
it, so ticking "supplier sites" can never request everything.

This matters twice over. A broad "read your data on all websites" prompt at
install time costs installs, and broad host permissions are the main thing that
pushes a Chrome Web Store submission from the fast path onto the in-depth review
path — typically 1–2 weeks, sometimes 3–4. Keep it narrow.

Do not add comment keys to `manifest.json` to explain any of this; Chrome warns
on unrecognised keys. Explain it here instead.

### Unlisted is not a shortcut on Chrome

Chrome Web Store **Unlisted** and **trusted testers** visibility change only who
can *find* the listing. They go through the same review and the same policy
requirements as a public listing — privacy disclosures, screenshots, and a
written justification per permission. Useful for a quiet launch; useless as a
way to skip review.

Firefox is the opposite: an **unlisted** (self-distributed) add-on is signed
automatically, normally in seconds, with manual review only if the automated
check fails and then usually under two days. So if the team needs durable
installs before launch, Firefox is the realistic route and Chrome is not —
on Chrome, side-loading unpacked remains the only pre-review option.

## Benchmarks

0.1.6 was measured against 100 (paper × antibody) pairs with a manually curated
reference standard, in live Chrome across 34 publisher domains. **That build is
kept, because the findings are claims about it** — the zip, the brief it produced
and the triage of what was adopted are in
[`benchmarks/2026-08-01-extension-mcp/`](../benchmarks/2026-08-01-extension-mcp/).
Everything it found is answered, in 0.1.7 and then 0.1.8. **A re-run of the same
100 papers on 2 August measured the application inference for the first time**,
and that produced 0.2.0 — the release that stopped the verdict depending on it.
Both briefs, and the triage of what was adopted and what was declined, are in that
directory; `CHANGELOG.md` has the fixes and the evidence for each.

Do not re-run that corpus to check a change — it is a known quantity now, so a
fix can be tuned to it without being any good. Its cases live on as fixtures in
`npm test` instead. The re-run did use it, and one of its findings is a
cautionary tale about that: it loaded the **0.1.6 zip kept in this directory**
and reported an identifier fix as missing that had shipped in 0.1.7.

## Known limits

- **PDFs are not annotated.** Most people read papers as PDFs, so this is the
  largest gap between the demo and everyday usefulness. Handling it means
  shipping a pdf.js-based viewer.
- **Application inference is a hint, not a verdict.** It reads, in order, the
  enclosing sentence; what the block inherits from the layout (a table cell's
  column header and row, a figure caption); then the nearest cue in a window
  around it. Nothing rests on it any more: it marks a row and names itself on the
  card, and the verdict is the whole record either way.

  It is **reliable for western blot and for nothing else** — 39 of 45 WB namings
  were right, against 3 of 15 for IF and 0 of 3 for IP, and IHC was never named at
  all across 25 papers that used it. So a non-WB reading carries a caution and a
  route to the connector, and a WB reading does not. `matcher.js::RELIABLE_CUES`
  is that list; widen it only against a measurement.
- **It does not tell you whether the paper controlled its antibodies, and it
  cannot reliably tell you which application the paper used.** Both were measured
  and both were cut back rather than guessed at — see `CHANGELOG.md`. They are
  the same limit: each needs the whole paper, not the characters near a catalogue
  number. Both now hand off to the OGA connector, from the card and the popup, in
  one shared sentence (`src/handoff.js`).
- **Abstract-only landing pages and supplementary material are out of reach.**
  Seven of the nine non-detections in the v0.1.6 benchmark were one of these:
  the reagent list is simply not in the DOM. Nothing the matcher can do.
- Abcam's knockout-controlled characterisation data is not merged into YCharOS
  verdicts: every record carries `src` and `ind` (independent) so the card can
  attribute each result to whoever generated it. Keep them separate.
