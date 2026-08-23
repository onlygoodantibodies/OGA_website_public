/**
 * Supplier-catalogue toggle check.
 *
 *   CHROMIUM_PATH=... xvfb-run -a node browser-extension/test/vendor.mjs
 *
 * The toggle does not add the supplier hosts to the manifest — it registers a
 * content script at runtime, which is a different mechanism from the one every
 * other page uses and has no coverage otherwise. This drives that mechanism:
 * registerScript() as options.js calls it, then a real navigation to a page it
 * should now cover.
 *
 * The permission prompt itself cannot be answered in a headless run, so the
 * host is granted in the staged manifest instead. What is under test is
 * everything after the grant.
 */
import fs from "node:fs"; import http from "node:http"; import os from "node:os"; import path from "node:path";
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const extRoot = path.join(here, "..");

// A stand-in supplier product page: a catalogue number and enough antibody
// wording to clear the context gate, and nothing else.
const page_html = `<!doctype html><html><body>
  <h1>Anti-TDP-43 antibody [EPR5810]</h1>
  <p>Rabbit recombinant monoclonal antibody, catalogue number ab109535, for western blot.</p>
</body></html>`;
const server = http.createServer((_q, r) => {
  r.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  r.end(page_html);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const origin = `http://127.0.0.1:${server.address().port}`;

const stage = fs.mkdtempSync(path.join(os.tmpdir(), "oga-v-"));
fs.cpSync(extRoot, stage, { recursive: true, filter: (s) => !s.includes("node_modules") });

// Grant the stand-in host up front. The real toggle obtains the equivalent
// grant from optional_host_permissions via the browser's prompt; from here on
// the code path is identical.
const mp = path.join(stage, "manifest.json");
const m = JSON.parse(fs.readFileSync(mp, "utf8"));
m.host_permissions = [...(m.host_permissions || []), "http://127.0.0.1/*"];
fs.writeFileSync(mp, JSON.stringify(m, null, 2));

const udd = fs.mkdtempSync(path.join(os.tmpdir(), "oga-vp-"));
const ctx = await chromium.launchPersistentContext(udd, {
  headless: false, executablePath: process.env.CHROMIUM_PATH,
  args: [`--disable-extensions-except=${stage}`, `--load-extension=${stage}`, "--no-sandbox"],
});
const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker", { timeout: 10000 });

const failures = [];
let passed = 0;
function check(name, ok, detail) {
  if (ok) { passed++; console.log("  ok   " + name); }
  else { failures.push(`${name}\n    ${detail}`); console.log("  FAIL " + name + " — " + detail); }
}

/* Before: the host is not in content_scripts, so nothing should mark. */
const before = await ctx.newPage();
await before.goto(origin + "/p.html", { waitUntil: "domcontentloaded" });
await before.waitForTimeout(800);
const marksBefore = await before.$$eval("mark.oga-hl", (n) => n.length);
check("nothing is marked before the toggle", marksBefore === 0, `found ${marksBefore} marks`);
await before.close();

/* Register exactly as options.js does. */
const registered = await sw.evaluate(async (matches) => {
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({ ids: ["oga-vendor-sites"] });
    if (existing && existing.length) return { ok: true, already: true };
    await chrome.scripting.registerContentScripts([{
      id: "oga-vendor-sites",
      matches,
      js: ["src/matcher.js", "src/card.js", "src/content.js"],
      css: ["src/content.css"],
      runAt: "document_idle",
    }]);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}, ["http://127.0.0.1/*"]);
console.log("registerContentScripts:", JSON.stringify(registered));
check("the content script registers", registered.ok, registered.error);

const listed = await sw.evaluate(() =>
  chrome.scripting.getRegisteredContentScripts().then((s) => s.map((x) => x.id)).catch((e) => String(e)));
console.log("registered ids:", JSON.stringify(listed));
check("it is listed afterwards",
  Array.isArray(listed) && listed.includes("oga-vendor-sites"), JSON.stringify(listed));

/* After: a fresh navigation should now be covered. */
const after = await ctx.newPage();
await after.goto(origin + "/p.html", { waitUntil: "domcontentloaded" });
await after.waitForTimeout(1500);
const marksAfter = await after.$$eval("mark.oga-hl", (n) => Array.from(n, (x) => x.textContent));
console.log("marks after:", JSON.stringify(marksAfter));
check("the supplier page is marked after the toggle", marksAfter.length > 0, "no marks appeared");

await ctx.close();
server.close();
if (failures.length) {
  console.error(`\n${failures.length} failing, ${passed} passing\n`);
  for (const f of failures) console.error("  FAIL " + f + "\n");
  process.exit(1);
}
console.log(`\nall ${passed} vendor checks passed`);
