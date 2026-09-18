/**
 * What the card draws, given a status — the figure above all.
 *
 * A knockout figure is the most persuasive thing on this card, and which one is
 * drawn is decided by `defaultApp`, whose job is to find *a* figure rather than
 * the *right* figure. That is correct when the reader has no application in
 * mind and wrong the moment they do: on a card headed "Not tested for FC" it
 * fell through to the IP tab and filled the panel with a knockout
 * immunoprecipitation. The headline was right, the caption was right, and the
 * picture answered a question nobody had asked.
 *
 * That failure is invisible to every other suite here. The card renders, the
 * tabs work, the verdicts are correct, and nothing in the DOM says the figure
 * belongs to the wrong assay — it is only wrong relative to what the PAPER did,
 * which lives in the status object. So it is pinned here, cheaply: jsdom, the
 * bundled dev index, and the real render path.
 *
 * The per-application caveat is here for a related reason. It arrives from
 * index.json (`application_scope`), so if the builder stops sending it the card
 * goes on rendering perfectly with the sentence simply absent — the same silent
 * omission as the caveat under the verdict, one level down.
 *
 * Deliberately not a browser test: nothing here crosses the worker boundary or
 * depends on a click. `tabs.mjs` already drives the wiring in Chromium; this is
 * the render, and a browser for it would cost ~0.9s to learn nothing more.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { JSDOM } from "jsdom";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const extRoot = path.join(here, "..");

function load() {
  const dom = new JSDOM("<!doctype html><body></body>", { url: "https://example.org/" });
  const root = { window: dom.window, document: dom.window.document };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  for (const f of ["src/matcher.js", "src/handoff.js", "src/card.js"]) {
    new Function("globalThis", "self", fs.readFileSync(path.join(extRoot, f), "utf8"))(
      globalThis, globalThis);
  }
  const raw = JSON.parse(fs.readFileSync(path.join(extRoot, "data/index.json"), "utf8"));
  const index = globalThis.OGAMatcher.prepareIndex(raw);
  globalThis.OGACard.setScope(index);
  return { index, doc: dom.window.document, root };
}

/** Render one status and hand back the parts that matter. */
function draw(index, doc, record, status) {
  const anchor = doc.body.appendChild(doc.createElement("span"));
  globalThis.OGACard.show(anchor, { status: { ...status, record }, matched: record.n, record });
  const card = doc.querySelector(".oga-card-host").shadowRoot.querySelector(".card");
  const selected = card.querySelector(".chip-sel");
  return {
    card,
    openTab: selected ? selected.querySelector("b").textContent.trim() : null,
    figure: card.querySelector(".fig figcaption")
      ? card.querySelector(".fig figcaption").textContent.trim() : null,
    nofig: card.querySelector(".nofig") ? card.querySelector(".nofig").textContent : null,
    appScope: card.querySelector(".appscope")
      ? card.querySelector(".appscope").textContent.trim() : null,
    scope: card.querySelector(".scope") ? card.querySelector(".scope").textContent.trim() : null,
    verdict: card.querySelector(".verdict")
      ? card.querySelector(".verdict").textContent.trim() : null,
  };
}

/** An antibody in the dev index with figures for more than one application. */
function multiFigureRecord(index) {
  const rec = Object.values(index.antibodies)
    .find((r) => r.img && Object.keys(r.img).length > 1);
  assert.ok(rec, "the dev index has no antibody with two figures to confuse");
  return rec;
}

function baseStatus(record) {
  const { passed, failed } = globalThis.OGAMatcher.recordVerdicts(record);
  // `qualifiers` is what `resolveStatus` copies off `record.q`, and leaving it
  // out here made every hand-built status in this file unqualified — so the
  // middle rung could not be reached by any test even once a record carried one.
  return { passed, failed, untested: [], uncertain: [], mentioned: [], unassessed: [],
           untestedApplications: [], qualifiers: record.q || {}, gene: record.g };
}

