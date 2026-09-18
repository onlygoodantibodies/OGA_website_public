/**
 * Declared-target notices: the mark, the card and the three paper verdicts.
 *
 * These are pinned rather than left to a field test because every failure here
 * is SILENT. A notice that does not fire looks exactly like a page that does not
 * mention one of the eleven codes; a notice that fires on the wrong reagent draws
 * a red mark reading "antibody to another protein" over something perfectly
 * ordinary, and nothing on the publisher's page contradicts it. Neither is
 * visible without asking.
 *
 * The one case worth the most is `as_declared`. Seventeen of the 406 p16 papers
 * used the reagent correctly, for ARPC5, and a mark on those is a false
 * accusation against a competent paper — the direction this whole feature has to
 * be wrong in, if it is wrong at all. It is one `worthShowing` line away at all
 * times, and nothing else in any suite would notice it going.
 *
 * Node plus jsdom, no browser: this is the render and the lookup, and `tabs.mjs`
 * already drives the wiring in Chromium.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..");

new Function(fs.readFileSync(path.join(root, "src/matcher.js"), "utf8"))();
const M = globalThis.OGAMatcher;

const raw = JSON.parse(fs.readFileSync(path.join(root, "data/index.json"), "utf8"));
const index = M.prepareIndex(raw);

// A page that declares this DOI is one the p16 list records as a mistaken use.
const MISTAKEN_DOI = "10.1038/s41586-019-0885-0";
// …and this one the list records as a CORRECT use of the ARPC5 antibody.
const AS_DECLARED_DOI = "10.1038/jhg.2011.126";
// …and this one it could not check, because the full text was paywalled.
const NOT_CHECKED_DOI = "10.1016/j.bcp.2022.114935";
// A DOI on the list for a DIFFERENT product code (sc-166760, not ab51243). The
// verdict is per (paper, product) pair and this is the case that proves it: a
// paper on the list is not a paper on the list about the reagent in your hand.
const OTHER_CODE_DOI = "10.1016/j.exger.2019.110805";

const paperWith = (doi) => ({ status: "not-covered", pageDoi: doi || null });
// A page that declares no DOI at all — an e-Century title, which registers none
// with Crossref — and names itself by its PubMed id instead. Listed against
// Cell Signaling's #3179 in the PERK review.
const PMID_ONLY = "36915767";
const paperByPmid = (pmid) => ({ status: "not-covered", pageDoi: null,
                                 pagePmid: pmid || null });

const METHODS = "Membranes were probed with a p16 antibody (Abcam, ab51243) "
  + "and with anti-p21 overnight at 4C.";

const hitsOn = (text, paper) => M.findMentions(text, index, undefined, paper);
const oneHit = (text, paper, needle) => {
  const found = hitsOn(text, paper).filter((h) => h.matched.includes(needle));
  assert.equal(found.length, 1,
    `expected one hit on ${needle}, got ${found.length}: `
    + found.map((h) => h.matched).join(", "));
  return found[0];
};

test("a listed product code is marked red, not grey", () => {
  const hit = oneHit(METHODS, paperWith(null), "ab51243");
  assert.equal(hit.status.level, "red");
  assert.equal(hit.status.reason, "target-confusion");
  // The colour is shared with "tested and not supportive", so the thing that
  // tells them apart has to be on the status and not only in the copy.
  assert.equal(hit.record, null);
  assert.equal(hit.status.confusion.words.declared, "ARPC5 (p16-ARC)");
  assert.equal(hit.status.confusion.words.mistaken_protein, "p16-INK4a");
});

test("nothing about it claims a verdict", () => {
  const hit = oneHit(METHODS, paperWith(MISTAKEN_DOI), "ab51243");
  // OGA has tested none of the eleven codes. A notice that arrived carrying
  // passed/failed applications would draw chips, and a chip is a verdict.
  assert.deepEqual(hit.status.passed, []);
  assert.deepEqual(hit.status.failed, []);
  assert.deepEqual(hit.status.uncertain, []);
});

test("a paper the review recorded as a CORRECT use is not marked at all", () => {
  const found = hitsOn(METHODS, paperWith(AS_DECLARED_DOI));
  assert.deepEqual(found.map((h) => h.matched), [],
    "a paper that used the reagent for the protein it is sold against must "
    + "draw no mark: the list records 17 of these and a red mark on one is a "
    + "false accusation against a competent paper");
});

test("a paper the review could not check keeps the mark and says so", () => {
  const hit = oneHit(METHODS, paperWith(NOT_CHECKED_DOI), "ab51243");
  assert.equal(hit.status.confusion.verdict, "not_checked");
});

test("the DOI decides the verdict, and an unlisted paper gets the hedge", () => {
  assert.equal(
    oneHit(METHODS, paperWith(MISTAKEN_DOI), "ab51243").status.confusion.verdict,
    "mistaken");
  assert.equal(
    oneHit(METHODS, paperWith("10.9999/not-a-real-paper"), "ab51243")
      .status.confusion.verdict, null);
  assert.equal(
    oneHit(METHODS, paperWith(null), "ab51243").status.confusion.verdict, null);
  // On the list, for another product. Nothing may carry over: that paper cited
  // sc-166760, and reporting its verdict against ab51243 would be the citation
  // layer's "wrong paper" failure one field along.
  assert.equal(
    oneHit(METHODS, paperWith(OTHER_CODE_DOI), "ab51243").status.confusion.verdict,
    null);
});

test("the DOI is read however the page prints it", () => {
  for (const printed of [MISTAKEN_DOI, MISTAKEN_DOI.toUpperCase(),
                         `https://doi.org/${MISTAKEN_DOI}`, `doi:${MISTAKEN_DOI}`]) {
    // paper.js::normaliseDoi has already run by the time the matcher sees this,
    // so what is pinned here is that the stored key matches its output. A DOI
    // stored in one case and looked up in another is a map of near misses.
    const doi = printed.trim()
      .replace(/^(?:doi:|https?:\/\/(?:dx\.)?doi\.org\/)/i, "").toLowerCase();
    assert.equal(
      oneHit(METHODS, paperWith(doi), "ab51243").status.confusion.verdict,
      "mistaken", `not matched when the page printed it as ${printed}`);
  }
});

test("a paper whose journal registers no DOI is still reached, by its PMID", () => {
  // The release this exists for. e-Century titles (Am J Transl Res, Am J Cancer
  // Res) have no DOI at all, so before 0.4.2 four papers the reviewer HAD
  // established were reachable by nothing — and because the PERK lists are
  // papers_only, that was not a soft hedge but silence.
  const text = "Membranes were probed with a PERK antibody (Cell Signaling, "
    + "#3179) and with anti-p21 overnight at 4C.";
  assert.equal(
    oneHit(text, paperByPmid(PMID_ONLY), "3179").status.confusion.verdict,
    "mistaken");
  // And an unlisted PMID draws NO MARK AT ALL, rather than borrowing the
  // verdict — `perk-for-p-erk` is papers_only, so widening how a paper is
  // named must not widen what is said about a paper nobody reviewed.
  assert.equal(hitsOn(text, paperByPmid("99999999")).length, 0);
});

test("a code a page can only print without its hash is still reachable", () => {
  // Cell Signaling prints `#3179` and that is what the datasheet says, so it is
  // what is stored — but TOKEN_RE begins [A-Za-z0-9], so a page saying "#3179"
  // only ever yields `3179`, and the collapsed map cannot help because four
  // characters is below MIN_COLLAPSED. All three CST codes were in the payload
  // and reachable by nothing, which is two of the four papers this release adds.
  const text = "Membranes were probed with a PERK antibody (Cell Signaling, "
    + "#3179) and with anti-p21 overnight at 4C.";
  const hit = oneHit(text, paperByPmid(PMID_ONLY), "3179");
  // The card still prints the supplier's own spelling.
  assert.equal(hit.status.confusion.id, "#3179");
});

test("a four-digit code with no supplier beside it is not marked", () => {
  // The whole of why the alias above is safe. MIN_COLLAPSED exists because a
  // short number belongs to every supplier at once; `supplier_required` answers
  // that by demanding the manufacturer's name within the window, and without it
  // nothing is drawn at all.
  const text = "Membranes were probed with a PERK antibody (cat. 3179) and "
    + "with anti-p21 overnight at 4C.";
  assert.equal(hitsOn(text, paperByPmid(PMID_ONLY)).length, 0);
});

test("the keys a page is looked up under are the three paper.js uses", () => {
  assert.deepEqual(M.paperLookupKeys({ pageDoi: "10.1/x" }), ["10.1/x"]);
  assert.deepEqual(M.paperLookupKeys({ pagePmid: "123" }), ["pmid:123"]);
  // A title alone is refused: a title is a string two papers can share and the
  // year is what confirms it, which is paper.js::confirmPaper's own rule.
  assert.deepEqual(M.paperLookupKeys({ pageTitleKey: "abc" }), []);
  // The page is tried across the year slack, because online-first and print
  // dates routinely differ by one. The LIST row is stored under one year.
  assert.deepEqual(M.paperLookupKeys({ pageTitleKey: "abc", pageYear: 2020 }),
                   ["title:abc:2019", "title:abc:2020", "title:abc:2021"]);
  // Most certain first, so a DOI never loses to a title that happens to collide.
  assert.deepEqual(
    M.paperLookupKeys({ pageDoi: "10.1/x", pagePmid: "9", pageTitleKey: "a",
                        pageYear: 2020 }).slice(0, 2),
    ["10.1/x", "pmid:9"]);
  assert.deepEqual(M.paperLookupKeys(null), []);
});

test("a page declaring nothing still gets the product half", () => {
  // Unchanged behaviour, asserted because widening the lookup is exactly the
  // kind of change that could start throwing on a page with no metadata.
  const text = "Blots were probed with a p16 antibody (Abcam, ab51243).";
  const hit = oneHit(text, { status: "not-covered" }, "ab51243");
  assert.equal(hit.status.confusion.verdict, null);
  assert.ok(hit.status.confusion.words.declared);
});

test("a publisher's typesetting cannot hide a notice", () => {
  // The same rule as the catalogue lookup: a substituted dash or an invisible
  // character must not make a documented reagent read as one nobody reviewed.
  const set = ["ab‑51243", "ab–​51243", "sc—166760",
               "14–6773–81", "AB51243"];
  for (const printed of set) {
    const text = `Cells were stained with an antibody (Abcam, ${printed}).`;
    const found = hitsOn(text, paperWith(null));
    assert.equal(found.length, 1, `${printed} drew ${found.length} marks`);
    assert.equal(found[0].status.reason, "target-confusion");
  }

  // WHAT IS NOT COVERED, and it is the tokeniser's boundary rather than this
  // feature's: `TOKEN_RE` only closes up a space between digits with exactly
  // three digits after it, so `ab 51243` and `14 6773 81` tokenise as two words
  // and never reach any lookup — the catalogue path has the same gap. Widening
  // it would make "passage 12" and "Figure 3" candidate identifiers across EVERY
  // number on the page, which is a far larger false-positive surface than this
  // buys back. Pinned so a later widening is a deliberate edit to this line
  // rather than a surprise.
  for (const printed of ["ab 51243", "14 6773 81"]) {
    const text = `Cells were stained with an antibody (Abcam, ${printed}).`;
    assert.deepEqual(hitsOn(text, paperWith(null)).map((h) => h.matched), [],
      `${printed} is matched now — if that is intended, move it into the set above`);
  }
});

test("a code whose shape belongs to no supplier needs its supplier named", () => {
  // Millipore's AB986 is typeset exactly like an Abcam catalogue number, so
  // marking every AB986 would put a Millipore notice on an Abcam product.
  const withName = "Sections were probed with an antibody (Millipore, AB986).";
  const without = "Sections were probed with an antibody (cat. AB986).";
  assert.equal(hitsOn(withName, paperWith(null)).length, 1);
  assert.deepEqual(hitsOn(without, paperWith(null)).map((h) => h.matched), []);
  // And the same for the Thermo number, whose cue is the name a paper prints.
  assert.equal(
    hitsOn("an antibody (Invitrogen, A-11132)", paperWith(null)).length, 1);
  assert.deepEqual(
    hitsOn("an antibody (cat. A-11132)", paperWith(null)).map((h) => h.matched), []);
});

test("a listed code outside antibody prose is not marked", () => {
  // The higher bar, the same one the inferred marks clear. A red mark saying
  // "this is an antibody to another protein" over a recombinant enzyme or a kit
  // is a claim about the wrong reagent.
  const text = "Recombinant protein (ab51243) was resuspended in PBS.";
  assert.deepEqual(hitsOn(text, paperWith(null)).map((h) => h.matched), []);
});

test("an antibody with no notice is untouched", () => {
  const rrid = Object.keys(raw.antibodies)[0];
  const record = raw.antibodies[rrid];
  const text = `Blots were probed with an antibody (${record.n}).`;
  const hit = hitsOn(text, paperWith(MISTAKEN_DOI))
    .find((h) => h.rrid === rrid);
  assert.ok(hit, "a real catalogue number still resolves");
  assert.equal(hit.status.confusion, undefined,
    "a paper on a list must not attach its notice to every reagent in it");
  assert.notEqual(hit.status.reason, "target-confusion");
});

/* -------------------------------------------------------------- the card */

