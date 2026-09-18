/**
 * One vocabulary for a verdict, across every file that prints one.
 *
 * OGA **characterises** antibodies; it does not recommend or validate them, and
 * a surface that recommends makes a stronger claim than knockout-controlled
 * data supports. `card.js::CODE_LABEL` is the vocabulary — not tested, not
 * supportive, supportive — and the tiles, the chips and the headlines all use
 * it.
 *
 * Four surfaces did not, and were found by unzipping the built artefact rather
 * than by any test here:
 *
 *   - `card.js::greyHeadline`, which had its OWN copy of the failed-side
 *     clause and so kept both the old words and the pre-0.3.3 bug of
 *     contradicting the chip beneath it;
 *   - `card.js`'s out-of-scope paragraph ("tested and not recommended in 2
 *     applications");
 *   - `matcher.js::verdictClause`, which is the mark's screen-reader label, so
 *     the one reader who could not see the chips got the old words;
 *   - the `split` tile's tooltip in `popup.html`.
 *
 * The release that removed the word from the toolbar panel claimed in its own
 * changelog to be removing the last of it. It was not, and nothing could tell:
 * `popup.test.mjs` checks two files, and three of these four are in the other
 * ones. Hence a sweep rather than a third per-file assertion.
 *
 * A NEW EXEMPTION NEEDS A WRITTEN REASON. An empty one fails, the same
 * convention as the Django suite's EXEMPT_MOUNTS — an exemption nobody had to
 * justify is how a list like this stops meaning anything.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.join(here, "..", "src");

/** file -> why a reader-facing "recommend" is correct there. */
const EXEMPT = {
  // The only thing on any of these surfaces that OGA does recommend is a
  // SETTING. "Run on any site — Recommended." is advice about the extension,
  // not a claim about an antibody, and rewording it would lose the one place
  // the word is doing honest work.
  "options.html": "recommends a setting, not an antibody",
};

/** Comment text is not a printed string, and the comments here are the record. */
function printedOnly(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^[ \t]*\/\/.*$/gm, "")
    .replace(/<!--[\s\S]*?-->/g, "");
}

/**
 * `RECOMMENDED` and `NOT_RECOMMENDED` are matcher.js's names for the index's
 * 0/1/2 codes. An internal identifier is not a printed string — but it IS how
 * the printed ones kept the word for so long, so they are removed by an exact
 * uppercase match and nothing looser.
 */
function withoutCodeNames(text) {
  return text.replace(/\b(?:NOT_)?RECOMMENDED\b/g, "«code»");
}

test("no surface recommends an antibody", () => {
  const files = fs.readdirSync(src).filter((f) => /\.(js|html)$/.test(f));
  assert.ok(files.length >= 8, `only ${files.length} source files found`);

  const offences = [];
  for (const file of files) {
    if (EXEMPT[file]) {
      assert.ok(EXEMPT[file].trim(), `${file} is exempt with no reason given`);
      continue;
    }
    const text = withoutCodeNames(printedOnly(
      fs.readFileSync(path.join(src, file), "utf8")));
    text.split("\n").forEach((line, i) => {
      if (/recommend/i.test(line)) {
        offences.push(`${file}:${i + 1} ${line.trim().slice(0, 100)}`);
      }
    });
  }
  assert.deepEqual(offences, [],
    "OGA characterises antibodies and does not recommend them; card.js::"
    + "CODE_LABEL has the words every surface uses:\n  "
    + offences.join("\n  "));
});

test("the words the surfaces do use are the card's own table", () => {
  const card = fs.readFileSync(path.join(src, "card.js"), "utf8");
  const m = card.match(/const CODE_LABEL = \{([^}]*)\}/);
  assert.ok(m, "CODE_LABEL is not where this test looks for it");
  const labels = new Function(`return {${m[1]}}`)();
  assert.deepEqual(labels, { 0: "not tested", 1: "not supportive", 2: "supportive" });

  // Each of the three has to appear on some surface, or the table is a
  // vocabulary nothing speaks.
  const all = ["card.js", "content.js", "matcher.js", "popup.js", "popup.html"]
    .map((f) => printedOnly(fs.readFileSync(path.join(src, f), "utf8")))
    .join("\n");
  for (const word of Object.values(labels)) {
    assert.ok(all.includes(word), `nothing prints "${word}"`);
  }
});

/* ------------------------------------------------- a list, in English */

/**
 * `join(" and ")` is right for two and wrong for everything above it.
 *
 * Six call sites across the card and the mark's label each had their own copy,
 * and two applications is the common case — so it read correctly for months
 * and then printed "This page mentions Western blot and Immunoprecipitation and
 * Immunofluorescence and IHC" on the first page that named four. Found by the
 * owner on a real card, 12 Sep 2026.
 *
 * `matcher.js::listOf` is the one joiner, for the reason `verdictClause` lives
 * there: the card and the screen-reader label both need it, and fixing the
 * wording on one surface and not the other is how a reader gets two different
 * answers about one row.
 */
test("a list of three or more is not joined with 'and' throughout", () => {
  const src = fs.readFileSync(path.join(here, "..", "src/matcher.js"), "utf8");
  globalThis.window = globalThis;
  new Function(src)();
  const M = globalThis.OGAMatcher;

  assert.equal(M.listOf([]), "");
  assert.equal(M.listOf(["WB"]), "WB");
  assert.equal(M.listOf(["WB", "IP"]), "WB and IP");
  assert.equal(M.listOf(["WB", "IP", "IF"]), "WB, IP and IF");
  assert.equal(M.listOf(["WB", "IP", "IF", "IHC"]), "WB, IP, IF and IHC");
  // No serial comma: house style everywhere else in this copy.
  assert.ok(!M.listOf(["a", "b", "c"]).includes(", and"));
});

test("no surface joins a list with 'and' on its own", () => {
  // The sweep, not the unit test, is what would have caught it: the bug was
  // six copies of one expression, and a unit test proves the replacement works
  // without proving anybody calls it.
  const offences = [];
  for (const file of ["card.js", "content.js", "matcher.js", "popup.js"]) {
    const text = printedOnly(
      fs.readFileSync(path.join(src, file), "utf8"));
    text.split("\n").forEach((line, i) => {
      if (/join\(" and "\)/.test(line)) {
        offences.push(`${file}:${i + 1} ${line.trim().slice(0, 90)}`);
      }
    });
  }
  assert.deepEqual(offences, [],
    "join(\" and \") reads as \"a and b and c\" above two items; "
    + "matcher.js::listOf is the joiner:\n  " + offences.join("\n  "));
});
