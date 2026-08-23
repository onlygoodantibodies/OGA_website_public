/**
 * The citation layer and the card's tabs, in a real browser.
 *
 *   CHROMIUM_PATH=... xvfb-run -a node browser-extension/test/tabs.mjs
 *
 * Two things here are invisible to every node suite, which is the whole reason
 * this file drives a browser:
 *
 *  - THE WORKER BOUNDARY. The lookup table lives in the service worker and the
 *    title rules live in the content script, and they talk over
 *    chrome.runtime.sendMessage. Both halves can be perfect and the feature
 *    still do nothing -- which is exactly what happened: `importScripts` in the
 *    worker throws in MV3, the worker then registers with no listeners, and
 *    every page silently stops being marked. Three green node suites said
 *    nothing about it.
 *  - A TAB THAT RENDERS BUT IS NOT WIRED. The markup is asserted easily and
 *    proves nothing; what matters is that clicking one changes what is drawn,
 *    and that the card survives the click at all (the mark's `mouseout`
 *    schedules a hide, so a tab outside `.card` would dismiss the card as the
 *    pointer travelled to it).
 */
import fs from "node:fs"; import http from "node:http"; import os from "node:os"; import path from "node:path";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const extRoot = path.join(here, "..");

// ab109535 / AB_10859634 in the bundled index: WB recommended, IF NOT
// recommended. So a paper the record says used it for IF must draw RED, where
// the whole-record verdict is mixed. That difference IS the citation layer.
const RRID = "AB_10859634";
const TITLE = "TDP-43 aggregation in β-amyloid models";   // Greek letter on purpose

const paper = fs.readFileSync(path.join(here, "fixtures/paper.html"), "utf8")
  .replace("<head>", `<head>
    <meta name="citation_title" content="${TITLE}">
    <meta name="citation_publication_date" content="2023-04-01">`);

