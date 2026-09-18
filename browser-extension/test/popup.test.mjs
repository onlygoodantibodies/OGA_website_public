/**
 * The toolbar panel's "Worth a look" list, and the words it uses.
 *
 * THE PANEL AND THE CARD MUST NOT DESCRIBE ONE RESULT TWO WAYS. This list said
 * "not recommended" while the hover card said "not supportive" and the tiles
 * four centimetres above it in the same panel said NOT SUPPORTIVE. Every page
 * on the site had moved off the word — OGA characterises antibodies, it does
 * not recommend them, and a panel that recommends makes a stronger claim than
 * the data does. It survived because nothing contradicted it except everything
 * around it, which is not a thing a test catches by accident.
 *
 * Two more shapes are pinned here because each states something false in a
 * different direction:
 *
 *   - a SPLIT verdict printed only its failures, turning "supports IP, not
 *     supportive for WB" into a flat negative;
 *   - a DECLARED-TARGET notice is level `red` and carries no failed
 *     applications, so it came out as a bare "not recommended" — a performance
 *     claim about a product OGA has never tested, in the one place on this
 *     panel that makes a claim in words rather than a count.
 *
 * Node plus jsdom: `concernLine` is a pure function and `renderConcerns` needs
 * one element. `focus.mjs` already drives the panel's tiles in Chromium.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..");

const popupSrc = fs.readFileSync(path.join(root, "src/popup.js"), "utf8");
const cardSrc = fs.readFileSync(path.join(root, "src/card.js"), "utf8");
const popupHtml = fs.readFileSync(path.join(root, "src/popup.html"), "utf8");

// The real panel markup, so the script finds every element it wires, and so
// the tile labels this file asserts against are the shipped ones.
const dom = new JSDOM(popupHtml, { runScripts: "outside-only" });
for (const key of ["window", "document"]) {
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
}
// Enough of the extension API for the script to load. It asks the active tab
// for a summary at boot; answering nothing is the "not running here" path and
// is exactly what this file wants, since it drives the renderers directly.
globalThis.chrome = {
  runtime: { lastError: null, getURL: (p) => p },
  tabs: { query: () => {}, sendMessage: () => {} },
  storage: { local: { get: () => {}, set: () => {} } },
};
// popup.js is a classic script: loaded in the browser its top-level functions
// ARE globals, but `new Function` gives them a function scope instead, so they
// are handed out explicitly rather than fished off globalThis. No test hook in
// the shipped file.
const { concernLine, renderConcerns, renderNotices, tileName } = new Function(
  popupSrc + "\nreturn { concernLine, renderConcerns, renderNotices, tileName };")();

/** popup.js with its comments removed.
 *
 * The vocabulary check below is about what the panel SAYS, and the code carries
 * a comment explaining which word it used to say — which is worth keeping and
 * is not a string any reader sees. Stripping is the difference between testing
 * the output and testing the file. */
const popupCode = popupSrc
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");

/* ------------------------------------------------------ one vocabulary */

test("the panel uses the card's words, not its own", () => {
  // The card's table is the source: CODE_LABEL = {0: not tested, 1: not
  // supportive, 2: supportive}. Read out of card.js rather than retyped, so
  // this fails if either side moves.
  const m = cardSrc.match(/const CODE_LABEL = \{([^}]*)\}/);
  assert.ok(m, "CODE_LABEL is not where this test looks for it");
  const labels = new Function(`return {${m[1]}}`)();
  assert.equal(labels[1], "not supportive");

  assert.ok(popupCode.includes(labels[1]),
    `the panel does not use the card's word for a negative (${labels[1]})`);
  assert.ok(!/not recommended/.test(popupCode),
    "the panel says 'not recommended'; OGA characterises antibodies and does "
    + "not recommend them, and the card and the tiles both say 'not supportive'");
  assert.ok(!/not recommended/.test(popupHtml),
    "a tile's tooltip says 'not recommended'");
});

/* ------------------------------------------------ what each line may claim */

test("a negative names the applications it failed", () => {
  const line = concernLine({ name: "MA1-510", gene: "NR3C1",
                             failed: ["WB", "FC"], passed: [] });
  assert.match(line, /MA1-510/);
  assert.match(line, /\(NR3C1\)/);
  assert.match(line, /not supportive for WB, FC/);
});