const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true });
for (const key of ["window", "document", "Node", "Element", "HTMLElement",
                   "MouseEvent", "getComputedStyle"]) {
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
}
new Function(fs.readFileSync(path.join(root, "src/card.js"), "utf8"))();
const Card = globalThis.OGACard;

function drawn(hit) {
  Card.show(dom.window.document.body, hit);
  const shadow = dom.window.document.querySelector("div").shadowRoot;
  return shadow.querySelector(".card").innerHTML;
}

test("the card leads with which protein the antibody is for", () => {
  const html = drawn(oneHit(METHODS, paperWith(MISTAKEN_DOI), "ab51243"));
  assert.match(html, /Antibody to ARPC5 \(p16-ARC\), not to p16-INK4a/);
  assert.match(html, /This paper is on a published list/);
  // Which antibody is for which target, in the reader's own terms.
  assert.match(html, /actin-related protein 2\/3 complex/);
  // The supplier's own statement, which is the strongest evidence on the card.
  assert.match(html, /no observed cross reactivity/i);
  // And the way to check it.
  assert.match(html, /forbetterscience\.com\/2026\/06\/02\/mind-over-antibody/);
  assert.match(html, /Read the review/);
});

test("the card says this is not a performance verdict", () => {
  const html = drawn(oneHit(METHODS, paperWith(MISTAKEN_DOI), "ab51243"));
  assert.match(html, /OGA has not tested this antibody/);
  // No chips, no scope line, no figure: there is no verdict to qualify, and a
  // caveat about what a result covers under a card holding none is the third
  // caveat on one screen.
  assert.doesNotMatch(html, /class="chips"/);
  assert.doesNotMatch(html, /consensus protocols/);
});

