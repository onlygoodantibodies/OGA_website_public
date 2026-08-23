/**
 * End-to-end check: load the real extension into Chromium, open a methods
 * section, and assert on what the reader actually sees.
 *
 *   node browser-extension/test/e2e.mjs [--screenshot out.png]
 *
 * The only thing altered for the test is the content-script match list, so the
 * fixture can be served from localhost. All extension code under test is the
 * shipped code.
 */
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here = path.dirname(fileURLToPath(import.meta.url));
const extRoot = path.join(here, "..");

const screenshotArg = process.argv.indexOf("--screenshot");
const screenshotPath = screenshotArg !== -1 ? process.argv[screenshotArg + 1] : null;

/* --- serve the fixture ---------------------------------------------------- */

const fixture = fs.readFileSync(path.join(here, "fixtures/paper.html"));
const server = http.createServer((_req, res) => {
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(fixture);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const port = server.address().port;
const url = `http://127.0.0.1:${port}/paper.html`;

/* --- stage a copy of the extension that also matches localhost ------------ */

const stage = fs.mkdtempSync(path.join(os.tmpdir(), "oga-ext-"));
fs.cpSync(extRoot, stage, {
  recursive: true,
  filter: (src) => !src.includes("node_modules") && !src.endsWith(".pyc"),
});
const manifest = JSON.parse(fs.readFileSync(path.join(stage, "manifest.json"), "utf8"));
manifest.content_scripts[0].matches.push("http://127.0.0.1/*");
fs.writeFileSync(path.join(stage, "manifest.json"), JSON.stringify(manifest, null, 2));

/* --- launch --------------------------------------------------------------- */

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "oga-profile-"));

// MV3 extensions do not load in the headless shell, so drive a full Chromium.
// Under CI/containers run this via `xvfb-run -a node ...`.
const launchOptions = {
  headless: false,
  args: [
    `--disable-extensions-except=${stage}`,
    `--load-extension=${stage}`,
    "--no-sandbox",
  ],
};
if (process.env.CHROMIUM_PATH) launchOptions.executablePath = process.env.CHROMIUM_PATH;

const context = await chromium.launchPersistentContext(userDataDir, launchOptions);

const failures = [];
let passed = 0;
function check(name, fn) {
  try { fn(); passed++; } catch (err) { failures.push(`${name}\n    ${err.message}`); }
}