/**
 * A record carrying a qualified negative — the middle rung.
 *
 * Built here rather than taken from `data/index.json`, which is a dev fixture
 * rebuilt from live data at download time and must not be hand-edited. It also
 * carries NO `q` key on any of its 18 records, which is exactly why the
 * headline/chip contradiction below survived three suites: nothing in this
 * repository had ever rendered a qualified negative.
 *
 * `sd` is "detects the target" — a negative the data still says something
 * on-target about, which is what the yellow rung means.
 */
function withQualifiedNegative(index, verdicts, qualifiers) {
  const key = Object.keys(index.antibodies)[0];
  const rec = { ...index.antibodies[key], a: verdicts, q: qualifiers };
  return rec;
}

/** Every application's rung, as the chips print it. */
function chipRungs(card) {
  return Object.fromEntries([...card.querySelectorAll(".chip")].map(
    (c) => [c.querySelector("b").textContent, c.querySelector("i").textContent]));
}

test("a mixed headline names the middle rung the chips name", () => {
  // Live on ab237703, 3 Sep 2026: headline "Supports IP, IF — not supportive
  // for WB" over a WB chip reading "limited support". `mixed` never asked
  // whether a failure was qualified, so it flattened the rung to the harder
  // word and contradicted the row directly beneath it.
  const { index, doc } = load();
  const rec = withQualifiedNegative(index, { WB: 1, IP: 2, IF: 2, FC: 0 }, { WB: "sd" });
  const out = draw(index, doc, rec, { ...baseStatus(rec), level: "mixed", reason: "no-application" });

  assert.equal(chipRungs(out.card).WB, "limited support",
    "the chip should carry the middle rung");
  assert.match(out.verdict, /limited support for WB/,
    `the headline flattened the middle rung: ${out.verdict}`);
  assert.doesNotMatch(out.verdict, /not supportive for WB/,
    `the headline contradicts its own WB chip: ${out.verdict}`);
});

test("a red headline separates what showed nothing from what did not", () => {
  // Live on ab104859 the same day, and the harder half: WB is limited support
  // while IP and IF genuinely showed nothing, so the headline has to name both
  // rungs rather than collapse three applications into one verdict.
  const { index, doc } = load();
  const rec = withQualifiedNegative(index, { WB: 1, IP: 1, IF: 1, FC: 0 }, { WB: "sd" });
  const out = draw(index, doc, rec, { ...baseStatus(rec), level: "red", reason: "no-application" });

  assert.match(out.verdict, /limited support for WB/, out.verdict);
  assert.match(out.verdict, /not supportive for IP, IF/, out.verdict);
  // "Tested; data limited support for WB" is not English -- `data` belongs to
  // the segment it leads, not to the sentence.
  assert.doesNotMatch(out.verdict, /data limited support/, out.verdict);
});

test("an unqualified record's headline is unchanged", () => {
  // The fix must be invisible where there is no qualifier, which is most of the
  // dataset. Both wordings are the originals.
  const { index, doc } = load();
  const red = withQualifiedNegative(index, { WB: 1, IP: 1, IF: 0, FC: 0 }, {});
  assert.match(
    draw(index, doc, red, { ...baseStatus(red), level: "red", reason: "no-application" }).verdict,
    /^Tested; data not supportive for WB, IP\b/);

  const mixed = withQualifiedNegative(index, { WB: 1, IP: 2, IF: 0, FC: 0 }, {});
  assert.match(
    draw(index, doc, mixed, { ...baseStatus(mixed), level: "mixed", reason: "no-application" }).verdict,
    /^Supports IP — not supportive for WB\b/);
});

test("a supportive verdict's reinforcing qualifier is not a limited rung", () => {
  // `sl` and `xs` reinforce rather than temper (recommendations.py's `tempers`),
  // so they must never turn a headline or a chip into "limited support" -- the
  // defect that put an amber edge on the best result the dataset records.
  const { index, doc } = load();
  const rec = withQualifiedNegative(index, { WB: 2, IP: 1, IF: 0, FC: 0 }, { WB: "xs", IP: "ns" });
  const out = draw(index, doc, rec, { ...baseStatus(rec), level: "mixed", reason: "no-application" });

  assert.equal(chipRungs(out.card).WB, "supportive");
  assert.match(out.verdict, /^Supports WB — limited support for IP\b/, out.verdict);
});

