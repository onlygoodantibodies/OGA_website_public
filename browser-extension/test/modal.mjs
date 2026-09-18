/**
 * Modal / pop-out check.
 *
 *   CHROMIUM_PATH=... xvfb-run -a node browser-extension/test/modal.mjs
 *
 * Publishers pre-render reagent tables inside a hidden modal and reveal them by
 * flipping an attribute, which adds no nodes. A childList observer never fires,
 * so the content has to be marked while it is still hidden.
 */
import fs from "node:fs"; import http from "node:http"; import os from "node:os"; import path from "node:path";
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
const here = path.dirname(fileURLToPath(import.meta.url));
const extRoot = path.join(here, "..");
const fixture = fs.readFileSync(path.join(here, "fixtures/modal.html"));
const server = http.createServer((_q,r)=>{r.writeHead(200,{"Content-Type":"text/html"});r.end(fixture);});
await new Promise(r=>server.listen(0,"127.0.0.1",r));
const stage = fs.mkdtempSync(path.join(os.tmpdir(),"oga-m-"));
fs.cpSync(extRoot, stage, {recursive:true, filter:s=>!s.includes("node_modules")});
const mp = path.join(stage,"manifest.json");
const m = JSON.parse(fs.readFileSync(mp,"utf8")); m.content_scripts[0].matches.push("http://127.0.0.1/*");
fs.writeFileSync(mp, JSON.stringify(m,null,2));
const udd = fs.mkdtempSync(path.join(os.tmpdir(),"oga-mp-"));
const ctx = await chromium.launchPersistentContext(udd,{headless:false,executablePath:process.env.CHROMIUM_PATH,
  args:[`--disable-extensions-except=${stage}`,`--load-extension=${stage}`,"--no-sandbox"]});
const p = await ctx.newPage();
await p.goto(`http://127.0.0.1:${server.address().port}/x.html`,{waitUntil:"domcontentloaded"});

/* WAIT FOR THE MARKS, NOT FOR THE CLOCK.
 *
 * This was `waitForTimeout(1600)`, and 1600ms is not a fact about anything —
 * it is how long the content script happened to take to fetch, prepare and
 * scan with the index of the day. Adding the PERK lists took the bundled
 * fixture from 40 KB to 75 KB and the wait stopped being enough on a CI
 * runner: `before` came back EMPTY, the suite reported "modal content was not
 * marked", and the log printed both marks present a line later because the
 * revealed read happened 2.5s further on. The product was fine and the clock
 * was wrong.
 *
 * Waiting for the condition is both robust and the assertion itself: the point
 * of this test is that content inside a `display:none` modal IS marked before
 * anybody reveals it, so waiting for two marks while hidden IS that claim. A
 * timeout here fails with the same meaning the old length check had. */
const MARKS = "#m mark.oga-hl";
let before = [];
try {
  await p.waitForFunction(
    (sel) => document.querySelectorAll(sel).length === 2, MARKS,
    { timeout: 15000 });
  before = await p.$$eval(MARKS, els=>els.map(e=>e.textContent.trim()));
} catch { /* left empty, and reported as the hidden-phase failure below */ }
console.log("while the modal is hidden :", JSON.stringify(before));
await p.waitForTimeout(2500);   // the reveal fires at 2.5s
const after = await p.$$eval(MARKS, els=>els.map(e=>
  `${[...e.classList].find(c=>c.startsWith('oga-')&&c!=='oga-hl')}:${e.textContent.trim()}`));
const visible = await p.evaluate(()=>getComputedStyle(document.getElementById('m')).display !== 'none');
console.log("after it is revealed     :", JSON.stringify(after), "| visible:", visible);
// Name WHICH half failed. The old message said "modal content was not marked"
// for any of the three, and printed a healthy `after` list directly above it —
// so the one real failure it ever caught read as a contradiction of its own log.
const why = [];
if (before.length !== 2) why.push(`hidden modal had ${before.length} marks, wanted 2`);
if (after.length !== 2) why.push(`revealed modal had ${after.length} marks, wanted 2`);
if (!visible) why.push("the modal never became visible");
if (why.length) {
  console.error("FAIL: " + why.join("; "));
  process.exitCode = 1;
} else {
  console.log("ok — pre-rendered modal content is marked and survives the reveal");
}
await ctx.close(); server.close();