test("a split verdict states both halves", () => {
  // The card's headline for this is "Supports IP — not supportive for WB".
  // Printing only the failures reports a flat negative about an antibody the
  // data supports for something.
  const line = concernLine({ name: "ab109535", gene: "TARDBP",
                             failed: ["WB"], passed: ["IP", "IF"] });
  assert.match(line, /supports IP, IF/);
  assert.match(line, /not supportive for WB/);
});

test("a wrong-target entry makes no performance claim at all", () => {
  const line = concernLine({
    name: "ab9361", kind: "wrong-target", gene: null, failed: [], passed: [],
    declared: "lacZ (E. coli beta-galactosidase)",
    mistakenFor: "mammalian beta-galactosidase",
  });
  assert.match(line, /an antibody to lacZ \(E\. coli beta-galactosidase\)/);
  assert.match(line, /not to mammalian beta-galactosidase/);
  // OGA has characterised none of the eleven, so the panel must not imply one.
  assert.ok(!/not supportive/.test(line), line);
  assert.ok(!/not recommended/.test(line), line);
  assert.match(line, /Not an OGA result/);
});

test("the empty state matches the words the list would have used", () => {
  renderConcerns([]);
  const text = dom.window.document.getElementById("concerns").textContent;
  assert.match(text, /not supportive/);
  assert.ok(!/not recommended/.test(text), text);
});

/* ------------------------------------- the hint the tiles write, per level */

test("every tile names itself, and names what it says on its face", () => {
  // There was a second table for this and it renamed two verdicts ("Recommended",
  // "Not recommended") and had no entry for `yellow` at all, so clicking the
  // limited-support tile wrote `undefined` into the hint. Reading the label off
  // the button means one string per level and no list to forget.
  const buttons = [...dom.window.document.querySelectorAll(".t[data-level]")];
  assert.ok(buttons.length >= 6, `only ${buttons.length} tiles found`);
  const seen = new Set();
  for (const b of buttons) {
    const level = b.dataset.level;
    seen.add(level);
    const name = tileName(b, level);
    assert.ok(name && name !== "undefined", `${level} has no name`);
    assert.notEqual(name.toLowerCase(), level,
      `${level} fell back to its own level name, so its tile has no label`);
    assert.ok(!/recommend/i.test(name), `${level} tile says "${name}"`);
  }
  // The rung that was missing, and the four either side of it.
  for (const level of ["green", "yellow", "red", "mixed", "blue", "grey"]) {
    assert.ok(seen.has(level), `no tile for ${level}`);
  }
});

test("a reagent with no gene still reads as a sentence", () => {
  const line = concernLine({ name: "GTX630196", gene: null,
                             failed: ["WB"], passed: [] });
  assert.match(line, /GTX630196<\/strong> — not supportive for WB/);
});

/* ------------------------------------ what the counts above the list count */

test("a red count says when part of it is not a result", () => {
  // The red tile's face reads NOT SUPPORTIVE, and a declared-target notice is
  // level red. So a paper citing ab9361 and nothing else drew `1 NOT
  // SUPPORTIVE` on a page where OGA has tested nothing (oncotarget 17778,
  // 12 Sep 2026). One red colour is the owner's decision and it holds; a count
  // labelled with only one of red's two meanings is a claim about the page.
  renderNotices(1);
  const el = dom.window.document.getElementById("notices");
  assert.equal(el.hidden, false);
  assert.match(el.textContent, /declared-target notice/);
  assert.match(el.textContent, /[Nn]ot an OGA test result/);
  assert.ok(!/\bis a declared-target notice\b.*marks are/.test(el.textContent));

  renderNotices(2);
  assert.match(el.textContent, /2 of the red marks are/);
});

test("no notices draws no line at all", () => {
  // Zero on almost every page, and a blank line under the tally reads as a
  // caveat that failed to load.
  renderNotices(0);
  const el = dom.window.document.getElementById("notices");
  assert.equal(el.hidden, true);
  assert.equal(el.textContent, "");
});

test("the panel says the tiles are marks and the list is reagents", () => {
  // 7 NOT SUPPORTIVE over two lines is two right answers to two questions, and
  // a reader cannot tell which is which without being told. Seen on a supplier
  // catalogue page, where one product fills several rows.
  assert.match(popupHtml, /Click a count to jump through those marks/);
  assert.match(popupHtml, /One line per reagent/);
});