test("an unlisted paper is hedged, a listed one is not", () => {
  const hedged = drawn(oneHit(METHODS, paperWith(null), "ab51243"));
  assert.match(hedged, /may have used the wrong antibody/);
  assert.match(hedged, /317 papers/, "the hedge carries what is behind it");
  const stated = drawn(oneHit(METHODS, paperWith(MISTAKEN_DOI), "ab51243"));
  assert.doesNotMatch(stated, /may have used the wrong antibody/);
});

test("a paper that could not be checked is not told it was wrong", () => {
  const html = drawn(oneHit(METHODS, paperWith(NOT_CHECKED_DOI), "ab51243"));
  assert.match(html, /full text could not be reached/);
  assert.doesNotMatch(html, /papers that used it as/);
  assert.doesNotMatch(html, /may have used the wrong antibody/);
});

test("the beta-galactosidase notice draws its own words and link", () => {
  const text = "Senescence was assessed with a beta-gal antibody (Abcam, ab9361).";
  const html = drawn(oneHit(text, paperWith("10.1139/bcb-2018-0126"), "ab9361"));
  assert.match(html, /Antibody to lacZ \(E\. coli beta-galactosidase\)/);
  assert.match(html, /not to mammalian beta-galactosidase/);
  assert.match(html, /forbetterscience\.com\/2026\/07\/21\/do-bacteria-grow-old/);
  // One notice's wording must never reach another's card.
  assert.doesNotMatch(html, /p16/);
});

