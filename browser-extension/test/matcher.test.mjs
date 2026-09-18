/**
 * Matcher tests. Run with:  node browser-extension/test/matcher.test.mjs
 *
 * These exercise the parts that decide what colour a reader sees, using real
 * records from the bundled index.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..");

// matcher.js attaches to globalThis, so evaluating it is enough.
const src = fs.readFileSync(path.join(root, "src/matcher.js"), "utf8");
new Function(src)();
const M = globalThis.OGAMatcher;

const raw = JSON.parse(fs.readFileSync(path.join(root, "data/index.json"), "utf8"));
const index = M.prepareIndex(raw);

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

const hitFor = (text, needle) => {
  const hits = M.findMentions(text, index);
  const hit = hits.find((h) => h.matched.toLowerCase().includes(needle.toLowerCase()));
  assert.ok(hit, `no hit matching "${needle}" in: ${hits.map((h) => h.matched).join(", ") || "(none)"}`);
  return hit;
};

// --- the headline case: one antibody, two applications, two verdicts --------

// The colour no longer follows the application the page seemed to name. A re-run
// of the 100-paper corpus measured why: 17 of the 54 papers the extension spoke
// about were told an application the paper had not used, and of 15
// immunofluorescence namings only 3 were right. So the reagent's whole record is
// what is shown and what the colour comes from; the detected application marks
// its row and says so in words.
//
// ab109535: WB recommended, IP and IF not. Its colour is therefore `mixed` on any
// page, and what changes between these two is only what the page was seen to say.

test("the page naming western blot does not narrow the verdict to WB", () => {
  const text = "Western blotting was performed with anti-TDP-43 antibody (Abcam ab109535) at 1:2000.";
  const hit = hitFor(text, "ab109535");
  assert.equal(hit.status.level, "mixed", "a held failure must never be painted green");
  assert.deepEqual(hit.status.mentioned, ["WB"], "the detection survives as a hint");
  assert.equal(hit.status.reason, "mentioned");
  assert.deepEqual(hit.status.passed, ["WB"]);
  assert.deepEqual(hit.status.failed, ["IP", "IF"]);
});

test("the same antibody on an immunofluorescence page reads identically", () => {
  // It used to go red here and green above, off an inference that is wrong four
  // times in five for this application.
  const text = "Immunofluorescence was performed with anti-TDP-43 antibody (Abcam ab109535) at 1:200.";
  const hit = hitFor(text, "ab109535");
  assert.equal(hit.status.level, "mixed");
  assert.deepEqual(hit.status.mentioned, ["IF"]);
  assert.deepEqual(hit.status.failed, ["IP", "IF"]);
});

test("failed in every assessed application -> red regardless of application", () => {
  const text = "Blots were probed with GTX630196 (GeneTex) overnight.";
  const hit = hitFor(text, "GTX630196");
  assert.equal(hit.status.level, "red");
});

test("passed in every assessed application -> green", () => {
  const text = "Immunoprecipitation used 10782-2-AP (Proteintech, 2 ug per reaction).";
  const hit = hitFor(text, "10782-2-AP");
  assert.equal(hit.status.level, "green");
  assert.deepEqual(hit.applications, ["IP"]);
});

test("two applications named -> both are marked, and the record is whole", () => {
  const text = "For western blotting and immunofluorescence we used antibody ab109535 (Abcam).";
  const hit = hitFor(text, "ab109535");
  assert.equal(hit.status.level, "mixed");
  assert.deepEqual(hit.status.mentioned, ["WB", "IF"]);
  // IP is reported even though the page never mentioned it — that is the point.
  assert.deepEqual(hit.status.failed, ["IP", "IF"]);
});

test("no application stated -> falls back to any-application verdict", () => {
  // ab109535 passes WB and fails IP and IF, so the fallback verdict is mixed.
  // This asserted green until the no-application branch was fixed — the test
  // encoded the bug, which is why it did not catch it.
  const text = "Antibodies used in this study: ab109535 (Abcam), RRID:AB_10859634.";
  const hit = hitFor(text, "AB_10859634");
  assert.equal(hit.status.reason, "no-application");
  assert.equal(hit.status.level, "mixed");
  assert.deepEqual(hit.status.mentioned, []);
});

// --- applications we have no data for must never inherit a verdict ---------

test("IHC is not an assessed application -> grey, not green", () => {
  const text = "Immunohistochemistry on paraffin sections was performed using ab109535 (Abcam).";
  const hit = hitFor(text, "ab109535");
  assert.equal(hit.status.level, "grey");
  assert.deepEqual(hit.status.unassessed, ["IHC"]);
});

test("flow cytometry was never tested for this antibody -> grey", () => {
  const text = "Flow cytometry was performed with ab109535 (Abcam).";
  const hit = hitFor(text, "ab109535");
  assert.equal(hit.status.level, "grey");
  assert.equal(hit.status.reason, "app-not-tested");
});

// --- blue and grey --------------------------------------------------------

test("unknown antibody against a target we have data for -> blue", () => {
  const text = "Western blotting used an anti-TDP-43 antibody (Fictional Bio, cat# ZZ9999).";
  const hits = M.findMentions(text, index);
  const hit = hits.find((h) => h.status.level === "blue");
  assert.ok(hit, `expected an blue hit, got: ${hits.map((h) => h.status.level).join(", ")}`);
  assert.equal(hit.status.gene, "TARDBP");
});

test("target that is not in the dataset at all -> grey", () => {
  const text = "Western blotting used an anti-Flotillin-1 antibody (cat# QQ1234).";
  const hits = M.findMentions(text, index);
  assert.ok(!hits.some((h) => ["green", "red", "blue"].includes(h.status.level)),
    `expected nothing but grey, got: ${hits.map((h) => h.status.level).join(", ")}`);
});

test("a reagent named twice is highlighted once, not blue beside a real verdict", () => {
  const text = "Membranes were probed with anti-TDP-43 antibody (Abcam ab109535, RRID:AB_10859634).";
  const hits = M.findMentions(text, index);
  assert.ok(!hits.some((h) => h.via === "target"),
    `the target phrase belongs to the catalogue number beside it: ${hits.map((h) => h.matched).join(", ")}`);
  // What matters is that no blue sits next to a looked-up verdict for the
  // same reagent, and that the marks agree with each other. Which verdict it
  // is belongs to the tests above; pinning "green" here is what let this test
  // keep passing while green was wrong.
  assert.ok(!hits.some((h) => h.status.level === "blue"), "blue beside a real verdict");
  assert.equal(new Set(hits.map((h) => h.status.level)).size, 1, "the two marks disagree");
});

test("a second, untested reagent against the same target keeps its blue", () => {
  const text = "Immunofluorescence used ab109535 (Abcam) at 1:200 on a confocal microscope. "
    + "An additional anti-TDP-43 antibody (Fictional Biosciences, cat# ZZ9999) was used for comparison.";
  const hits = M.findMentions(text, index);
  assert.ok(hits.some((h) => h.status.level === "blue"),
    `expected the second reagent to stay blue: ${hits.map((h) => h.status.level).join(", ")}`);
});

test("a heading in the next section does not govern this section's reagents", () => {
  // "Western blotting" heading, then the reagent, then an "Immunofluorescence"
  // heading below. Only the heading above applies.
  const text = "Western blotting\nMembranes were probed overnight with ab109535 at 1:2000.\nImmunofluorescence\nCells were fixed in paraformaldehyde.";
  const hit = hitFor(text, "ab109535");
  // Asserted on the DETECTION, not on the colour. The colour is a property of
  // the record now, so pinning it here would pass whatever the inference did —
  // which is exactly how a scoping bug hides.
  assert.deepEqual(hit.applications, ["WB"]);
  assert.deepEqual(hit.status.mentioned, ["WB"]);
});

test("gene alias resolves to the official symbol", () => {
  assert.equal(M.resolveGene("TDP-43", index), "TARDBP");
  assert.equal(M.resolveGene("TDP43", index), "TARDBP");
  assert.equal(M.resolveGene("progranulin", index), "GRN");
  assert.equal(M.resolveGene("p62", index), "SQSTM1");
});

// --- false-positive guards -------------------------------------------------

test("a bare number with no antibody context is ignored", () => {
  // 89718 is a real Cell Signaling catalogue number in the index.
  const text = "Between 1990 and 2020 the cohort grew to 89718 participants across all sites.";
  const hits = M.findMentions(text, index);
  assert.equal(hits.length, 0, `expected no hits, got: ${hits.map((h) => h.matched).join(", ")}`);
});

test("the same bare number IS matched when antibody context is present", () => {
  const text = "Blots were probed with anti-TDP-43 antibody (Cell Signaling Technology, Cat# 89718) overnight.";
  const hit = hitFor(text, "89718");
  assert.equal(hit.record.s, "Cell Signaling Technology");
});

test("RRID wins over an overlapping catalogue token", () => {
  const text = "Western blot used ab109535 (RRID:AB_10859634).";
  const hits = M.findMentions(text, index);
  const rridHits = hits.filter((h) => h.via === "rrid");
  assert.equal(rridHits.length, 1);
  // No double-decoration of the same span.
  for (let i = 1; i < hits.length; i++) {
    assert.ok(hits[i].start >= hits[i - 1].end, "hits must not overlap");
  }
});

test("ordinary prose produces no hits", () => {
  const text = "The results in Figure 2 show a clear increase in signal over time in all three replicates.";
  assert.equal(M.findMentions(text, index).length, 0);
});

// --- findings from testing on real papers ----------------------------------

test("a target is never read out of a neighbouring table row", () => {
  // Key Resources tables put each reagent on its own row / text node. Reading
  // across rows made an unrelated RRID claim the next row's target.
  const table = [
    "anti-mCherry antibody (BioLegend, 677701)  RRID:AB_2801131",
    "anti-p62 antibody (Abcam, ab109012)  RRID:AB_10861798",
  ].join("\n");
  const hits = M.findMentions(table, index);
  assert.ok(!hits.some((h) => h.matched.includes("AB_2801131")),
    `the mCherry row must not be marked: ${hits.map((h) => h.matched).join(", ")}`);
  assert.ok(hits.some((h) => h.status.level === "blue" && h.status.gene === "SQSTM1"),
    "the p62 row should still be blue");
});

test("reagents we hold no data on are not marked at all", () => {
  // One real page produced 25 marks, every one of them a grey on an unknown
  // RRID. That buries the marks that mean something.
  const text = "anti-TRPA1 (Novus, NB110-40763) RRID:AB_714801; anti-TRPV1 (Alomone, ACC-030) RRID:AB_2040264";
  assert.deepEqual(M.findMentions(text, index), [],
    "an all-unknown reagent list should produce nothing");
});

test("grey is kept where it is informative", () => {
  // In the dataset, but used in an application nobody assessed.
  const text = "Immunohistochemistry on paraffin sections was performed with ab254166 (Abcam).";
  const hits = M.findMentions(text, index);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].status.level, "grey");
});

test("a spelled-out target name resolves to its gene", () => {
  const text = "primary antibodies against amyloid precursor protein (APP, ab101492, 1:100, Abcam) overnight";
  const hits = M.findMentions(text, index);
  const blue = hits.find((h) => h.status.level === "blue");
  assert.ok(blue, `expected blue for APP: ${hits.map((h) => h.status.level).join(", ")}`);
  assert.equal(blue.status.gene, "APP");
});

// --- application cues ------------------------------------------------------

test("the adjective forms papers actually use are recognised", () => {
  // "-istr" only ever spells the noun, so the adjectives silently missed. The
  // IHC one mattered most: that cue exists to refuse a verdict, not give one.
  const forms = {
    immunohistochemical: "IHC", immunohistochemically: "IHC", "immuno-histochemical": "IHC",
    immunohistochemistry: "IHC", immunocytochemical: "IF", immunoblotted: "WB",
    "western blotted": "WB", immunofluorescent: "IF",
  };
  for (const [word, app] of Object.entries(forms)) {
    const text = `Samples were analysed by ${word} with ab109535 (Abcam).`;
    assert.deepEqual(M.inferApplications(text, text.indexOf("ab109535")), [app],
      `"${word}" should read as ${app}`);
  }
});

test("an antibody used for IHC is never shown its Western blot verdict", () => {
  const text = "For immunohistochemical staining, the primary detection antibodies used were anti-TARDBP (Abcam; ab109535).";
  const hit = M.findMentions(text, index).find((h) => h.record);
  assert.equal(hit.status.level, "grey");
  assert.equal(hit.status.reason, "app-not-assessed");
  assert.deepEqual(hit.status.unassessed, ["IHC"]);
});

test("a heading carries its application down to the reagents listed under it", () => {
  const text = "Immunofluorescence staining\nCells were fixed in 4% PFA.\nThey were then incubated with ab109535 overnight.";
  assert.deepEqual(M.inferApplications(text, text.indexOf("ab109535")), ["IF"]);
});

test("a sentence does not begin in a previous block", () => {
  // Blocks with no full stop in them at all — a table's cells. The enclosing
  // sentence was searched backwards to the last "." anywhere in the document, so
  // with none it began at character 0 and the "sentence" was every row above.
  //
  // The filler puts the cue out of reach of the fallback window below, which is
  // what isolates the sentence scan: that window looks back across blocks on
  // purpose, because it is how a section heading reaches its reagents.
  const rows = `Western blot\n${"filler ".repeat(120)}\nMAB7778`;
  assert.deepEqual(M.inferApplications(rows, rows.indexOf("MAB7778")), [],
    "an application from an earlier block leaked in as this one's sentence");
});

test("layout context is optional and only consulted when the text is silent", () => {
  // The caller supplies what a block inherits from where it sits. Without it,
  // nothing changes — every other test here calls findMentions with no blocks.
  const rows = "Antibody\nab109535\nDilution\n1:200";
  const at = rows.indexOf("ab109535");
  const blocks = [{ start: at, end: at + 8, context: "Immunofluorescence" }];
  assert.deepEqual(M.inferApplications(rows, at, blocks), ["IF"]);

  // …but it never overrules what the sentence itself says.
  const stated = "Membranes were immunoblotted with ab109535 at 1:1000.";
  const from = stated.indexOf("ab109535");
  assert.deepEqual(
    M.inferApplications(stated, from, [{ start: 0, end: stated.length, context: "Immunofluorescence" }]),
    ["WB"]);
});

// --- clone IDs -------------------------------------------------------------

test("an antibody cited only by its clone is matched", () => {
  // "anti-CaMKII (pan) (D11A10) antibody" gives no catalogue number at all,
  // and the clone identifies the product just as precisely.
  const text = "Western blotting using anti-TDP-43 (pan) (EPR5810) antibody (b, d, top).";
  const hit = M.findMentions(text, index).find((h) => h.via === "clone");
  assert.ok(hit, "the clone should be matched");
  assert.equal(hit.record.n, "ab109535");
  assert.equal(hit.record.cl, "EPR5810");
});

test("a concentration is never read as an identifier", () => {
  // "A71 antibody (200 ng/ml, 1:1000)" put a real verdict on a dilution: 200 is
  // a clone ID in the live data and every other gate passed. What separates the
  // two is what follows the number, not the number.
  const weak = { ...raw, genes: [...raw.genes, "GBA1"] };
  weak.antibodies = { ...raw.antibodies, AB_TEST: { n: "abtest", s: "Abcam", g: "GBA1", cl: "200", a: { WB: 2, IP: 1, IF: 1, FC: 0 }, ind: true } };
  weak.clones = { ...(raw.clones || {}), 200: "AB_TEST" };
  const idx = M.prepareIndex(weak);

  const text = "A71 antibody (200 ng/ml, 1:1000) or antibody 3135 (1:10,000) was added overnight.";
  assert.deepEqual(M.findMentions(text, idx), []);

  // Still reachable where the paper actually names it as a clone.
  const named = "The antibody (clone 200) was used at 1:500 for western blotting.";
  const hit = M.findMentions(named, idx).find((h) => h.via === "clone");
  assert.ok(hit, "an explicitly named clone should still match");
});

test("a long list of antibodies is not thrown away by distance", () => {
  // The commonest methods form: "the primary antibodies were listed below:"
  // once, then twenty reagents run together. By the twentieth the word is
  // hundreds of characters back, and a distance-only rule loses the lot.
  const idx = M.prepareIndex({ ...raw, genes: [...raw.genes, "TARDBP", "APP"] });
  const text = "The blots were developed using the ChemiDoc system (Bio-Rad). Information about the "
    + "primary antibodies was listed below: RNF8 (Santa Cruz, sc-271462); p-H3 (Ser10) (Cell "
    + "Signaling Technology, 3377S); TARDBP (Santa Cruz, sc-100362); APP (Proteintech, 20667-1-AP).";
  const genes = M.findMentions(text, idx).filter((h) => h.via === "gene-adjacent").map((h) => h.status.gene);
  assert.ok(genes.includes("TARDBP"), `lost the far end of the list: ${genes.join(", ")}`);
  assert.ok(genes.includes("APP"), `lost the far end of the list: ${genes.join(", ")}`);
});

test("Santa Cruz numbering is matchable", () => {
  // sc-271462: the hyphen after the letters made the whole supplier invisible.
  const idx = M.prepareIndex({ ...raw, genes: [...raw.genes, "TARDBP"] });
  const text = "The primary antibodies used were TARDBP (Santa Cruz, sc-100362).";
  const hit = M.findMentions(text, idx).find((h) => h.via === "gene-adjacent");
  assert.ok(hit, "sc- numbers should match");
  assert.equal(hit.matched, "sc-100362");
});

test("a reagent that is not an antibody gets no inferred mark", () => {
  // Recombinant enzymes are bought by catalogue number from named suppliers and
  // read identically to an antibody. "cat no." is not evidence of an antibody.
  const idx = M.prepareIndex({ ...raw, genes: [...raw.genes, "MMP7"] });
  const enzyme = "Heparitinase I, II, III, chondroitinase ABC, and MMP7 (cat no. M4565) were purchased from Sigma-Aldrich (St. Louis, MO).";
  assert.ok(!M.findMentions(enzyme, idx).some((h) => h.via === "gene-adjacent"),
    "an enzyme should not be blueed as an antibody");

  const antibody = "Antibodies against MMP7 (cat no. M4565) were purchased from Sigma-Aldrich.";
  const hit = M.findMentions(antibody, idx).find((h) => h.via === "gene-adjacent");
  assert.ok(hit && hit.status.level === "blue", "the same shape, but an antibody, still blues");
});

test("a clone is only trusted with antibody wording nearby", () => {
  // Clones are short and collide easily -- "4F9", "1M10", "DB9".
  const text = "Sample EPR5810 was collected from patient 4 and sequenced.";
  assert.deepEqual(M.findMentions(text, index), []);
});

test("the clone does not also draw an blue for its own target", () => {
  const text = "Western blotting using anti-TDP-43 (pan) (EPR5810) antibody.";
  const levels = M.findMentions(text, index).map((h) => h.status.level);
  assert.ok(!levels.includes("blue"), `one reagent, one mark: got ${levels.join(", ")}`);
});

// --- a target named against an unheld catalogue number ---------------------

test("a target named immediately before an unknown catalogue number goes blue", () => {
  // The shape almost every methods section uses, and the one that produced
  // nothing at all before: the paper states the attribution, so there is no
  // guessing involved.
  const text = "Antibodies against APP(#A2164), ATF6(#A0202) were purchased from ABclonal.";
  const hits = M.findMentions(text, index);
  assert.equal(hits.length, 1, `expected one mark, got ${hits.map((h) => h.matched).join(", ")}`);
  assert.equal(hits[0].matched, "A2164");
  assert.equal(hits[0].status.level, "blue");
  assert.equal(hits[0].status.gene, "APP");
});

test("the spacing and prefixes real papers use are all covered", () => {
  for (const text of [
    "Antibody against APP (#A2164) was used. Abcam supplied it.",
    "APP (EPR1234) (#A2164) antibody, Abcam",
    "anti-APP (Abcam, cat# A2164) was used at 1:1000",
  ]) {
    const hits = M.findMentions(text, index);
    const blue = hits.filter((h) => h.status.level === "blue" && h.status.gene === "APP");
    assert.equal(blue.length, 1, `expected exactly one APP blue in: ${text}`);
  }
});

test("a parenthesised number that is not a catalogue number stays unmarked", () => {
  // Dilutions, years and figure callouts sit in exactly the same position.
  for (const text of [
    "The APP antibody (1:1000) was incubated overnight.",
    "APP (2019) reported this previously.",
    "HRP Goat Anti-Mouse IgG(#AS003) was used as the secondary antibody.",
  ]) {
    const hits = M.findMentions(text, index);
    assert.ok(!hits.some((h) => h.via === "gene-adjacent"),
      `should not infer a catalogue number from: ${text}`);
  }
});

test("a known catalogue number keeps its real verdict rather than going blue", () => {
  const text = "Antibodies against TARDBP (Abcam, cat# ab109535) were used for western blotting.";
  const hits = M.findMentions(text, index);
  const hit = hits.find((h) => h.matched.toLowerCase() === "ab109535");
  assert.ok(hit, "the known catalogue number should still be found");
  assert.ok(hit.record, "it should carry its record, not be inferred");
  assert.notEqual(hit.status.level, "blue");
});

// --- the no-application path, all four shapes ------------------------------
//
// resolveStatus() tested passedAll.length before it looked at failedAll, so an
// antibody that passes some assessed applications and fails others came back
// GREEN — with the failures sitting in status.failed, which the green card
// does not lead with. `mixed` was used correctly in the per-application branch
// and was unreachable here.
//
// It multiplies: 43.1% of the shipped index passes some and fails others, and
// a mark-level audit found this branch is taken on 78% of marks, because
// inferApplications() usually cannot tell what a page used a reagent for. It
// hid in the first benchmark because 17 of that corpus's 18 antibodies fail
// every assessed application, so failedAll-only always returned red.

const noApp = (record) => M.resolveStatus(record, [], null, index);

// --- yellow: not supportive, and yet it did the thing ------------------------
//
// "Detected the target and did not meet the bar" and "showed nothing" are
// different findings, and a reader deciding whether to try a reagent needs them
// apart. On live data that is 491 of the 1,833 negatives the index publishes.
//
// The mark is ONE colour for a whole reagent while the card lists each
// application, so yellow has to mean "nothing here showed nothing" — a single
// unqualified failure keeps it red. That conservative direction is the thing to
// pin: getting it wrong paints a reagent that failed outright as one worth a
// try.

test("every failed application qualified -> yellow, not red", () => {
  const status = noApp({ g: "GBA1", a: { WB: 1, IP: 1, IF: 0, FC: 0 },
                         q: { WB: "sd", IP: "se" } });
  assert.equal(status.level, "yellow");
});

test("one failure that showed nothing keeps the mark red", () => {
  const status = noApp({ g: "GBA1", a: { WB: 1, IP: 1, IF: 0, FC: 0 },
                         q: { WB: "sd" } });
  assert.equal(status.level, "red", "a reagent that showed nothing must not read as yellow");
});

test("no qualifiers at all is red, exactly as before", () => {
  assert.equal(noApp({ g: "GBA1", a: { WB: 1, IP: 0, IF: 0, FC: 0 } }).level, "red");
});

test("a record built before the key existed still resolves", () => {
  // The index ships `q` sparsely and an older cached copy has none at all.
  const status = noApp({ g: "GBA1", a: { WB: 1, IP: 0, IF: 0, FC: 0 } });
  assert.equal(status.level, "red");
  assert.deepEqual(status.qualifiers, {});
});

test("a supportive verdict is never repainted by its qualifier", () => {
  // `ns` is the tab on a green cell, not a colour of its own: the antibody IS
  // supported for the application.
  const status = noApp({ g: "GBA1", a: { WB: 2, IP: 0, IF: 0, FC: 0 },
                         q: { WB: "ns" } });
  assert.equal(status.level, "green");
});

test("a looked-up application narrows to yellow the same way", () => {
  const record = { g: "GBA1", a: { WB: 1, IP: 2, IF: 0, FC: 0 }, q: { WB: "sd" } };
  // `via: "paper"` is what makes it a LOOKUP rather than a proximity guess, and
  // only a lookup may narrow the verdict to one application.
  const status = M.resolveStatus(
    record, ["WB"], null, index, { via: "paper", applications: ["WB"] });
  assert.equal(status.level, "yellow", "the citation layer must carry the distinction too");
});

test("no application stated, passes some and fails others -> mixed, not green", () => {
  // ab109535: WB recommended, IP and IF not recommended. The exact shape that
  // was painting green over two published knockout-controlled failures.
  const status = noApp(index.antibodies.AB_10859634);
  assert.equal(status.level, "mixed", "an antibody with published failures must never be green");
  assert.equal(status.reason, "no-application", "the card still has to explain the page said nothing");
  assert.deepEqual(status.passed, ["WB"]);
  assert.deepEqual(status.failed, ["IP", "IF"]);
});

test("no application stated, passes only -> green", () => {
  // 10782-2-AP: WB, IP and IF all recommended, FC never assessed.
  const status = noApp(index.antibodies.AB_615042);
  assert.equal(status.level, "green");
  assert.equal(status.reason, "no-application");
  assert.deepEqual(status.failed, [], "green must carry no failures at all");
});

test("no application stated, fails only -> red", () => {
  // GTX630196 fails every assessed application.
  const status = noApp(index.antibodies.AB_2888198);
  assert.equal(status.level, "red");
  assert.equal(status.reason, "no-application");
  assert.deepEqual(status.passed, []);
});

test("no application stated, no verdict anywhere -> grey", () => {
  const status = noApp({ g: "TARDBP", a: { WB: 0, IP: 0, IF: 0, FC: 0 } });
  assert.equal(status.level, "grey");
  assert.equal(status.reason, "record-untested");
});

test("no antibody in the index is ever green while holding a failure", () => {
  // The invariant behind the four shapes above, swept over every record the
  // bundled index carries rather than the three named ones.
  for (const [rrid, record] of Object.entries(index.antibodies)) {
    const status = noApp(record);
    if (status.level !== "green") continue;
    const { failed } = M.recordVerdicts(record);
    assert.deepEqual(failed, [], `${rrid} (${record.n}) is green but fails ${failed.join(", ")}`);
  }
});

test("a mention with no stated application picks the same verdict end to end", () => {
  // Through findMentions rather than resolveStatus directly, because the bug
  // reached a reader through a painted mark, not through a unit call.
  const hit = hitFor("The antibody ab109535 (Abcam) was obtained commercially.", "ab109535");
  assert.deepEqual(hit.applications, [], "this fixture must exercise the no-application path");
  assert.equal(hit.status.level, "mixed");
});

// --- a mark must not run past what resolved --------------------------------
//
// TARGET_PATTERNS[1] captures up to four whitespace-separated words, and its
// character class included a bare `.`, so a full stop did not end the capture.
// findMentions then marked m[0] in full regardless of how much resolveGene had
// actually consumed. On a real Cell Death & Disease figure legend the mark ran
// 38 characters past its target, through the sentence boundary and a panel
// label, and ended on a cell line — with the whole string printed as the card
// headline. 8 of 123 audited marks came from this pattern; 2 over-ran.

const spanOf = (text, needle) => {
  const hit = M.findMentions(text, index).find((h) => h.via === "target");
  assert.ok(hit, `no target-pattern mark in: ${text}`);
  return text.slice(hit.start, hit.end);
};

test("a capture never crosses a sentence boundary", () => {
  const text = "…analyzed by immunoblotting using antibodies against TDP-43 and Parkin. "
    + "i HepG2 were transfected with scRNA…";
  const span = spanOf(text, "TDP-43");
  assert.equal(span, "antibodies against TDP-43");
  assert.ok(!span.includes("."), `the mark crossed the full stop: "${span}"`);
  assert.ok(!/HepG2|\bi\b/.test(span), `the mark reached the panel label or the cell line: "${span}"`);
});

test("a mark stops at the words that resolved, not at the end of the match", () => {
  assert.equal(
    spanOf("Blots used antibodies to TDP-43 raised in rabbit at 1:1000 for western blotting."),
    "antibodies to TDP-43", "it swallowed the rest of the phrase");
});

test("a spelled-out target keeps all the words that resolved it", () => {
  // The shrink must not undo the four-word capture's whole reason for existing.
  assert.equal(
    spanOf("Antibodies against amyloid precursor protein overnight at 4C were used for immunoblotting."),
    "Antibodies against amyloid precursor protein");
});

test("the other two target patterns keep what follows their capture", () => {
  // "X antibody" captures X and matches " antibody" after it; shrinking to the
  // capture alone would cut the word that identified it as an antibody.
  assert.equal(
    spanOf("An additional TDP-43 antibody (Vendor, cat# ZZ9999) was used for western blotting."),
    "TDP-43 antibody");
  assert.equal(
    spanOf("Immunoblotting used anti-TDP-43 at 1:1000."),
    "anti-TDP-43");
});

test("no mark anywhere contains a full stop followed by a space", () => {
  // The cheap invariant: a span that spans a sentence is always wrong, whatever
  // produced it. This would have caught the bug above on any page.
  const pages = [
    "…analyzed by immunoblotting using antibodies against TDP-43 and Parkin. i HepG2 were transfected…",
    "Antibodies against amyloid precursor protein and Beclin 1. Panel b shows HepG2 cells.",
    "Western blotting used ab109535 (Abcam). Immunofluorescence used GTX630196 (GeneTex).",
    "Antibodies were anti-TDP-43. Cells were then lysed in RIPA buffer.",
  ];
  for (const text of pages) {
    for (const hit of M.findMentions(text, index)) {
      const span = text.slice(hit.start, hit.end);
      assert.ok(!/\.\s/.test(span), `mark spans a sentence: "${span}"`);
    }
  }
});

// --- blue has to show its working -----------------------------------------
//
// Blue is the only verdict the extension infers rather than looks up, and it
// was 62 of the 239 marks drawn across the benchmark. The card quotes the text
// that produced the gene, so these pin the two things that quote depends on:
// which words resolved, and how they came to be attached to this identifier.

test("targetVia says how the target was attributed, which `via` does not", () => {
  // From the dataset row: certain.
  const looked = hitFor("Western blotting used ab109535 (Abcam) antibody.", "ab109535");
  assert.equal(looked.targetVia, "record");

  // Stated by the page against the catalogue number.
  const stated = hitFor("Antibodies against TARDBP(#A2164) were purchased from ABclonal.", "A2164");
  assert.equal(stated.targetVia, "stated");
  assert.equal(stated.via, "gene-adjacent");

  // Nearest resolvable name in the block — the one that can be wrong.
  const nearby = hitFor("Anti-TDP-43 antibody was used (RRID:AB_9999999) for western blotting.", "AB_9999999");
  assert.equal(nearby.targetVia, "nearby");
  assert.equal(nearby.status.level, "blue");
});

test("target.raw is the phrase that resolved, not the whole capture", () => {
  // TARGET_PATTERNS grabs up to four words, so the capture routinely carries
  // trailing prose. Quoting that back at a reader would print
  // "TDP-43 antibody overnight" and make a correct inference look broken.
  const hit = hitFor("Antibodies against TDP-43 overnight at 4C were used (RRID:AB_9999999).", "AB_9999999");
  assert.equal(hit.status.level, "blue");
  assert.ok(!/overnight/i.test(hit.target.raw), `raw carried trailing prose: "${hit.target.raw}"`);
});

test("the quoted phrase is what the page wrote, and the gene is what it resolved to", () => {
  // The alias case is the whole reason for showing the reader both: the page
  // says TDP-43, the dataset says TARDBP, and the reader has to see the step.
  const hit = hitFor("An additional anti-TDP-43 antibody (Vendor, cat# QQ7781) was used for western blotting.", "anti-TDP-43");
  assert.equal(hit.status.level, "blue");
  assert.equal(hit.target.raw, "TDP-43");
  assert.equal(hit.status.gene, "TARDBP");
});

test("a target attributed from the record is never blue", () => {
  // targetVia "record" means the dataset told us; there is nothing to check
  // and nothing to quote. Blue must never reach the card with that.
  for (const text of [
    "Western blotting used ab109535 (Abcam) antibody.",
    "Blots were probed with GTX630196 (GeneTex) overnight.",
  ]) {
    for (const hit of M.findMentions(text, index)) {
      if (hit.targetVia === "record") assert.notEqual(hit.status.level, "blue", text);
    }
  }
});

// --- grey must not hide a failure ------------------------------------------
//
// On 7 of 95 benchmark pages the target antibody was headed "No independent
// data" for antibodies that are in the dataset and FAILED knockout-controlled
// testing — the page had used them for IHC, or for an assessed application
// with no result. status.passed / status.failed are empty on those branches,
// so anything reporting what we hold has to read the record instead.

test("a record's verdicts are read from the record, not from the page's applications", () => {
  const status = M.resolveStatus(index.antibodies.AB_10859634, ["IHC"], null, index);
  assert.equal(status.level, "grey");
  assert.equal(status.reason, "app-not-assessed");
  // The record's own verdicts ride along even here, because the card lists every
  // application whatever the page said — the reader has to be able to tell "we
  // tested it and it failed" from "we have not tested this".
  assert.deepEqual(status.passed, ["WB"], "the whole record travels with the status");
  const { passed, failed } = M.recordVerdicts(status.record);
  assert.deepEqual(passed, ["WB"]);
  assert.deepEqual(failed, ["IP", "IF"]);
  assert.equal(M.verdictClause(status.record),
    "supports WB, not supportive for IP, IF");
});

test("the three record shapes each produce a clause", () => {
  const only = (a) => ({ a });
  assert.equal(M.verdictClause(only({ WB: 1, IP: 0, IF: 1, FC: 0 })),
    "not supportive for WB, IF", "failures only — the benchmark's own case");
  assert.equal(M.verdictClause(only({ WB: 2, IP: 0, IF: 0, FC: 0 })),
    "supports WB", "passes only");
  assert.equal(M.verdictClause(only({ WB: 2, IP: 1, IF: 0, FC: 0 })),
    "supports WB, not supportive for IP", "both");
});

test("a record with no verdict anywhere keeps the untested wording", () => {
  // The one case where "No independent data" is the true thing to say.
  assert.equal(M.verdictClause({ a: { WB: 0, IP: 0, IF: 0, FC: 0 } }), "");
  assert.equal(M.verdictClause(null), "");
  assert.equal(M.verdictClause(undefined), "");
});

// --- publisher typesetting -------------------------------------------------
//
// Both of these were verified live in the DOM during the v0.1.6 benchmark, and
// both pages scored zero marks while listing OGA-characterised antibodies.
// They are house styles at two of the largest publishers in the match list,
// so they will be met again on every paper those publishers set.
//
// The bundled index is a TARDBP-only seed, so the Proteintech numbers the
// benchmark actually lost (14060-1-AP, 23274-1-AP, 11306-1-AP) are not in it.
// The house styles are pinned twice over: against the real index using the
// Proteintech numbers the seed does hold, and against a synthetic index below
// using the exact strings read off the two pages.

const SEPARATORS = [
  ["U+0020 space (BMJ)", " "],
  ["U+2009 thin space", " "],
  ["U+00A0 no-break space", " "],
  ["U+202F narrow no-break space", " "],
  ["U+200A hair space", " "],
  ["comma", ","],
];

const DASHES = [
  ["U+002D hyphen", "-"],
  ["U+2010 hyphen", "‐"],
  ["U+2011 non-breaking hyphen", "‑"],
  ["U+2013 en-dash (Springer)", "–"],
  ["U+2014 em-dash", "—"],
  ["U+2212 minus", "−"],
];

for (const [sepName, sep] of SEPARATORS) {
  test(`a thousands separator inside a catalogue number is ignored — ${sepName}`, () => {
    const text = `Western blotting used Beclin1 (1:1000, 10${sep}782-2-AP, Proteintech).`;
    const hit = hitFor(text, "782-2-AP");
    assert.equal(hit.record.n, "10782-2-AP");
    assert.equal(hit.status.level, "green");
    // The mark must cover what the page shows, separator and all.
    assert.equal(text.slice(hit.start, hit.end), `10${sep}782-2-AP`);
  });
}

for (const [dashName, dash] of DASHES) {
  test(`a dash variant in a catalogue number is folded to a hyphen — ${dashName}`, () => {
    const text = `Immunoblotting used Parkin antibody (Proteintech, 10,782${dash}2-AP).`;
    const hit = hitFor(text, "782");
    assert.equal(hit.record.n, "10782-2-AP");
  });
}

test("the Springer line that scored zero marks now resolves", () => {
  // link.springer.com/article/10.1186/s12906-020-02992-7, verbatim shape.
  const text = "Pink1 rabbit polyclonal antibody (Proteintech, 12,892–1-AP, 1:200) and "
    + "Parkin rabbit polyclonal antibody (Proteintech, 10,782–2-AP, 1:200) were used for immunoblotting.";
  const names = M.findMentions(text, index).filter((h) => h.record).map((h) => h.record.n).sort();
  assert.deepEqual(names, ["10782-2-AP", "12892-1-AP"]);
});

test("the BMJ line that scored zero marks now resolves", () => {
  // svn.bmj.com/content/10/1/32, verbatim shape.
  const text = "Beclin1 (1:1000, 10 782-2-AP), PINK1 (1:1000, 12 892-1-AP, Proteintech) "
    + "were detected by western blot.";
  const names = M.findMentions(text, index).filter((h) => h.record).map((h) => h.record.n).sort();
  assert.deepEqual(names, ["10782-2-AP", "12892-1-AP"]);
});

test("zero-width characters anywhere in an identifier are stripped", () => {
  for (const zw of ["​", "‌", "‍", "﻿", "­"]) {
    const text = `Western blot with 10782${zw}-2-AP (Proteintech) antibody.`;
    const hit = hitFor(text, "10782");
    assert.equal(hit.record.n, "10782-2-AP", `zero-width U+${zw.charCodeAt(0).toString(16)}`);
  }
});

// The guards that widening the tokeniser could have quietly disarmed. Each of
// these is a string the new separator rule brings within reach of the index.

test("a dilution is never read as a catalogue number", () => {
  for (const text of [
    "The antibody was used at 1:1 000 dilution for western blot.",
    "Primary antibody, 1:12 500, was applied overnight.",
  ]) {
    assert.deepEqual(M.findMentions(text, index).map((h) => h.matched), [], text);
  }
});

test("a concentration is never read as a catalogue number", () => {
  const text = "A71 antibody was used at 200 ng/ml and 10 782 ng/ml for western blot.";
  assert.ok(!M.findMentions(text, index).some((h) => h.record),
    "a unit after the number means it is a measurement, not an identifier");
});

test("a separated number still has to clear the context gate a bare one does", () => {
  // 89718 is a real catalogue number in the index and a bare number, so it
  // needs antibody vocabulary nearby. Setting it as "89 718" must not let it
  // past that gate just by no longer looking like a bare number.
  assert.deepEqual(
    M.findMentions("A cohort of 89 718 participants was analysed.", index).map((h) => h.matched),
    []);
  const hit = hitFor("Western blot used cat# 89 718 antibody (Abcam).", "89 718");
  assert.ok(hit.record, "with antibody context it is still found");
});

test("a year in prose does not glue itself to the next identifier", () => {
  // Without the group-of-three rule the tokeniser reads "2019 10 782-2-AP" as
  // one lump, which is in no index — widening it would have lost the very
  // identifier it was widened to find.
  const text = "As reported in 2019 10 782-2-AP was used for western blotting (Proteintech antibody).";
  const hit = hitFor(text, "10 782-2-AP");
  assert.equal(hit.record.n, "10782-2-AP");
});

test("a full stop between digits is left alone", () => {
  // Decimals sit exactly where a separator would. Stripping one turns a
  // concentration into a catalogue number.
  assert.equal(M.normaliseIdentifier("1.5"), "1.5");
  assert.equal(M.normaliseIdentifier("10,782–2-AP"), "10782-2-AP");
});

// --- the benchmark's own strings, against a synthetic index -----------------

const synthetic = M.prepareIndex({
  genes: ["PRKN", "SFRP1", "PINK1", "BECN1"],
  aliases: { parkin: "PRKN", "pink-1": "PINK1" },
  antibodies: {
    AB_TEST1: { n: "14060-1-AP", g: "PRKN", s: "Proteintech", a: { WB: 1, IP: 0, IF: 1, FC: 0 } },
    AB_TEST2: { n: "23274-1-AP", g: "PINK1", s: "Proteintech", a: { WB: 1, IP: 0, IF: 0, FC: 0 } },
    AB_TEST3: { n: "11306-1-AP", g: "BECN1", s: "Proteintech", a: { WB: 2, IP: 0, IF: 0, FC: 0 } },
    AB_TEST4: { n: "sc-138763", g: "PRKN", s: "Santa Cruz", a: { WB: 1, IP: 0, IF: 0, FC: 0 } },
    AB_TEST5: { n: "NBP2-23490", g: "PRKN", s: "Novus", a: { WB: 1, IP: 0, IF: 0, FC: 0 } },
    AB_TEST6: { n: "ab15954", g: "PRKN", s: "Abcam", a: { WB: 1, IP: 0, IF: 0, FC: 0 } },
    // The brief's named mixed fixture. It is a real production-index entry —
    // ATP2B1, WB and IP recommended, IF not recommended — but the bundled
    // data/index.json is a TARDBP-only seed, so it is carried here instead.
    AB_TESTPA: { n: "PA1-914", g: "ATP2B1", s: "Thermo Fisher Scientific",
                 a: { WB: 2, IP: 2, IF: 1, FC: 0 } },
  },
  catalogue: {
    "14060-1-ap": "AB_TEST1", "23274-1-ap": "AB_TEST2", "11306-1-ap": "AB_TEST3",
    "sc-138763": "AB_TEST4", "nbp2-23490": "AB_TEST5", "ab15954": "AB_TEST6",
    "pa1-914": "AB_TESTPA",
  },
  clones: {},
});

test("PA1-914 with no stated application is mixed, not green", () => {
  // The brief's named case, end to end through findMentions on the synthetic
  // index: WB and IP recommended, IF not. Green here would tell a reader an
  // antibody with a published IF failure is recommended.
  const hits = M.findMentions("The antibody PA1-914 (Thermo Fisher Scientific) was obtained commercially.", synthetic);
  const hit = hits.find((h) => h.matched === "PA1-914");
  assert.ok(hit, `PA1-914 was not found: ${hits.map((h) => h.matched).join(", ")}`);
  assert.deepEqual(hit.applications, [], "this fixture must exercise the no-application path");
  assert.equal(hit.status.level, "mixed");
  assert.deepEqual(hit.status.passed, ["WB", "IP"]);
  assert.deepEqual(hit.status.failed, ["IF"]);
});

const syntheticName = (text, needle) => {
  const hits = M.findMentions(text, synthetic);
  const hit = hits.find((h) => h.matched.includes(needle));
  assert.ok(hit, `no hit for "${needle}" in: ${hits.map((h) => h.matched).join(", ") || "(none)"}`);
  return hit.record && hit.record.n;
};

test("the exact strings the benchmark read off BMJ and Springer", () => {
  const cases = [
    ["14 060-1-AP",       "14060-1-AP"],   // BMJ, U+0020
    ["14 060-1-AP",  "14060-1-AP"],   // thin space
    ["14,060–1-AP",  "14060-1-AP"],   // Springer, comma + en-dash
    ["23,274–1-AP",  "23274-1-AP"],
    ["11 306-1-AP",       "11306-1-AP"],
    ["sc-138763",         "sc-138763"],    // unchanged
    ["NBP2-23490",        "NBP2-23490"],   // unchanged
    ["ab15954",           "ab15954"],      // unchanged
  ];
  for (const [written, expected] of cases) {
    const text = `Western blotting used antibody (Proteintech, ${written}, 1:1000).`;
    assert.equal(syntheticName(text, written.slice(-6)), expected, written);
  }
});

test("a hyphen the publisher MOVED still reaches the record", () => {
  // normaliseIdentifier handles typesetting a publisher adds. It cannot handle
  // one that moves the number's own punctuation, and that happens too: Thermo's
  // MA5-11154 is printed MA511154, Proteintech's 11820-1-AP as 11820-1AP. Eight
  // benchmark antibodies that ARE in the dataset were reported absent on this.
  for (const [written, expected] of [
    ["14060-1AP", "14060-1-AP"],     // the hyphen the paper dropped
    ["140601AP", "14060-1-AP"],      // all of them dropped
    ["pa1914", "PA1-914"],
  ]) {
    const text = `Western blotting used antibody (Proteintech, ${written}, 1:1000).`;
    assert.equal(syntheticName(text, written), expected, written);
  }
});

test("the collapsed key is the LAST resort, never the first", () => {
  // A stored number that genuinely contains the character still wins on its own
  // exact form, so widening this can only ever ADD a match.
  const text = "Western blotting used antibody (Proteintech, 14060-1-AP, 1:1000).";
  assert.equal(syntheticName(text, "14060-1-AP"), "14060-1-AP");
  // …and a short collapsed key is not offered at all: catalogue numbers are not
  // unique across suppliers, so "2642" must never reach another vendor's row.
  assert.equal(M.collapseIdentifier("2642"), "");
  assert.equal(M.collapseIdentifier("A-5-4"), "");
  assert.equal(M.collapseIdentifier("14060-1-AP"), "140601ap");
});

test("collapseIdentifier agrees with the server's collapse_identifier", () => {
  // One printed string must not resolve here and come back absent from the
  // server. Same rules, same minimum length — see
  // mcp_servers/common/manuscript.py::collapse_identifier.
  assert.equal(M.collapseIdentifier("MA5-11154"), "ma511154");
  assert.equal(M.collapseIdentifier("MA511154"), "ma511154");
  assert.equal(M.collapseIdentifier("11820-1AP"), "118201ap");
  assert.equal(M.collapseIdentifier("11820-1-AP"), "118201ap");
});

test("the strings that must still not match", () => {
  for (const text of [
    "The antibody was used at a 1:1 000 dilution for western blot.",
    "The antibody was used at 200 ng/ml for western blot.",
    "Parkin (2019) reported this previously.",
  ]) {
    assert.deepEqual(M.findMentions(text, synthetic).filter((h) => h.record).map((h) => h.matched),
      [], text);
  }
});

// --- control detection is deliberately absent ------------------------------

test("the matcher exposes no control detection", () => {
  // Removed in 0.1.7, not deprecated: the page-level scan scored kappa 0.11
  // against per-antibody ground truth and put "selectivity control present"
  // under a named antibody on 64% of papers when the true rate was 13%. The
  // analysis moved to the OGA MCP server, where a cue can be linked to the
  // antibody it belongs to. Re-adding it would resurrect the false reassurance.
  assert.equal(M.findControlSignals, undefined);
});

// --- every level this can return has to be paintable -----------------------

test("every level resolveStatus returns has a class in content.js", () => {
  // The pairing that was missing. `content.js::LEVEL_CLASS` is the only thing
  // that paints a mark, so a level absent from it produces
  // `class="oga-hl undefined"` — no colour, no underline, no pattern, while the
  // card behind it stays perfectly correct. Drawn and invisible.
  //
  // `yellow` was in that state from 0.3.1, the release that introduced the
  // middle rung, until 12 Sep 2026. Nothing could catch it: the bundled
  // 18-record fixture carries no qualifier, so no local run produces a yellow
  // mark at all, and the CSS, the card and the popup all had their half.
  //
  // Both sides are derived. The levels come from driving the matcher over
  // shaped records; the table is read out of the source. Asserting a string
  // appears in a file would pass on a `LEVEL_CLASS` that names the level and
  // paints the wrong thing, and would not notice a level added later.
  const src = fs.readFileSync(path.join(root, "src/content.js"), "utf8");
  const m = src.match(/const LEVEL_CLASS = \{([\s\S]*?)\};/);
  assert.ok(m, "LEVEL_CLASS is not where this test looks for it");
  const table = new Function(`return {${m[1]}}`)();

  const rec = (a, q) => ({ n: "X", g: "GENE", s: "S", a, q });
  const ALL = { WB: 0, IP: 0, IF: 0, FC: 0 };
  const levels = new Set([
    // supportive, unsupportive, both, and the middle rung a qualifier earns
    M.resolveStatus(rec({ ...ALL, WB: 2 }), [], null, index).level,
    M.resolveStatus(rec({ ...ALL, WB: 1 }), [], null, index).level,
    M.resolveStatus(rec({ ...ALL, WB: 2, IP: 1 }), [], null, index).level,
    M.resolveStatus(rec({ ...ALL, WB: 1 }, { WB: "ns" }), [], null, index).level,
    // no record: the target is characterised, or nothing is held at all
    M.resolveStatus(null, [], { gene: [...index.geneSet][0] }, index).level,
    M.resolveStatus(null, [], { gene: "NOTAGENE" }, index).level,
  ]);

  assert.ok(levels.has("yellow"),
    "a qualified negative should reach the middle rung; if this changed, the "
    + "list below is no longer testing what it thinks");
  const unpaintable = [...levels].filter((l) => !table[l]);
  assert.deepEqual(unpaintable, [],
    `content.js::LEVEL_CLASS paints no class for: ${unpaintable.join(", ")}`);
});

// --- report ----------------------------------------------------------------

if (failures.length) {
  console.error(`\n${failures.length} failing, ${passed} passing\n`);
  for (const f of failures) console.error("  FAIL " + f + "\n");
  process.exit(1);
}
console.log(`all ${passed} matcher tests passed`);