test("a paper's application that OGA does not assess draws no figure at all", () => {
  const { index, doc } = load();
  const rec = multiFigureRecord(index);
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "grey", reason: "app-not-assessed",
    unassessed: ["IHC"], untestedApplications: ["IHC"],
  });
  assert.equal(out.figure, null,
    "a knockout figure for another assay was drawn on an IHC paper");
  assert.equal(out.openTab, null,
    "a tab was preselected for an application the paper did not use");
});

test("a paper's application with no result opens that application's own tab", () => {
  const { index, doc } = load();
  const rec = Object.values(index.antibodies).find(
    (r) => r.img && Object.keys(r.img).length > 1 && (r.a.FC ?? 0) === 0);
  assert.ok(rec, "the dev index has no antibody untested for FC with other figures");
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "grey", reason: "app-not-tested", mentioned: ["FC"],
  });
  assert.equal(out.openTab, "FC");
  assert.equal(out.figure, null, "another assay's figure was drawn");
  assert.match(out.nofig, /No published Flow cytometry figure/);
  assert.match(out.nofig, /has not tested it in this application/);
});

test("with no application named, a figure is still offered", () => {
  // The fall-through is right when the reader has no application in mind, and
  // removing it would cost every reader the evidence. Pinned so the fix above
  // cannot quietly widen into "never show a figure".
  const { index, doc } = load();
  const rec = multiFigureRecord(index);
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "mixed", reason: "no-application",
  });
  assert.ok(out.figure, "no figure offered on a card with nothing else to go on");
});

test("the immunofluorescence panel says results depend on fixation", () => {
  const { index, doc } = load();
  const rec = Object.values(index.antibodies).find((r) => r.img && r.img.IF);
  assert.ok(rec, "the dev index has no antibody with an IF figure");
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "green", reason: "paper-application",
    mentioned: ["IF"], scopedTo: ["IF"],
  });
  assert.equal(out.figure, "Immunofluorescence — genetic control vs wild-type");
  assert.match(out.appScope, /fixation and permeabilisation dependent/);
});

test("an application with nothing specific to say carries no note", () => {
  const { index, doc } = load();
  const rec = Object.values(index.antibodies).find((r) => r.img && r.img.WB);
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "green", reason: "paper-application",
    mentioned: ["WB"], scopedTo: ["WB"],
  });
  assert.equal(out.appScope, null, "a caveat-shaped blank was drawn under WB");
});

test("the verdict carries the dataset caveat, from the index", () => {
  const { index, doc } = load();
  const rec = multiFigureRecord(index);
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "mixed", reason: "no-application",
  });
  // Just the sentence since 3 Sep 2026. "Read the protocols" moved up into the
  // verdict, where `withConditions` links the qualifier itself — see the next
  // test. Two links to one URL a line apart is clutter, and the reader who
  // needs it is the one reading the strong sentence, not the caveat.
  assert.equal(out.scope, index.scopeShort);
});

