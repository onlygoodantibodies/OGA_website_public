/**
 * Page-level regression tests. Run with:  node browser-extension/test/pages.test.mjs
 *
 * These load the **real content script** into a jsdom document and read back
 * the <mark> elements it painted — the same TreeWalker, the same skip-tag set,
 * the same inline-tag joining, the same multi-node decoration a live page gets.
 * What is asserted is what a reader would see, not what findMentions() returns
 * for a string.
 *
 * That distinction is the whole point. Both parser bugs the v0.1.6 benchmark
 * found were publisher *typesetting* — a space, a comma and an en-dash inside
 * a catalogue number — and neither was visible from unit-testing the matcher
 * against strings someone had typed by hand. See fixtures/publishers/README.md
 * for what each fixture reproduces and why it is a reconstruction rather than
 * a saved page.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..");
const fixtureDir = path.join(here, "fixtures/publishers");

const rawIndex = JSON.parse(fs.readFileSync(path.join(root, "data/index.json"), "utf8"));
// Read from the manifest rather than listed again here. A second copy of this
// list is how `src/paper.js` came to be loaded in the browser and not in the
// tests -- which fails as a TypeError at boot, so every fixture goes blank at
// once and the cause is nowhere near the symptom.
const SOURCES = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"))
  .content_scripts[0].js
  .map((rel) => fs.readFileSync(path.join(root, rel), "utf8"));

let passed = 0;
const failures = [];
function test(name, fn) {
  try {
    fn();
    passed++;
  } catch (err) {
    failures.push(`${name}\n    ${err.message}`);
  }
}

const LEVEL = {
  "oga-green": "green", "oga-red": "red", "oga-mixed": "mixed",
  "oga-amber": "amber", "oga-grey": "grey",
};

/**
 * Run the shipped content script over one fixture and return its marks.
 *
 * Each fixture gets its own jsdom and its own evaluation of the sources: the
 * content script keeps its state (the index, the settings, the hit list) in
 * closure variables, so sharing one evaluation across fixtures would let the
 * first page's hits leak into the second's.
 */
async function scan(file, settings = {}) {
  return scanHtml(fs.readFileSync(path.join(fixtureDir, file), "utf8"), settings);
}

async function scanHtml(html, settings = {}) {
  const window = boot(html, settings);
  await new Promise((r) => window.setTimeout(r, 30));
  return marksIn(window);
}

/** The marks a window is currently painted with, as scanHtml reports them. */
function marksIn(window) {
  return [...window.document.querySelectorAll("mark.oga-hl")].map((el) => ({
    text: el.textContent,
    level: LEVEL[[...el.classList].find((c) => LEVEL[c])] || "?",
    label: el.getAttribute("aria-label") || "",
    // Marks sharing a data-oga id are one mention split across text nodes.
    id: el.dataset.oga,
  }));
}

/**
 * A jsdom window with the real content script running in it, handed back
 * un-awaited so a test can mutate the page the way a publisher does and watch
 * what the extension makes of it.
 */
function boot(html, settings = {}, url = "https://example.org/article") {
  const dom = new JSDOM(html, {
    url,
    pretendToBeVisual: true,
    // So window.eval runs with the window as its global — without it the
    // content script's bare `document` resolves to Node's, which has none.
    // "outside-only" keeps the fixtures' own <script> tags inert, which is
    // what we want: they are page content, not part of the test.
    runScripts: "outside-only",
  });
  const { window } = dom;

  // The content script asks the service worker for the index once at boot and
  // does nothing at all if that call fails, so this shim is what makes it run.
  window.chrome = {
    runtime: {
      lastError: null,
      onMessage: { addListener() {} },
      sendMessage(msg, cb) {
        if (msg && msg.type === "oga:get-index" && typeof cb === "function") {
          cb({ index: rawIndex, settings: { enabled: true, ...settings } });
        }
      },
    },
  };
  // jsdom has no requestIdleCallback; without it the script waits 300 ms.
  window.requestIdleCallback = (fn) => window.setTimeout(fn, 0);

  for (const src of SOURCES) window.eval(src);
  return window;
}

/**
 * The hover card's own markup, opened the way a reader opens it — a mouseover on
 * the mark, through the real listener, into the real shadow root.
 */
async function cardFor(html, needle) {
  const dom = new JSDOM(html, { url: "https://example.org/article", pretendToBeVisual: true, runScripts: "outside-only" });
  const { window } = dom;
  window.chrome = {
    runtime: {
      lastError: null, onMessage: { addListener() {} },
      sendMessage(msg, cb) {
        if (msg && msg.type === "oga:get-index" && typeof cb === "function") {
          cb({ index: rawIndex, settings: { enabled: true } });
        }
      },
    },
  };
  window.requestIdleCallback = (fn) => window.setTimeout(fn, 0);
  for (const src of SOURCES) window.eval(src);
  await new Promise((r) => window.setTimeout(r, 30));

  const mark = [...window.document.querySelectorAll("mark.oga-hl")]
    .find((m) => bare(m.textContent).includes(bare(needle)));
  assert.ok(mark, `no mark for "${needle}"`);
  mark.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  await new Promise((r) => window.setTimeout(r, 30));
  const hostEl = window.document.querySelector(".oga-card-host");
  assert.ok(hostEl && hostEl.shadowRoot, "the card never opened");
  return hostEl.shadowRoot.querySelector(".card").innerHTML;
}

