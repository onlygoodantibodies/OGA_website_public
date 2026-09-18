/**
 * Settings page persistence check.
 *
 *   CHROMIUM_PATH=... xvfb-run -a node browser-extension/test/settings.mjs
 *
 * Drives the real options page in a real browser, because the failure this
 * covers is invisible to a unit test: Save appeared to work, said "Saved", and
 * stored nothing, because a permissions call earlier in the same handler threw
 * and unwound the write that came after it.
 */
import fs from "node:fs"; import os from "node:os"; import path from "node:path";
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const extRoot = path.join(here, "..");
const stage = fs.mkdtempSync(path.join(os.tmpdir(), "oga-s-"));
fs.cpSync(extRoot, stage, { recursive: true, filter: (s) => !s.includes("node_modules") });
const udd = fs.mkdtempSync(path.join(os.tmpdir(), "oga-sp-"));
const ctx = await chromium.launchPersistentContext(udd, {
  headless: false, executablePath: process.env.CHROMIUM_PATH,
  args: [`--disable-extensions-except=${stage}`, `--load-extension=${stage}`, "--no-sandbox"],
});

const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker", { timeout: 10000 });
const extId = new URL(sw.url()).host;

const failures = [];
let passed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log("  ok   " + name); }
  catch (err) { failures.push(`${name}\n    ${err.message}`); console.log("  FAIL " + name); }
}

const page = await ctx.newPage();
await page.goto(`chrome-extension://${extId}/src/options.html`, { waitUntil: "domcontentloaded" });

/* WAIT FOR THE PAGE TO HAVE FILLED ITS OWN CONTROLS.
 *
 * Every checkbox in `options.html` ships with no `checked` attribute, and
 * `options.js::load` ticks them from `background.js::DEFAULT_SETTINGS` — over a
 * `sendMessage` round trip to the service worker, well after DOMContentLoaded.
 * `showGrey` defaults to **true**, so on a loaded runner this raced and LOST
 * SILENTLY: `uncheck` ran while the box was still unticked, which is a no-op,
 * `load` then ticked it, and Save honestly stored `showGrey: true`. Green here
 * for as long as this machine won the race; red on CI (run 130, 12 Sep 2026)
 * on a commit that added one binary file under `signed/` and could not have
 * touched it.
 *
 * Waiting for the box to be TICKED is the precondition rather than a sleep:
 * this test's premise is "turn off a setting that is on", and this is the
 * assertion that it is on to begin with. `patterns` is not waited for — it
 * defaults to FALSE, so there is nothing to wait for and `check` below is the
 * thing that turns it on. */
await page.waitForFunction(
  () => document.getElementById("showGrey").checked, { timeout: 8000 });

// A setting that needs no host permission at all: if this does not survive,
// nothing else about the page matters. Both directions are covered on purpose:
// `showGrey` off against a true default, `patterns` on against a false one, so
// neither assertion can pass on a save that never happened.
await page.uncheck("#showGrey");
await page.check("#patterns");
await page.click("#save");
await page.waitForFunction(() => document.getElementById("status").textContent.length > 0, { timeout: 8000 });
const status = await page.textContent("#status");
console.log("status after save:", JSON.stringify(status));

const stored = await sw.evaluate(async () => {
  const local = (await chrome.storage.local.get("settings")).settings || null;
  const sync = await chrome.storage.sync.get("settings").then((s) => s.settings || null).catch(() => null);
  return { local, sync };
});
console.log("stored:", JSON.stringify(stored));

check("the write reached at least one storage area", () => {
  const s = stored.sync || stored.local;
  if (!s) throw new Error("nothing was stored at all");
});
check("showGrey was turned off and stayed off", () => {
  const s = stored.sync || stored.local;
  if (s.showGrey !== false) throw new Error(`showGrey is ${s.showGrey}`);
});
check("patterns was turned on and stayed on", () => {
  const s = stored.sync || stored.local;
  if (s.patterns !== true) throw new Error(`patterns is ${s.patterns}`);
});

// Reload: what the page shows must be what was actually stored, not the
// defaults and not what was typed.
await page.reload({ waitUntil: "domcontentloaded" });
/* The same race on the way back, and `showGrey` cannot be the signal now: it
 * was saved OFF and the markup has it off too, so unticked is
 * indistinguishable from "load has not run yet" and a sleep would be asserting
 * the markup. `patterns` was saved ON against a false default, so its tick is
 * the one observable that only storage can have produced. */
await page.waitForFunction(
  () => document.getElementById("patterns").checked, { timeout: 8000 });
const shown = await page.evaluate(() => ({
  showGrey: document.getElementById("showGrey").checked,
  patterns: document.getElementById("patterns").checked,
}));
console.log("shown after reload:", JSON.stringify(shown));
check("the reopened page shows the saved values", () => {
  if (shown.showGrey !== false || shown.patterns !== true) {
    throw new Error(`reopened as ${JSON.stringify(shown)}`);
  }
});

// The declined-permission path must not take the other settings down with it.
// Chrome auto-denies permissions.request without a user gesture it trusts, so
// this asserts the weaker but essential property: everything else still saved.
await page.check("#allSites");
await page.click("#save");
await page.waitForFunction(() => document.getElementById("status").textContent.length > 0, { timeout: 8000 });
const afterPerm = await sw.evaluate(async () =>
  (await chrome.storage.local.get("settings")).settings || null);
console.log("stored after a site-access attempt:", JSON.stringify(afterPerm));
check("a site-access failure does not discard the other settings", () => {
  if (!afterPerm || afterPerm.patterns !== true) {
    throw new Error(`patterns lost: ${JSON.stringify(afterPerm)}`);
  }
});

await ctx.close();
if (failures.length) {
  console.error(`\n${failures.length} failing, ${passed} passing\n`);
  for (const f of failures) console.error("  FAIL " + f + "\n");
  process.exit(1);
}
console.log(`\nall ${passed} settings checks passed`);