test("every verdict binds itself to the conditions that produced it", () => {
  // The scope line underneath is not enough on its own: 0.2.5 shipped
  // "Tested and not recommended for WB" as a standalone sentence, and a reader
  // who takes only the headline takes an unqualified verdict. Losing the
  // qualifier again would render perfectly and say something stronger than we
  // mean, on a card beside a named commercial product.
  const { index, doc } = load();
  const V = globalThis.OGAMatcher.recordVerdicts;
  const all = Object.values(index.antibodies);
  const cases = {
    green: all.find((r) => V(r).passed.length && !V(r).failed.length),
    red: all.find((r) => V(r).failed.length && !V(r).passed.length),
    mixed: all.find((r) => V(r).passed.length && V(r).failed.length),
  };
  for (const [level, rec] of Object.entries(cases)) {
    assert.ok(rec, `the dev index has no ${level} record`);
    const out = draw(index, doc, rec, {
      ...baseStatus(rec), level, reason: "no-application",
    });
    // Against the SERVED wording, not a phrase spelled here. The whole point of
    // shipping it in the index is that the owner can reword it without a store
    // submission, and a test that hard-codes the words fails the next time she
    // does — which trains somebody to edit the assertion rather than read it.
    const verdict = out.card.querySelector(".verdict").textContent;
    assert.ok(verdict.endsWith(index.conditionsQualifier),
      `${level} verdict is unqualified: ${verdict}`);

    // And the qualifier is the way to check it. The link reaches every verdict;
    // the scope line it replaced was drawn on three levels out of six.
    const link = out.card.querySelector(".verdict a");
    assert.ok(link, `${level} verdict does not link its protocols`);
    assert.equal(link.getAttribute("href"), index.protocolsUrl);
    assert.equal(link.textContent, index.conditionsQualifier);
  }
});

test('"not tested" is not qualified, because it is not a result', () => {
  // An application nobody ran did not come out any way under any conditions.
  // Qualifying it would imply a result exists.
  const { index, doc } = load();
  const rec = Object.values(index.antibodies).find(
    (r) => (r.a.FC ?? 0) === 0 && globalThis.OGAMatcher.recordVerdicts(r).passed.length);
  assert.ok(rec, "no record untested for FC with a verdict elsewhere");
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "grey", reason: "app-not-tested", mentioned: ["FC"],
  });
  const verdict = out.card.querySelector(".verdict").textContent;
  assert.match(verdict, /^Not tested for FC —/,
    "the lead should name the untested application plainly");
  assert.ok(!verdict.startsWith(`Not tested for FC ${index.conditionsQualifier}`),
    "an application nobody ran was described as a result under conditions");
});


/* The headline may not name an application the verdict was narrowed away from.
 *
 * The colour is scoped when the citation layer says which application the paper
 * used; the words were not. So a card narrowed to western blot printed
 * "Limited support for WB, IP, IF" over chips reading IP not supportive and IF
 * not supportive — claiming limited support for two applications that have
 * none. Seen on a real ab104859 card, 29 Aug 2026, in a screenshot taken for
 * the store listing.
 *
 * Yellow is where it shows, because `failed` can hold applications the
 * narrowing excluded. The same mismatch is latent in green, red and mixed, so
 * they are pinned too.
 */
test("a scoped headline names only what the lookup narrowed to", () => {
  const { index, doc } = load();
  const rec = Object.values(index.antibodies).find(
    (r) => (r.a.WB ?? 0) === 1 && (r.a.IP ?? 0) === 1 && (r.a.IF ?? 0) === 1);
  assert.ok(rec, "the dev index has no antibody failing WB, IP and IF");
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "yellow", reason: "paper-application",
    mentioned: ["WB"], scopedTo: ["WB"], qualifiers: { WB: "sd" },
  });
  assert.match(out.verdict, /Limited support for WB/);
  assert.doesNotMatch(out.verdict, /IP/,
    "the headline named an application the verdict was narrowed away from");
  assert.doesNotMatch(out.verdict, /IF/);
});

test("an unscoped headline still states the whole record", () => {
  // Proximity never sets `scopedTo`, and must not be able to shrink the
  // headline: that guess was wrong on 17 of 54 papers.
  const { index, doc } = load();
  const rec = Object.values(index.antibodies).find(
    (r) => (r.a.WB ?? 0) === 1 && (r.a.IP ?? 0) === 1);
  const out = draw(index, doc, rec, {
    ...baseStatus(rec), level: "red", reason: "mentioned", mentioned: ["WB"],
  });
  assert.match(out.verdict, /WB/);
  assert.match(out.verdict, /IP/,
    "a proximity guess shrank the headline to its own application");
});