/** Mentions, not marks: an identifier split by inline markup is one antibody. */
function mentions(marks) {
  const byId = new Map();
  for (const m of marks) {
    if (!byId.has(m.id)) byId.set(m.id, { text: "", level: m.level, label: m.label });
    byId.get(m.id).text += m.text;
  }
  return [...byId.values()];
}

/**
 * Compare on the identifier, not on its typesetting: the mark's text is the
 * page's own characters, separators and zero-width junk included, which is
 * exactly right on screen and useless for looking a fixture's answer up.
 */
const bare = (s) => s.replace(/[\s    ​‌‍﻿­,]/g, "");

const found = (marks, needle) => {
  const hit = mentions(marks).find((m) => bare(m.text).includes(bare(needle)));
  assert.ok(hit, `no mark for "${needle}"; saw ${JSON.stringify(mentions(marks))}`);
  return hit;
};

/* ------------------------------------------------------------------ cases */

const CASES = [
  // Expected colours are the antibody's WHOLE record — green only when every
  // assessed application passes, red only when every one fails, `mixed` when
  // they disagree. They were per-application until the 2 August re-run measured
  // the inference: 17 of the 54 papers it spoke about were told an application
  // the paper had not used. So the colour comes from the record and the page's
  // own words go in the card's status line instead.
  //
  // Taken from data/index.json by hand, not from resolveStatus, so this table
  // still disagrees with the code when the code is wrong.
  {
    file: "bmj.html",
    // The bug: a space as a thousands separator split the token in two, so
    // neither half was in the index. This page scored zero marks live.
    name: "BMJ — a space inside the catalogue number",
    expect: [["10 782-2-AP", "green"], ["12 892-1-AP", "mixed"]],
  },
  {
    file: "springer.html",
    // The bug: a comma split the token, and an en-dash where the number has a
    // hyphen failed the key even once rejoined. Zero marks live.
    name: "Springer — comma separator and an en-dash",
    expect: [["10,782–2-AP", "green"], ["12,892–1-AP", "mixed"]],
  },
  {
    file: "wiley.html",
    // The footnote marker on "Western blotting<sup><a>1</a></sup>" no longer
    // glues to the cue, so WB is still DETECTED here — asserted below on the
    // card's own words rather than on the colour, which no longer moves with it.
    name: "Wiley — identifier split across inline elements, footnote on the cue",
    expect: [["ab109535", "mixed"], ["GTX630196", "red"]],
  },
  {
    file: "frontiers.html",
    name: "Frontiers — identifier partly inside a link",
    expect: [["MA5-27828", "green"], ["NBP1-92695", "green"]],
  },
  {
    file: "mdpi.html",
    name: "MDPI — reagent table",
    expect: [["ab133547", "mixed"], ["MAB7778", "mixed"]],
  },
  {
    file: "nature.html",
    name: "Nature — methods paragraph below a recommendations rail",
    expect: [["ab109535", "mixed"]],
  },
  {
    file: "sciencedirect.html",
    // A Key Resources Table names reagents and not what they were used for, and
    // the "western blot" sentence sits after the table — forward of the block,
    // which inferApplications deliberately never reaches. The commonest real
    // shape: the audit found 78% of marks take it.
    name: "ScienceDirect — Key Resources Table",
    expect: [["ab109535", "mixed"], ["GTX630196", "red"]],
  },
  {
    file: "dovepress.html",
    name: "Dovepress — long semicolon-separated reagent list",
    expect: [
      ["10782-2-AP", "green"], ["12892-1-AP", "mixed"], ["80001-1-RR", "mixed"],
      ["80002-1-RR", "green"], ["MA5-32627", "mixed"], ["A19123", "mixed"],
    ],
  },
  {
    file: "plos.html",
    // IF, from the heading — and both antibodies hold a pass AND a fail, so
    // both are mixed however the page is read. The heading's effect is on the
    // card's status line and on which chip is marked.
    name: "PLOS — application taken from the heading",
    expect: [["ab109535", "mixed"], ["89789", "mixed"]],
  },
  {
    file: "spandidos.html",
    name: "Spandidos — no-break space between digit groups",
    expect: [["10 782-2-AP", "green"], ["12 892-1-AP", "mixed"]],
  },
  {
    file: "oncotarget.html",
    name: "Oncotarget — zero-width space and soft hyphen inside identifiers",
    expect: [["ab109535", "mixed"], ["GTX630196", "red"]],
  },
  {
    file: "sage.html",
    // ab254166 stays GREY: the page names IHC, which is not one of the four
    // assessed, so nothing we hold speaks to what this page did. That refusal
    // survives the change — it asserts nothing, and IHC was never once named
    // wrongly in the corpus.
    name: "SAGE — narrow no-break space, and IHC inherits no verdict",
    expect: [["ab254166", "grey"], ["10 782-2-AP", "green"]],
  },
];