try {
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e)));

  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("mark.oga-hl", { timeout: 15000 });
  // Let the idle-callback scan settle.
  await page.waitForTimeout(800);

  const marks = await page.$$eval("mark.oga-hl", (els) =>
    els.map((el) => ({
      text: el.textContent.trim(),
      cls: [...el.classList].find((c) => c.startsWith("oga-") && c !== "oga-hl"),
      inside: el.closest("p") ? el.closest("p").id : null,
      label: el.getAttribute("aria-label"),
    })));

  const find = (text, inside) =>
    marks.find((m) => m.text.includes(text) && (!inside || m.inside === inside));

  // The colour is the antibody's whole record, not the slice matching whatever
  // application the page seemed to name — the 2 August re-run found the page's
  // application was wrong on 17 of the 54 papers it named one for. What the page
  // was seen to say is carried in the label instead, and asserted there.
  check("the section's application is reported without narrowing the verdict", () => {
    const m = find("ab109535", "p-wb") || find("AB_10859634", "p-wb");
    assert.ok(m, "no highlight on the WB mention");
    assert.equal(m.cls, "oga-mixed", "ab109535 fails IP and IF and must not be green");
    assert.match(m.label, /mentions Western blot/i, m.label);
  });

  check("the same antibody in the IF section reads the same, and says so", () => {
    const m = find("ab109535", "p-if");
    assert.ok(m, "no highlight on the IF mention");
    assert.equal(m.cls, "oga-mixed", "one reagent, one record, one colour");
    assert.match(m.label, /mentions immunofluorescence/i, m.label);
  });

  check("antibody that failed everything renders red", () => {
    const m = find("GTX630196");
    assert.ok(m);
    assert.equal(m.cls, "oga-red");
  });

  check("an antibody that passes some and fails others is never green", () => {
    const m = find("MAB7778", "p-ip");
    assert.ok(m);
    assert.equal(m.cls, "oga-mixed");     // MAB7778 fails WB, passes IP and IF
    assert.match(m.label, /mentions immunoprecipitation/i, m.label);
  });

  check("IHC gets no verdict carried over from other applications", () => {
    const m = find("ab254166", "p-ihc");
    assert.ok(m, "no highlight in the IHC section");
    assert.equal(m.cls, "oga-grey");
  });

  check("untested antibody against a characterised target renders amber", () => {
    const amber = marks.filter((m) => m.cls === "oga-amber" && m.inside === "p-if");
    assert.ok(amber.length >= 1, `expected amber in the IF section, saw ${JSON.stringify(marks.filter(m => m.inside === "p-if"))}`);
  });

  check("target absent from the dataset stays grey", () => {
    const hits = marks.filter((m) => m.inside === "p-other");
    assert.ok(!hits.some((m) => ["oga-green", "oga-red", "oga-amber"].includes(m.cls)),
      `Flotillin-1 should not be coloured: ${JSON.stringify(hits)}`);
  });

  check("an identifier broken up by inline markup is still matched", () => {
    // Publishers split catalogue numbers with <span>/<a> constantly. A mention
    // straddling two text nodes used to be skipped entirely, so the antibody
    // looked absent from the dataset.
    const parts = marks.filter((m) => m.inside === "p-split");
    assert.ok(parts.length >= 1, "split catalogue number was not matched at all");
    assert.ok(parts.every((m) => m.cls === "oga-green"),
      `expected green, got ${JSON.stringify(parts)}`);
    assert.equal(parts.map((m) => m.text).join(""), "10782-2-AP",
      "the marked pieces should reconstruct the whole identifier");
  });

  check("a bare number in ordinary prose is not highlighted", () => {
    const hits = marks.filter((m) => m.inside === "p-prose");
    assert.equal(hits.length, 0, `unexpected highlights: ${JSON.stringify(hits)}`);
  });

  check("a reagent named twice is marked once", () => {
    const wbAmber = marks.filter((m) => m.inside === "p-wb" && m.cls === "oga-amber");
    assert.equal(wbAmber.length, 0,
      `"anti-TDP-43 antibody (Abcam ab109535)" is one reagent: ${JSON.stringify(wbAmber)}`);
  });

  check("highlights carry an accessible label", () => {
    const m = find("ab109535", "p-wb") || find("AB_10859634", "p-wb");
    assert.ok(m.label && m.label.length > 10, "missing aria-label");
  });

  // --- stability across a DOM change ---------------------------------------
  // Publisher pages mutate after load. Our own <mark> insertions must not be
  // read back as new content, and a genuine change must be picked up cleanly.

  const before = marks.length;
  await page.evaluate(() => {
    const p = document.createElement("p");
    p.id = "p-late";
    p.textContent = "Western blotting was also performed with 80002-1-RR (Proteintech).";
    document.body.appendChild(p);
  });
  await page.waitForTimeout(1600);

  const after = await page.$$eval("mark.oga-hl", (els) =>
    els.map((el) => ({
      text: el.textContent.trim(),
      cls: [...el.classList].find((c) => c.startsWith("oga-") && c !== "oga-hl"),
      inside: el.closest("p") ? el.closest("p").id : null,
    })));

  check("late-injected content is scanned", () => {
    const late = after.find((m) => m.inside === "p-late");
    assert.ok(late, "content added after load was not scanned");
    assert.equal(late.cls, "oga-green");
  });

  check("existing marks are neither duplicated nor re-coloured", () => {
    const existing = after.filter((m) => m.inside !== "p-late");
    assert.equal(existing.length, before, "mark count changed for unmodified content");
    for (const m of existing) {
      const orig = marks.find((o) => o.inside === m.inside && o.text === m.text);
      assert.ok(orig, `unexpected new mark: ${JSON.stringify(m)}`);
      assert.equal(m.cls, orig.cls, `colour changed on rescan for "${m.text}"`);
    }
  });

  // --- hover card -----------------------------------------------------------

  const wbMark = page.locator("#p-wb mark.oga-hl", { hasText: "ab109535" }).first();
  await wbMark.hover();
  await page.waitForTimeout(400);

  const card = await page.evaluate(() => {
    const host = document.querySelector(".oga-card-host");
    if (!host || !host.shadowRoot) return null;
    const el = host.shadowRoot.querySelector(".card");
    if (!el || el.style.display === "none") return null;
    return {
      text: el.textContent.replace(/\s+/g, " ").trim(),
      chips: [...el.querySelectorAll(".chip")].map((c) => c.textContent.replace(/\s+/g, " ").trim()),
      hasImage: Boolean(el.querySelector("figure img")),
      links: [...el.querySelectorAll(".links a")].map((a) => a.getAttribute("href")),
    };
  });

  check("hovering opens a card", () => assert.ok(card, "no card appeared on hover"));

  check("card gives the per-application breakdown", () => {
    assert.ok(card.chips.length === 4, `expected 4 application chips, got ${card.chips.length}`);
    assert.ok(card.chips.some((c) => /WB.*recommended/.test(c)));
    assert.ok(card.chips.some((c) => /IP.*not recommended/.test(c)));
    assert.ok(card.chips.some((c) => /FC.*not tested/.test(c)));
  });

  check("card states the provenance of the data", () => {
    assert.match(card.text, /knockout controls/i);
  });

  check("card links out to the gene page and the report", () => {
    assert.ok(card.links.some((h) => h.includes("/antibodies/TARDBP/")), JSON.stringify(card.links));
    assert.ok(card.links.some((h) => h.includes("doi.org")), JSON.stringify(card.links));
  });

  check("card offers the validation image", () => {
    assert.ok(card.hasImage, "expected a validation figure in the card");
  });

  // --- the amber card shows its working -------------------------------------
  // Amber is the only verdict inferred rather than looked up, and it was 62 of
  // the 239 marks the benchmark drew. The card has to say the target was read
  // off the page, quote the words it read, and still keep the dataset caveat.
  //
  // Where it says the target was read has moved. The headline now states what is
  // AVAILABLE — the reason the mark exists at all — and the provenance sits in
  // the body, which is where "amber card quotes the text the target came from"
  // has always checked for it. Nothing was dropped; the two claims swapped
  // places, so the two checks below split accordingly.

  await page.locator("#p-if mark.oga-amber").first().hover();
  await page.waitForTimeout(400);

  const amberCard = await page.evaluate(() => {
    const host = document.querySelector(".oga-card-host");
    const el = host && host.shadowRoot && host.shadowRoot.querySelector(".card");
    if (!el || el.style.display === "none") return null;
    return {
      text: el.textContent.replace(/\s+/g, " ").trim(),
      html: el.innerHTML,
      verdict: (el.querySelector(".verdict") || {}).textContent || "",
    };
  });

  check("amber headline says what is available, not what is absent", () => {
    assert.ok(amberCard, "no card appeared on hovering an amber mark");
    // The mark's whole value is that characterised antibodies exist for this
    // target; the absence is the precondition, not the message. Leading with it
    // made the card read as a verdict on a reagent — and on OGA's own gene
    // pages, where "10 APP antibodies" is descriptive text and no product at
    // all, as an accusation about an antibody that does not exist.
    assert.match(amberCard.verdict, /characterised antibodies available for/i);
    assert.match(amberCard.verdict, /TARDBP/);
    assert.doesNotMatch(amberCard.verdict, /not in the dataset/i);
  });

  check("amber card quotes the text the target came from", () => {
    // The target was READ off the page, not looked up, and the card still says
    // so — in the body now rather than the headline.
    assert.match(amberCard.text, /taken from/i);
    assert.match(amberCard.text, /TDP-43/, "the phrase that produced the gene is not quoted");
    assert.match(amberCard.text, /TARDBP/, "the gene it resolved to is not named");
  });

  check("amber card keeps the dataset caveat and does not hedge the claim", () => {
    // The caveat is that *this* antibody is untested, so the reader does not
    // read "alternatives exist" as a result about the one in front of them. It
    // used to read "absence is not a verdict on quality"; the explanation went
    // on 7 Aug 2026 (untested is untested) and the word "verdict" with it, but
    // the claim itself is load-bearing and stays.
    assert.match(amberCard.text, /has not been tested/i);
    assert.doesNotMatch(amberCard.text, /verdict/i);
    assert.doesNotMatch(amberCard.text, /\bmay have\b|\bmight have\b/i);
    // Transparency, not a warning: the audit found 0 of 45 amber marks with a
    // wrong gene, so the card shows its source and then states the claim.
    assert.doesNotMatch(amberCard.text, /check it matches|not necessarily|if the target is right/i);
  });

  check("the quoted phrase is escaped, not injected", () => {
    // target.raw is page-derived text going into innerHTML. The character
    // classes that capture it cannot currently produce markup, which is
    // exactly the kind of thing that stops being true quietly.
    assert.ok(!/<script|onerror=/i.test(amberCard.html), "unescaped page text reached the card");
  });

  check("no uncaught page errors", () => {
    assert.deepEqual(consoleErrors, []);
  });

  if (screenshotPath) {
    await page.setViewportSize({ width: 900, height: 900 });
    await page.locator("#p-wb mark.oga-hl").first().hover();
    await page.waitForTimeout(500);
    await page.screenshot({ path: screenshotPath, fullPage: false });
    console.log("screenshot ->", screenshotPath);
  }

  console.log(`\nhighlights found: ${marks.length}`);
  for (const m of marks) console.log(`  ${String(m.cls).padEnd(10)} ${m.inside ?? "-"}  "${m.text}"`);
} finally {
  await context.close();
  server.close();
  fs.rmSync(stage, { recursive: true, force: true });
  fs.rmSync(userDataDir, { recursive: true, force: true });
}

if (failures.length) {
  console.error(`\n${failures.length} failing, ${passed} passing\n`);
  for (const f of failures) console.error("  FAIL " + f + "\n");
  process.exit(1);
}
console.log(`\nall ${passed} end-to-end checks passed`);
