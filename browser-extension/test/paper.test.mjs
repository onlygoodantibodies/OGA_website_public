/**
 * Paper identification and the looked-up application.  node test/paper.test.mjs
 *
 * Two things are pinned here, and they are the two that fail SILENTLY.
 *
 * 1. THE NORMALISER AGREES WITH PYTHON. The builder hashes a title in
 *    core/citations.py and the extension hashes it in src/paper.js. If those two
 *    ever disagree the extension simply stops recognising papers — no error, no
 *    empty result, just a feature that quietly does nothing on the subset of
 *    titles where they diverge. Both sides read the same vector file, so neither
 *    can move alone. (Greek letters are the reason: 9.1% of real titles carry
 *    one.)
 *
 * 2. A LOOKED-UP APPLICATION NARROWS AND A GUESSED ONE DOES NOT. That asymmetry
 *    is the whole citation layer. Collapsing it in either direction is invisible
 *    on screen: narrowing on proximity resurrects a verdict that is wrong a third
 *    of the time it speaks, and refusing to narrow on a lookup silently discards
 *    the feature while every card still renders perfectly.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..");
const repo = path.join(root, "..");

new Function(fs.readFileSync(path.join(root, "src/paper.js"), "utf8"))();
new Function(fs.readFileSync(path.join(root, "src/matcher.js"), "utf8"))();
const P = globalThis.OGAPaper;
const M = globalThis.OGAMatcher;

// The REAL findPaper out of the shipped service worker, not a copy of it. The
// worker deliberately holds no string rules -- but it does hold the lookup
// order, and testing a reimplementation of that would pin nothing. A stub
// `chrome` is enough: background.js only registers listeners at import time.
const noop = { addListener() {} };
globalThis.chrome = {
  runtime: { onInstalled: noop, onStartup: noop, onMessage: noop, getURL: (p) => p },
  alarms: { create() {}, onAlarm: noop },
  storage: { local: { get: async () => ({}), set: async () => {} },
             sync: { get: async () => ({}) } },
};
const { findPaper } = new Function(
  fs.readFileSync(path.join(root, "src/background.js"), "utf8") + "\nreturn { findPaper };")();

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed++; } catch (err) { failures.push(`${name}\n    ${err.message}`); }
}

/* ---------------------------------------------- 1. agreement with Python */

const vectorFile = path.join(repo, "core/data/title_normalisation_vectors.json");
test("title normalisation agrees with core/citations.py, vector for vector", () => {
  const { vectors } = JSON.parse(fs.readFileSync(vectorFile, "utf8"));
  assert.ok(vectors.length >= 50, `expected a real corpus, got ${vectors.length}`);
  const wrong = vectors.filter((v) =>
    P.normaliseTitle(v.title) !== v.normalised || P.titleKey(v.title) !== v.key);
  assert.equal(wrong.length, 0,
    `${wrong.length} of ${vectors.length} disagree, first: ${JSON.stringify(wrong[0])}`);
});

test("a Greek letter and its spelled-out name reach the same key", () => {
  // The case that motivated the transliteration table. If this fails, every
  // paper whose title carries a Greek letter silently stops being recognised.
  assert.equal(P.titleKey("Loss of β-catenin in stem cells"),
               P.titleKey("Loss of beta-catenin in stem cells"));
  assert.equal(P.titleKey("NF-κB signalling"), P.titleKey("NF-kappaB signalling"));
});

test("titleKey stays inside 32 bits (Math.imul, not *)", () => {
  // A plain `*` overflows to a float and diverges from Python on a fraction of
  // titles -- which would look exactly like patchy CiteAb coverage.
  for (const t of ["a", "an ordinary paper title about kinases", "ζ".repeat(40)]) {
    assert.match(P.titleKey(t), /^[0-9a-f]{8}$/);
  }
});

/* ------------------------------------------------------- 2. the lookup */

// Two reagents on one paper: AB_1 failed IF and passed WB; AB_2 passed everything.
const TABLE = {
  schema: 1,
  source: { citeab_data_through: "2026-05-05" },
  rrids: ["AB_1", "AB_2"],
  tokens: ["IHC", "ChIP"],
  //  reagent0 -> IF(4)                 reagent1 -> WB(1) + untested-app(32) token IHC(0)
  papers: ["0:4", "1:21.0"],
  years: [2023, 2021],
  by_title: { [P.titleKey("A paper about kinases")]: 0,
              [P.titleKey("Another paper")]: 1 },
  by_pmid: { "12345678": 0 },
  by_doi: {},
};

// The two halves as they actually run: the worker looks up, the page confirms.
const look = (keys) => P.confirmPaper(findPaper(TABLE, keys), keys);
const keysFor = (title, extra) => ({ titleKey: P.titleKey(title), ...(extra || {}) });

test("a missing table is `unavailable`, never `not-covered`", () => {
  // The distinction the whole card copy rests on: one is a fact about the
  // citation record, the other is a fact about us.
  assert.equal(look({ pmid: "12345678" }, null).status, "covered");  // sanity
  assert.equal(P.confirmPaper(findPaper(null, { pmid: "1" }), {}).status, "unavailable");
  assert.equal(P.confirmPaper(findPaper({}, { pmid: "1" }), {}).status, "unavailable");
  assert.equal(P.confirmPaper(findPaper(TABLE, null), {}).status, "unavailable");
});

test("a paper the table does not hold is `not-covered`", () => {
  assert.equal(look(keysFor("Some other paper", { pmid: "999" })).status, "not-covered");
});