const scans = new Map();
for (const c of CASES) scans.set(c.file, await scan(c.file));

for (const { file, name, expect } of CASES) {
  const marks = scans.get(file);
  for (const [needle, level] of expect) {
    test(`${name}: ${needle} -> ${level}`, () => {
      assert.equal(found(marks, needle).level, level);
    });
  }
}

/* ------------------------------------------ properties that hold everywhere */

test("every publisher fixture produces at least one mark", () => {
  // The failure this catches is the one that cost two whole pages in v0.1.6:
  // not a wrong colour, but nothing at all, which is indistinguishable from
  // "this paper cites no characterised antibodies".
  for (const [file, marks] of scans) {
    assert.ok(marks.length > 0, `${file} scored zero marks`);
  }
});

test("no mark on any page spans a sentence", () => {
  // The cheap invariant from the mark-level audit: a highlight containing a
  // full stop followed by a space has run past whatever it was marking. It is
  // worth sweeping every fixture rather than the one that broke, because the
  // over-capture was a property of the pattern, not of the page.
  for (const [file, marks] of scans) {
    for (const m of mentions(marks)) {
      assert.ok(!/\.\s/.test(m.text), `${file}: mark spans a sentence: "${m.text}"`);
    }
  }
});

test("no mark is absurdly long", () => {
  // The same defect measured a different way. The Cell Death & Disease case
  // ran 40 characters; a legitimate mark is an identifier or a short target
  // phrase, and the longest here is "Antibodies against amyloid precursor
  // protein" at 44 — so this catches a runaway without pinning a house style.
  for (const [file, marks] of scans) {
    for (const m of mentions(marks)) {
      assert.ok(m.text.length <= 60, `${file}: mark is ${m.text.length} chars: "${m.text}"`);
    }
  }
});

test("a mention split across text nodes stays one antibody", () => {
  // Wiley splits ab109535 across two spans; the pieces must share one id and
  // reconstruct the whole identifier, or the page reads as two reagents.
  const marks = scans.get("wiley.html");
  const parts = marks.filter((m) => m.text.includes("ab") || m.text.includes("109535"));
  assert.equal(new Set(parts.map((m) => m.id)).size, 1, "split identifier got two ids");
  assert.equal(parts.map((m) => m.text).join(""), "ab109535");
});

test("the recommendations rail contributes no marks of its own", () => {
  // It sits inside <body> and is walked like any other text. Nothing in it is
  // a characterised antibody, so nothing in it may be marked.
  const marks = mentions(scans.get("nature.html"));
  assert.equal(marks.length, 1, `expected only the methods mention: ${JSON.stringify(marks)}`);
});

test("a target is never read out of a neighbouring table row", () => {
  // Every cell of a Key Resources Table is its own block. Without block
  // scoping, the anti-p62 row's target is adopted by the row above it.
  const marks = mentions(scans.get("sciencedirect.html"));
  assert.ok(!marks.some((m) => m.text.includes("ab56416")),
    `a reagent we hold no data on must not be marked: ${JSON.stringify(marks)}`);
});

test("grey never claims there is no data when the record holds a verdict", () => {
  // The v0.1.6 wording bug, asserted where a reader meets it: the accessible
  // label on a real mark. ab254166 is in the dataset and fails IP and IF; the
  // page used it for IHC, which nobody has assessed.
  const grey = found(scans.get("sage.html"), "ab254166");
  assert.equal(grey.level, "grey");
  assert.ok(!/no independent data/i.test(grey.label),
    `grey label still claims no data: "${grey.label}"`);
  assert.match(grey.label, /not recommended for/i);
});

test("the four assessed applications are never confused with IHC", () => {
  const grey = found(scans.get("sage.html"), "ab254166");
  assert.ok(!/recommended for IHC/i.test(grey.label),
    `IHC must never carry a verdict: "${grey.label}"`);
});

/* ------------------------------------------- over-capture, through the DOM */

// A minimal page, for behaviours that belong to the matcher rather than to any
// one publisher's house style. Keeps the fixtures in fixtures/publishers/ about
// what they say they are about.
const article = (body) => `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Methods</title></head><body><div class="body">${body}</div></body></html>`;