/* ------------------------------- a list that may only speak about its own papers */

/**
 * PERK vs p-ERK, and why it cannot behave like p16.
 *
 * A notice is found by the PRODUCT CODE, so by default a page naming one draws
 * a mark whatever paper it is, and with no verdict the card leads "This paper
 * MAY have used the wrong antibody" plus the review's tally. That is a fair
 * caution where the product is mostly misused: 317 of the 406 p16 papers used
 * `ab51243` as a p16-INK4a antibody.
 *
 * It is the wrong bet on PERK. `ab65142` is a perfectly good PERK antibody and
 * nearly every paper citing it used it correctly for the unfolded protein
 * response, so the same caution would be a false accusation against a correct
 * paper — the failure `as_declared` exists to prevent, arriving through a
 * different door. `papers_only` is the per-notice flag; these pin both sides of
 * it, because a flag that turned the caution off everywhere would be a
 * regression on the two lists that shipped with it.
 */

// On the PERK list: a 2016 Oncotarget paper that used ab65142 for p-ERK.
const PERK_DOI = "10.18632/oncotarget.10087";
const PERK_METHODS = "Sections were stained for p-ERK (Abcam, ab65142) and for "
  + "PKM2 with the antibody listed above.";

test("a papers_only notice marks a paper the list names", () => {
  const hits = hitsOn(PERK_METHODS, paperWith(PERK_DOI));
  const hit = hits.find((h) => h.matched.toLowerCase() === "ab65142");
  assert.ok(hit, `ab65142 was not marked: ${JSON.stringify(hits.map(h => h.matched))}`);
  assert.equal(hit.status.reason, "target-confusion");
  assert.equal(hit.status.confusion.verdict, "mistaken");
  assert.match(hit.status.confusion.words.declared, /EIF2AK3/);
});