test("PubMed id is tried before the title", () => {
  const found = look(keysFor("Another paper", { pmid: "12345678" }));
  assert.equal(found.matchedOn, "pmid");
  assert.deepEqual(Object.keys(found.reagents), ["AB_1"]);
});

test("a title match is confirmed against the year, and a wrong year refuses", () => {
  assert.equal(look(keysFor("A paper about kinases", { year: 2023 })).status, "covered");
  // one year of slack: online-first vs print
  assert.equal(look(keysFor("A paper about kinases", { year: 2024 })).status, "covered");
  // four years apart is a different paper, and attaching its applications to
  // this one is the one failure with no symptom on screen
  assert.equal(look(keysFor("A paper about kinases", { year: 2019 })).status, "not-covered");
});

test("a PubMed id match is NOT second-guessed by the year", () => {
  // A PubMed id IS the paper. Refusing it over a date the publisher printed
  // differently would throw away the most reliable key we have.
  assert.equal(look({ pmid: "12345678", year: 1999 }).status, "covered");
});

test("unpack names the applications OGA does not test, rather than a flag", () => {
  const found = look(keysFor("Another paper", { year: 2021 }));
  assert.deepEqual(found.reagents.AB_2.applications, ["WB"]);
  assert.deepEqual(found.reagents.AB_2.untestedApplications, ["IHC"]);
});

test("pageKeys hashes the declared title, and ignores document.title", () => {
  const doc = {
    location: { href: "https://example.org/x" },
    querySelector(sel) {
      if (sel.includes("citation_title")) return { content: "A paper about kinases" };
      if (sel.includes("citation_publication_date")) return { content: "2023-04-01" };
      return null;
    },
  };
  const keys = P.pageKeys(doc);
  assert.equal(keys.titleKey, P.titleKey("A paper about kinases"));
  assert.equal(keys.year, 2023);
  assert.equal(look(keys).status, "covered");
});

test("a PubMed id is read out of a pubmed.ncbi.nlm.nih.gov URL", () => {
  const doc = { location: { href: "https://pubmed.ncbi.nlm.nih.gov/12345678/" },
                querySelector: () => null };
  assert.equal(P.pageKeys(doc).pmid, "12345678");
});

/* --------------------------------- 3. narrowing: looked-up vs proximity */

const RECORD = { n: "ab1", g: "TDP43", s: "Abcam", a: { WB: 2, IP: 0, IF: 1, FC: 0 } };
const index = { geneSet: new Set(["TDP43"]) };

test("a looked-up application narrows the verdict to that application", () => {
  const status = M.resolveStatus(RECORD, ["IF"], null, index,
    { via: "paper", paperStatus: "covered" });
  assert.equal(status.level, "red", "failed IF, and the paper used it for IF");
  assert.equal(status.reason, "paper-application");
  assert.deepEqual(status.scopedTo, ["IF"]);
  assert.deepEqual(status.uncertain, [], "a lookup carries no proximity caution");
});

test("the SAME application guessed from the page does not narrow", () => {
  const status = M.resolveStatus(RECORD, ["IF"], null, index,
    { via: "proximity", paperStatus: "not-covered" });
  assert.equal(status.reason, "mentioned", "not paper-application");
  assert.equal(status.level, "mixed", "the whole record: passed WB, failed IF");
  assert.deepEqual(status.uncertain, ["IF"], "IF is not a reliable page cue");
});

test("an ambiguous CiteAb token is reported and never narrowed on", () => {
  // Bare `IF` in CiteAb does not say cultured cells or tissue, and OGA's IF
  // verdict is cultured cells only.
  const status = M.resolveStatus(RECORD, ["IF"], null, index,
    { via: "paper", paperStatus: "covered", ambiguous: true });
  assert.notEqual(status.reason, "paper-application");
  assert.equal(status.applicationAmbiguous, true);
  assert.equal(status.level, "mixed", "falls back to the whole record");
});

test("a looked-up application we never tested cannot manufacture a colour", () => {
  const untestedOnly = { ...RECORD, a: { WB: 0, IP: 0, IF: 0, FC: 0 } };
  const status = M.resolveStatus(untestedOnly, ["IF"], null, index,
    { via: "paper", paperStatus: "covered" });
  assert.notEqual(status.reason, "paper-application");
  assert.equal(status.level, "grey");
});

test("default source keeps every existing caller on proximity", () => {
  const withMode = M.resolveStatus(RECORD, ["IF"], null, index, undefined);
  assert.equal(withMode.via, "proximity");
  assert.equal(withMode.level, "mixed");
});

test("findMentions with no paper behaves exactly as before", () => {
  const idx = M.prepareIndex(JSON.parse(
    fs.readFileSync(path.join(root, "data/index.json"), "utf8")));
  const text = "Blots were probed with anti-TDP-43 (Abcam, ab109535) for western blot.";
  const a = M.findMentions(text, idx, undefined);
  const b = M.findMentions(text, idx, undefined, { status: "unavailable" });
  assert.deepEqual(a.map((h) => h.status.level), b.map((h) => h.status.level));
  assert.ok(a.length, "fixture should still produce a hit");
});

console.log(failures.length
  ? `\n${failures.length} FAILED:\n  ${failures.join("\n  ")}\n\n${passed} passed`
  : `all ${passed} paper tests passed`);
process.exit(failures.length ? 1 : 0);