// The live Cell Death & Disease figure legend (10.1038/s41419-018-1112-x)
// whose mark ran 38 characters past its target, and the phrase that used to
// swallow its own trailing prose. Asserted through the real content script,
// because the mark a reader sees is what went wrong — the gene resolved fine.
const legend = await scanHtml(article(
  `<figure><figcaption>h Lysates were analyzed by immunoblotting using antibodies
   against TDP-43 and Parkin. i HepG2 were transfected with scRNA.</figcaption></figure>`));

const trailing = await scanHtml(article(
  `<h3>Western blot</h3><p>Blots used antibodies to TDP-43 raised in rabbit at 1:1000.</p>`));

test("a figure legend's mark stops at the target, not at the next sentence", () => {
  const m = mentions(legend).find((m) => m.text.includes("TDP-43"));
  assert.ok(m, `nothing marked: ${JSON.stringify(mentions(legend))}`);
  assert.ok(!m.text.includes("."), `the mark crossed the full stop: "${m.text}"`);
  assert.ok(!/HepG2/.test(m.text), `the mark ended on a cell line: "${m.text}"`);
});

test("a target phrase does not swallow the prose after it", () => {
  const m = mentions(trailing).find((m) => m.text.includes("TDP-43"));
  assert.ok(m && !/raised in rabbit/.test(m.text), `"${m && m.text}"`);
});

/* --------------------------------------------------- footnote markers glue */
//
// Inline tags are joined with no separator so an identifier split across
// <span>s stays one string. Publishers hang reference markers off words with
// the same markup, and the join made one token out of two things:
//
//   Western blotting<sup>1</sup>  ->  "Western blotting1"   application lost
//   ab109535<sup>1</sup>          ->  "ab1095351"           antibody lost
//
// The second is the severe one: the token is in no index, so nothing is marked
// at all — the same silent miss the BMJ and Springer separators produced, and
// indistinguishable from a page that cites no characterised antibodies.

const marked = (html) => scanHtml(article(html)).then(mentions);

const FOOTNOTE_CASES = [
  ["superscript on the cue", `<p>Western blotting<sup>1</sup> used ab109535 (Abcam).</p>`],
  ["nested anchor, Nature style", `<p>Western blotting<sup><a href="#f1">1</a></sup> used ab109535 (Abcam).</p>`],
  ["a bare in-page reference link", `<p>Western blotting<a href="#r14">14</a> used ab109535 (Abcam).</p>`],
  ["a bracketed reference link", `<p>Western blotting<a href="#r14">[14]</a> used ab109535 (Abcam).</p>`],
  ["a list of markers", `<p>Western blotting<sup>1,2</sup> used ab109535 (Abcam).</p>`],
  ["a marker on the catalogue number", `<p>Western blotting used ab109535<sup>1</sup> (Abcam).</p>`],
  ["a marker before the catalogue number", `<p>Western blotting used <sup>1</sup>ab109535 (Abcam).</p>`],
];

const footnotes = [];
for (const [name, html] of FOOTNOTE_CASES) footnotes.push([name, await marked(html)]);

for (const [name, marks] of footnotes) {
  test(`a reference marker does not hide the antibody — ${name}`, () => {
    const hit = marks.find((m) => bare(m.text).includes("ab109535"));
    assert.ok(hit, `nothing marked: ${JSON.stringify(marks)}`);
    // Asserted on the DETECTION, not the colour. The colour is the record's now
    // and would read `mixed` whether or not the cue survived the marker, so
    // pinning it here would pass through the very bug this test exists for.
    assert.match(hit.label, /mentions Western blot/i,
      `the application was lost to the marker: ${hit.label}`);
  });
}

// The other half: markup that looks similar and must keep behaving.

test("a subscript in a name is left joined", () => {
  // IP<sub>3</sub>R is a name, not a footnote. <sub> is deliberately excluded
  // from the rule — separating it would break a name rather than repair one.
  return marked(`<p>Western blotting used IP<sub>3</sub>R and ab109535 (Abcam).</p>`)
    .then((marks) => assert.ok(marks.some((m) => m.text.includes("ab109535"))));
});

test("a superscript that is not a marker is left joined", () => {
  // A phospho-site starts with a letter, so it is part of the name beside it.
  return marked(`<p>Western blotting used anti-AKT<sup>S473</sup> and ab109535 (Abcam).</p>`)
    .then((marks) => assert.ok(marks.some((m) => m.text.includes("ab109535"))));
});

test("a link that wraps an identifier is not a reference marker", () => {
  // An in-page href is what a reference link has and what this does not.
  return marked(`<p>Western blotting used <a href="/p">MA5-27828</a> (Thermo).</p>`)
    .then((marks) => assert.ok(marks.some((m) => m.text.includes("MA5-27828")),
      `the identifier was lost: ${JSON.stringify(marks)}`));
});