const server = http.createServer((_q, r) => {
  r.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  r.end(paper);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const url = `http://127.0.0.1:${server.address().port}/paper.html`;

const stage = fs.mkdtempSync(path.join(os.tmpdir(), "oga-tabs-"));
fs.cpSync(extRoot, stage, { recursive: true, filter: (s) => !s.includes("node_modules") });
const manifest = JSON.parse(fs.readFileSync(path.join(stage, "manifest.json"), "utf8"));
manifest.content_scripts[0].matches.push("http://127.0.0.1/*");
fs.writeFileSync(path.join(stage, "manifest.json"), JSON.stringify(manifest, null, 2));

// The title key computed by the SHIPPED normaliser, so this test cannot pass by
// agreeing with a private copy of the rules.
new Function(fs.readFileSync(path.join(extRoot, "src/paper.js"), "utf8"))();
const titleKey = globalThis.OGAPaper.titleKey(TITLE);

const CITATIONS = {
  schema: 1,
  source: { citeab_data_through: "2026-05-05" },
  rrids: [RRID],
  tokens: ["IHC"],
  papers: ["0:24.0"],           // 0x24 = IF(4) + UNTESTED_APP(32); token 0 = IHC
  years: [2023],
  by_title: { [titleKey]: 0 },
  by_pmid: {},
  by_doi: {},
};

const context = await chromium.launchPersistentContext(
  fs.mkdtempSync(path.join(os.tmpdir(), "oga-tabs-profile-")), {
    headless: false,
    args: [`--disable-extensions-except=${stage}`, `--load-extension=${stage}`, "--no-sandbox"],
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });

const failures = [];
let passed = 0;
const check = (name, fn) => {
  try { fn(); passed++; console.log(`  ok   ${name}`); }
  catch (err) { failures.push(`${name}\n    ${err.message}`); console.log(`  FAIL ${name}`); }
};

try {
  // Seed the citation table into the worker's storage: there is no portal to
  // fetch it from in a test, and what is under test is everything after arrival.
  let [worker] = context.serviceWorkers();
  if (!worker) worker = await context.waitForEvent("serviceworker", { timeout: 15000 });
  await worker.evaluate(async (table) => {
    await chrome.storage.local.set({ citations: table, citationsFetchedAt: Date.now() });
  }, CITATIONS);

  const page = await context.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("mark.oga-hl", { timeout: 15000 });
  await page.waitForTimeout(800);

  const marks = await page.$$eval("mark.oga-hl", (els) => els.map((el) => ({
    text: el.textContent.trim(),
    cls: [...el.classList].find((c) => c.startsWith("oga-") && c !== "oga-hl"),
    inside: el.closest("p") ? el.closest("p").id : null,
  })));

  check("a looked-up application narrows the mark that proximity left mixed", () => {
    // Same antibody, same page, in the WB paragraph -- but the citation record
    // says this PAPER used it for IF, and it failed IF.
    const wb = marks.find((m) => m.text.includes("ab109535") && m.inside === "p-wb");
    assert.ok(wb, `no ab109535 mark: ${JSON.stringify(marks)}`);
    assert.equal(wb.cls, "oga-red",
      "expected red from the looked-up IF verdict, got " + wb.cls);
  });

  // Hover it and read the card out of the shadow root.
  const anchor = page.locator('mark.oga-hl:has-text("ab109535")').first();
  await anchor.hover();
  await page.waitForTimeout(400);

  const cardText = () => page.evaluate(() => {
    const h = document.querySelector(".oga-card-host");
    return h && h.shadowRoot ? h.shadowRoot.querySelector(".card").textContent : "";
  });
  const tabs = () => page.evaluate(() => {
    const h = document.querySelector(".oga-card-host");
    if (!h || !h.shadowRoot) return [];
    return [...h.shadowRoot.querySelectorAll("[data-tab]")].map((b) => ({
      app: b.getAttribute("data-tab"),
      pressed: b.getAttribute("aria-pressed") === "true",
      tag: b.tagName,
    }));
  });
  const caption = () => page.evaluate(() => {
    const h = document.querySelector(".oga-card-host");
    const el = h && h.shadowRoot && h.shadowRoot.querySelector(".fig figcaption");
    return el ? el.textContent.trim() : "";
  });

  // Everything the page has to be asked for is read BEFORE the assertions:
  // `check` is synchronous, so an assert on a pending promise passes silently.
  const first = await tabs();
  const firstCaption = await caption();
  const firstText = await cardText();

  check("the card draws one real button per assessed application", () => {
    assert.deepEqual(first.map((t) => t.app), ["WB", "IP", "IF", "FC"]);
    assert.ok(first.every((t) => t.tag === "BUTTON"),
      "tabs must be buttons so they are keyboard-reachable");
  });

  check("it opens on the application the PAPER used, not an arbitrary one", () => {
    assert.deepEqual(first.filter((t) => t.pressed).map((t) => t.app), ["IF"]);
    assert.match(firstCaption, /Immunofluorescence/);
  });

  check("an application OGA does not test is named AND answered with context", () => {
    // Naming it leaves a reader with nothing to do. What is known about the
    // reagent -- in the applications that WERE tested -- is the actionable half.
    assert.match(firstText, /IHC/,
      "the paper used it for IHC and the card should say so by name");
    assert.match(firstText, /does not test/);
    assert.match(firstText, /For context/,
      "must say what OGA did find, not just what it did not");
  });

  // The click: this is what a node suite cannot see.
  await page.evaluate(() => {
    const h = document.querySelector(".oga-card-host");
    h.shadowRoot.querySelector('[data-tab="WB"]').click();
  });
  await page.waitForTimeout(250);

  const afterClick = await tabs();
  const afterCaption = await caption();
  check("clicking a tab actually changes what is drawn", () => {
    assert.deepEqual(afterClick.filter((t) => t.pressed).map((t) => t.app), ["WB"]);
    assert.match(afterCaption, /Western blot/,
      `figure did not switch; caption is "${afterCaption}"`);
  });

  check("the card survives the click instead of dismissing itself", () => {
    assert.ok(afterClick.length === 4, "the card closed when a tab was clicked");
  });
} finally {
  await context.close();
  server.close();
}

console.log(failures.length
  ? `\n${failures.length} FAILED:\n  ${failures.join("\n  ")}\n`
  : `\nall ${passed} tab checks passed`);
process.exit(failures.length ? 1 : 0);