test("and says nothing at all about a paper it does not", () => {
  const hits = hitsOn(PERK_METHODS, paperWith("10.1038/nature12345"));
  assert.ok(!hits.some((h) => h.status.reason === "target-confusion"),
    "an unlisted paper was marked: a PERK antibody used correctly for PERK "
    + "would be accused of being the wrong reagent");
});

test("nor about a page that declares no DOI", () => {
  const hits = hitsOn(PERK_METHODS, paperWith(null));
  assert.ok(!hits.some((h) => h.status.reason === "target-confusion"),
    "a page with no DOI was marked from the product code alone");
});

test("the p16 list keeps the caution it shipped with", () => {
  // The flag is per notice, and this is the assertion that makes that true:
  // 317 of 406 is a base rate that earns the warning, and turning it off here
  // would lose the whole point of marking an unlisted paper.
  const hits = hitsOn(METHODS, paperWith("10.1038/nature12345"));
  const hit = hits.find((h) => h.status.reason === "target-confusion");
  assert.ok(hit, "an unlisted p16 paper is no longer marked");
  assert.equal(hit.status.confusion.verdict, null);
  assert.ok(!hit.status.confusion.words.papers_only);
});

test("the bidirectional half is its own notice", () => {
  // sc-7383 is a p-ERK antibody used for PERK — the same collision the other
  // way round, and the schema carries one declared target per notice, so it is
  // a second file rather than a second field.
  const kinds = index.confusions.kinds;
  assert.match(kinds["perk-for-p-erk"].declared, /EIF2AK3/);
  assert.match(kinds["perk-for-p-erk"].mistaken_protein, /ERK1\/2/);
  assert.match(kinds["p-erk-for-perk"].declared, /MAPK/);
  assert.equal(kinds["p-erk-for-perk"].mistaken_protein, "PERK");
  for (const id of ["perk-for-p-erk", "p-erk-for-perk"]) {
    assert.equal(kinds[id].papers_only, true, `${id} is not papers_only`);
  }
});