/* -------------------------------------------------- amber shows its working */
//
// Amber is the only verdict read out of the page rather than looked up, and it
// was 26% of every mark the benchmark saw. If a reagent list picks up its
// neighbour's target, the card names the wrong protein confidently — while
// pointing the reader at other suppliers' products. These assert the card
// admits the attribution, on the surface a reader actually meets.

// An unknown RRID whose target is the nearest name in the same block — the
// weak attribution, and the one the failure mode lives in.
const nearby = await scanHtml(article(
  `<h3>Western blot</h3><p>Anti-TDP-43 antibody was used (RRID:AB_9999999) at 1:1000.</p>`));

// A target the page states against a catalogue number we do not hold.
const stated = await scanHtml(article(
  `<h3>Western blot</h3><p>Antibodies against TARDBP(#A2164) were purchased from ABclonal.</p>`));

test("an inferred target is quoted back, not just asserted", () => {
  const hit = found(nearby, "AB_9999999");
  assert.equal(hit.level, "amber");
  assert.match(hit.label, /TDP-43/, "the phrase that produced the gene is not shown");
  assert.match(hit.label, /TARDBP/, "the gene it resolved to is not shown");
});

test("the proximity attribution still says which name it took", () => {
  assert.match(found(nearby, "AB_9999999").label, /closest target name in this block/i);
});

test("a stated attribution is worded as stated, not as closest", () => {
  const hit = found(stated, "A2164");
  assert.equal(hit.level, "amber");
  assert.doesNotMatch(hit.label, /closest target name/i);
  assert.match(hit.label, /TARDBP/);
});

test("amber never asserts the target without saying where it came from", () => {
  // The old label — "tested alternatives against SQSTM1 exist" — stated the
  // target as flatly as green states a verdict that came from a hash lookup.
  for (const marks of [nearby, stated, scans.get("sciencedirect.html")]) {
    for (const m of mentions(marks).filter((m) => m.level === "amber")) {
      assert.match(m.label, /taken from/i, `amber label makes a bare claim: "${m.label}"`);
    }
  }
});

test("amber shows its working without warning about it", () => {
  // A mark-level audit found 0 of 45 amber marks with a wrong gene, and 43 of
  // 45 taking the target from an explicit anti-X phrase inside the marked
  // span. Telling a reader to double-check a signal that reliable teaches them
  // to discount it, so the provenance is stated and the claim is made outright.
  for (const marks of [nearby, stated, scans.get("sciencedirect.html")]) {
    for (const m of mentions(marks).filter((m) => m.level === "amber")) {
      assert.doesNotMatch(m.label, /check it matches|not necessarily|if the target is right/i,
        `amber label warns rather than informs: "${m.label}"`);
    }
  }
});

test("the load-bearing dataset caveat survives", () => {
  // Correct, and not what this change was about. Surfacing the attribution
  // must not have quietly cost the clause that was already right.
  for (const m of mentions(nearby).filter((m) => m.level === "amber")) {
    assert.match(m.label, /absence is not a verdict on quality/i);
  }
});

test("the claim is not watered down to \"may have alternatives\"", () => {
  // Hedging costs usefulness and removes no error; visibility is what makes a
  // wrong inference self-correcting.
  for (const m of mentions(nearby).filter((m) => m.level === "amber")) {
    assert.doesNotMatch(m.label, /\bmay have\b|\bmight have\b|\bpossibly\b/i);
    assert.match(m.label, /has knockout-controlled antibodies you can use/i);
  }
});

test("amber leads with what is available, not with what is absent", () => {
  // Amber's whole reason for existing is that characterised antibodies exist for
  // the target; the absence is the precondition, not the message. Leading with
  // "not in the dataset" made a mark whose value is the alternative read as a
  // verdict on a reagent — and on OGA's own gene pages, where "10 APP
  // antibodies" is descriptive text and no product at all, as an accusation
  // about an antibody that does not exist.
  for (const m of mentions(nearby).filter((m) => m.level === "amber")) {
    const available = m.label.search(/has knockout-controlled antibodies/i);
    const absent = m.label.search(/not in the dataset/i);
    assert.ok(available > -1, m.label);
    assert.ok(absent > -1, "the dataset caveat is a different claim and stays");
    assert.ok(available < absent, `absence should not lead: ${m.label}`);
  }
});

/* ------------------------------------------ the application a table states */
//
// A reagent table is where methods sections put antibodies, and it defeats a
// character window completely: the application is a COLUMN, so for the antibody
// in row 12 the cue sits in the header row, behind eleven other reagents.
//
// The window did not merely miss it. Run over the two-row table below without
// the layout, `ab109535` came back with no application at all (its own row's
// "Western blot" is a later block) and `MAB7778` picked up "Western blot" from
// THE ROW ABOVE and was painted red. One antibody lost its verdict; the next
// was given its neighbour's — a confident wrong answer, which is the failure
// this must not trade up for coverage.
//
// ab109535: WB recommended, IP/IF not.   MAB7778: IF/IP recommended, WB not.
// So a row swap flips both to red, and both being green is only possible if
// each row was read as its own.

const reagentTable = await scanHtml(article(`
  <table>
    <tr><th>Target</th><th>Catalogue</th><th>Application</th><th>Dilution</th></tr>
    <tr><td>TDP-43</td><td>ab109535</td><td>Western blot</td><td>1:1000</td></tr>
    <tr><td>TDP-43</td><td>MAB7778</td><td>Immunofluorescence</td><td>1:200</td></tr>
  </table>`));

test("each table row is read for its own application, not its neighbour's", () => {
  // Asserted on what the page was seen to SAY. The colour is the whole record's
  // now, and both of these are `mixed` whichever row's application they took —
  // so a colour assertion here would pass through the bug it was written for.
  assert.match(found(reagentTable, "ab109535").label, /mentions Western blot/i);
  assert.match(found(reagentTable, "MAB7778").label, /mentions immunofluorescence/i);
  assert.doesNotMatch(found(reagentTable, "MAB7778").label, /Western blot\b/i,
    "row 2 took row 1's application");
});

const captionTable = await scanHtml(article(`
  <table>
    <caption>Antibodies used for immunofluorescence</caption>
    <tr><th>Target</th><th>Catalogue</th></tr>
    <tr><td>TDP-43</td><td>ab109535</td></tr>
  </table>`));

test("a table caption names the application for every row under it", () => {
  assert.match(found(captionTable, "ab109535").label, /mentions immunofluorescence/i);
});

const columnHeader = await scanHtml(article(`
  <table>
    <thead><tr><th>Target</th><th>Immunofluorescence</th></tr></thead>
    <tbody><tr><td>TDP-43</td><td>ab109535 (1:200)</td></tr></tbody>
  </table>`));

test("an application stated only in the column header reaches the cell", () => {
  assert.match(found(columnHeader, "ab109535").label, /mentions immunofluorescence/i);
});

const splitLegend = await scanHtml(article(`
  <figure>
    <figcaption><p><b>Figure 3.</b> Representative immunofluorescence of cortical
    neurons.</p><p>Cells were probed with ab109535 at 1:200.</p></figcaption>
  </figure>`));

test("a figure legend governs its reagent across its own sub-blocks", () => {
  // The cue and the catalogue number are in different <p>s inside one caption —
  // one legend to a reader, two unrelated blocks to a character window.
  assert.match(found(splitLegend, "ab109535").label, /mentions immunofluorescence/i);
});

const plainProse = await scanHtml(article(
  `<h3>Western blot</h3><p>Membranes were probed with ab109535 (Abcam) at 1:1000.</p>`));

test("prose outside any table is unaffected", () => {
  // The layout pass must not disturb the path that already worked: a heading
  // above a methods paragraph is reached by the character window, as before.
  assert.match(found(plainProse, "ab109535").label, /mentions Western blot/i);
});

// Found by driving the real extension in Chromium, not by any test here — the
// minimal fixture above has no competing cue, and a real methods page has
// several. `scanApplications` recorded only the FIRST occurrence of each
// application, so "the closest cue" was chosen between first-occurrences: with
// a reagent table above, WB's position was the table's row 1 and IF's was its
// row 2, and the <h3> directly over the paragraph never entered the comparison.
const realisticMethods = await scanHtml(article(`
  <h3>Antibodies</h3>
  <table>
    <tr><th>Target</th><th>Catalogue</th><th>Application</th></tr>
    <tr><td>TDP-43</td><td>MAB7778</td><td>Western blot</td></tr>
    <tr><td>TDP-43</td><td>12892-1-AP</td><td>Immunofluorescence</td></tr>
  </table>
  <h3>Western blotting</h3>
  <p>Membranes were blocked and probed with ab109535 (Abcam) at 1:1000
  overnight, then developed by ECL.</p>`));

test("the nearest cue wins, not the first one in the window", () => {
  // The heading immediately above the paragraph says Western blotting; the only
  // IF cue on the page is in a table further away.
  const m = found(realisticMethods, "ab109535");
  assert.match(m.label, /mentions Western blot/i, m.label);
  assert.doesNotMatch(m.label, /immunofluorescence/i, `took a distant cue: ${m.label}`);
});

/* ------------------------------ a caution on the namings that earn one */
//
// Per-application precision on the 2 August re-run: WB 39 of 45 namings correct,
// IF 3 of 15, IP 0 of 3 (named three times on papers that never used it), IHC and
// FC never named at all. So the detector is trustworthy for one application and
// not for the rest, and the card says which of the two a reader is looking at.
//
// Hedging the reliable signal too is the failure mode this avoids: it teaches a
// reader to discount the caution, and then it is worth nothing where it counts.
// Same reasoning that kept cautionary language OFF the amber card, where the
// audit found 45 of 45 attributions right.

const cautionCases = [
  ["immunofluorescence", `<h3>Immunofluorescence</h3><p>Cells were probed with ab109535 (Abcam) at 1:200.</p>`],
  ["immunoprecipitation", `<h3>Immunoprecipitation</h3><p>Lysates were incubated with ab109535 (Abcam).</p>`],
];
const cautions = [];
for (const [name, html] of cautionCases) cautions.push([name, await marked(article(html))]);

for (const [name, marks] of cautions) {
  test(`a ${name} reading is flagged as unreliable`, () => {
    const hit = marks.find((m) => bare(m.text).includes("ab109535"));
    assert.ok(hit, "nothing marked");
    assert.match(hit.label, /unreliable/i, hit.label);
    assert.match(hit.label, /check what the paper used/i, hit.label);
  });
}

test("a western blot reading carries no caution", () => {
  const hit = found(plainProse, "ab109535");
  assert.match(hit.label, /mentions Western blot/i, hit.label);
  assert.doesNotMatch(hit.label, /unreliable|check what the paper/i,
    `the reliable naming was hedged too: ${hit.label}`);
});

// Awaited out here, not inside test(): test() does not await its callback, so a
// rejection from an async body is never seen and the check would always pass.
const cautionCard = await cardFor(
  article(`<h3>Immunofluorescence</h3><p>Cells were probed with ab109535 (Abcam) at 1:200.</p>`),
  "ab109535");

test("the caution names the application it doubts, and reaches the card", () => {
  assert.match(cautionCard, /Immunofluorescence/, "the card does not name what it doubts");
  assert.match(cautionCard, /unreliable/i, "the card carries no caution");
});

/* ------------------------------------------------- the connector hand-off */
//
// The two questions the extension has MEASURED itself unable to answer — which
// application a paper used, and whether it controlled its antibodies — both have
// the same honest destination. Two surfaces point there and must not drift, so
// the claim and the URL come from src/handoff.js and this checks both ends.

const handoffSrc = fs.readFileSync(path.join(root, "src/handoff.js"), "utf8");
const H = (() => { const g = {}; new Function("globalThis", handoffSrc).call(g, g); return g.OGAHandoff; })();

const handoffCard = await cardFor(
  article(`<p>Membranes were probed with ab109535 (Abcam) at 1:1000.</p>`), "ab109535");

test("the remedy sits beside the caution, and only there", () => {
  // The sentence that says the reading is unreliable has to carry the route
  // itself, or a reader told to go and check is left to find it two elements
  // down past the results.
  const why = cautionCard.slice(cautionCard.indexOf('class="why"'),
                                cautionCard.indexOf('class="chips"'));
  assert.ok(why.includes(H.URL), "the caution does not carry the link");
  assert.match(why, /AI connector/i, why);

  // …and exactly once on the card. The hand-off under the chips drops to the
  // half that has not been said, so two routes to one page never sit three
  // lines apart.
  const routes = cautionCard.split(H.URL).length - 1;
  assert.equal(routes, 1, `${routes} links to the connector on one card`);
  assert.match(cautionCard, /controlled its antibodies is also read/,
    "the controls half was dropped with the duplicate link");
});

test("the card offers the connector, on the card that has the record", () => {
  assert.ok(handoffCard.includes(H.URL), "no link to the connector on the card");
  assert.ok(handoffCard.includes("read from the whole paper"),
    `the claim is not the shared one: ${handoffCard}`);
});

test("the popup points at the same place, in the same words", () => {
  const popup = fs.readFileSync(path.join(root, "src/popup.html"), "utf8");
  const js = fs.readFileSync(path.join(root, "src/popup.js"), "utf8");
  assert.match(popup, /handoff\.js/, "the popup does not load the shared source");
  assert.match(js, /OGAHandoff/, "the popup does not read the shared source");
  // …and neither surface hard-codes a second copy of the URL.
  for (const [name, text] of [["popup.html", popup], ["popup.js", js],
                              ["card.js", fs.readFileSync(path.join(root, "src/card.js"), "utf8")]]) {
    assert.ok(!text.includes(H.URL), `${name} hard-codes the connector URL`);
  }
});

test("the hand-off claims nothing about this page", () => {
  // It is the destination for a question we did NOT answer. If it ever starts
  // reporting a finding, the removal that created it has been undone.
  assert.doesNotMatch(H.WHY, /\bthis paper\b|\bthis page\b/i, H.WHY);
});

/* --------------------------------------------------------------- settings */

// Awaited before the assertion, not inside it: test() is synchronous, so a
// promise handed to it resolves after the run has already reported success.
const greyOff = await scan("sage.html", { showGrey: false });

test("turning grey off removes grey marks and leaves the rest", () => {
  // Asserted through the real content script, because levelEnabled() is the
  // only thing standing between a setting and a page full of marks.
  assert.ok(!greyOff.some((m) => m.level === "grey"), "grey survived being turned off");
  assert.ok(greyOff.some((m) => m.level === "green"), "the rest of the page went with it");
});

/* ------------------------------------------------ the reference list */

// Both of these come from the 18 August 2026 Europe PMC field test, which read
// 241 highlights off 20 records and produced exactly one false positive.

const REF_PAGE = `<!doctype html><body>
  <div class="methods"><p>Western blotting used anti-TDP-43 antibody (Abcam, ab109535).</p></div>
  <section class="ref-list"><h2>References</h2><ol>
    <li>Smith J, et al. Anti-inflammatory signalling in cells. Virology. 2012;429(2):897-89.</li>
    <li>Jones K. A study of ab109535 in cortical neurons. J Cell Biol. 2019;218:1-12.</li>
  </ol></section></body>`;

const refMarks = await scanHtml(REF_PAGE);

test("a page range in the reference list is not read as a catalogue number", () => {
  // The live one was "Virology. 2012;429(2):136-47" -> 13647, a real Cell
  // Signaling antibody with a real green verdict, underlined on somebody else's
  // page numbers. 897-89 is the same shape against this fixture's index.
  assert.ok(!refMarks.some((m) => bare(m.text).includes("897-89")),
    `a page range was marked: ${JSON.stringify(refMarks.map((m) => m.text))}`);
});

test("an identifier inside the reference list is left alone", () => {
  assert.equal(refMarks.length, 1, `expected only the methods mention, saw ${JSON.stringify(refMarks.map((m) => m.text))}`);
  assert.ok(bare(refMarks[0].text).includes("ab109535"));
});

const HEADING_REF_PAGE = `<!doctype html><body><div id="article">
  <p>Western blotting used anti-TDP-43 antibody (Abcam, ab109535).</p>
  <h2>References</h2>
  <p>1. Smith J, et al. Anti-inflammatory signalling. Virology. 2012;429(2):897-89.</p>
</div></body>`;

const headingRefMarks = await scanHtml(HEADING_REF_PAGE);

test("a references heading with no class on it still ends the scannable text", () => {
  assert.ok(!headingRefMarks.some((m) => bare(m.text).includes("897-89")),
    `a page range under a bare References heading was marked: ${JSON.stringify(headingRefMarks.map((m) => m.text))}`);
  // And the article above the heading is untouched: excluding the heading's
  // PARENT instead of its following siblings would take the paper with it.
  assert.equal(headingRefMarks.length, 1, "the methods mention went with the references");
});

/* --------------------------------------- full text that lands after the scan */

const LATE_BODY = `<div class="methods"><p>Western blotting used anti-TDP-43 antibody
  (Abcam, ab109535) and 10782-2-AP (Proteintech).</p></div>`;

const accordion = boot(`<!doctype html><body><h1>Article</h1><section id="ft"></section></body>`);
await new Promise((r) => accordion.setTimeout(r, 40));
const beforeOpen = marksIn(accordion);
accordion.document.querySelector("#ft").innerHTML = LATE_BODY;
await new Promise((r) => accordion.setTimeout(r, 900));
const afterOpen = marksIn(accordion);

test("full text injected when the accordion opens is scanned", () => {
  assert.equal(beforeOpen.length, 0, "there was nothing to mark before the body landed");
  assert.equal(afterOpen.length, 2, `saw ${JSON.stringify(afterOpen.map((m) => m.text))}`);
});

// The Europe PMC failure itself: the body lands into a page that has NOT gone
// quiet. Citation counts, Altmetric badges and lazy images keep the mutation
// stream running, and a settle timer that restarts on every batch never fires
// -- so the article was never rescanned and the record showed zero highlights
// with nothing on screen saying a scan had not happened.
const busy = boot(`<!doctype html><body><h1>Article</h1><span id="noise"></span><section id="ft"></section></body>`);
await new Promise((r) => busy.setTimeout(r, 40));
const noise = busy.document.querySelector("#noise");
const ticking = busy.setInterval(() => noise.appendChild(busy.document.createElement("i")), 100);
busy.document.querySelector("#ft").innerHTML = LATE_BODY;
await new Promise((r) => busy.setTimeout(r, 2800));
busy.clearInterval(ticking);
const onBusyPage = marksIn(busy);

test("a page that never stops mutating is still rescanned", () => {
  assert.equal(onBusyPage.length, 2,
    `the settle timer starved: saw ${JSON.stringify(onBusyPage.map((m) => m.text))}`);
});

/* ----------------------------------------------------------------- report */

if (failures.length) {
  console.error(`\n${failures.length} failing, ${passed} passing\n`);
  for (const f of failures) console.error("  FAIL " + f + "\n");
  process.exit(1);
}
console.log(`all ${passed} page-level checks passed across ${CASES.length} publisher house styles`);
