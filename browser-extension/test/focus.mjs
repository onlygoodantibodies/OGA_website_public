/**
 * Popup "jump to next mark" check.
 *
 *   CHROMIUM_PATH=... xvfb-run -a node browser-extension/test/focus.mjs
 *
 * Drives the same message the popup sends, so it exercises the real path:
 * tile click -> content script -> scroll + pulse.
 */
import fs from "node:fs"; import http from "node:http"; import os from "node:os"; import path from "node:path";
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
const here = path.dirname(fileURLToPath(import.meta.url));
const extRoot = path.join(here, "..");
const fixture = fs.readFileSync(path.join(here, "fixtures/paper.html"));
const server = http.createServer((_q,r)=>{r.writeHead(200,{"Content-Type":"text/html"});r.end(fixture);});
await new Promise(r=>server.listen(0,"127.0.0.1",r));
const stage = fs.mkdtempSync(path.join(os.tmpdir(),"oga-f-"));
fs.cpSync(extRoot, stage, {recursive:true, filter:s=>!s.includes("node_modules")});
const mp = path.join(stage,"manifest.json");
const m = JSON.parse(fs.readFileSync(mp,"utf8")); m.content_scripts[0].matches.push("http://127.0.0.1/*");
fs.writeFileSync(mp, JSON.stringify(m,null,2));
const udd = fs.mkdtempSync(path.join(os.tmpdir(),"oga-fp-"));
const ctx = await chromium.launchPersistentContext(udd,{headless:false,executablePath:process.env.CHROMIUM_PATH,
  args:[`--disable-extensions-except=${stage}`,`--load-extension=${stage}`,"--no-sandbox"]});
const page = await ctx.newPage();
await page.setViewportSize({width:900,height:500});
await page.goto(`http://127.0.0.1:${server.address().port}/p.html`,{waitUntil:"domcontentloaded"});
await page.waitForSelector("mark.oga-hl",{timeout:15000});
await page.waitForTimeout(900);

const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker",{timeout:10000});
// Same lookup popup.js uses — the extension has no "tabs" permission, so
// tab.url is not readable; the active tab is what the popup acts on.
const tabId = await sw.evaluate(async () => {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t && t.id;
});
console.log("active tabId:", tabId);

async function click(level) {
  const res = await sw.evaluate(({tabId, level}) =>
    chrome.tabs.sendMessage(tabId, {type:"oga:focus", level}), {tabId, level});
  await page.waitForTimeout(500);
  const st = await page.evaluate(() => ({
    scrollTop: Math.round(document.body.scrollTop || document.documentElement.scrollTop),
    pulsing: document.querySelector("mark.oga-focus")?.textContent.trim() ?? null,
  }));
  console.log(`  click ${level.padEnd(6)} -> ${JSON.stringify(res)}  pulsing=${JSON.stringify(st.pulsing)}`);
  return st;
}
console.log("clicking the green tile repeatedly (should advance, then wrap):");
await click("green"); await click("green"); await click("green"); await click("green"); await click("green");
console.log("other levels:");
await click("red"); await click("amber"); await click("grey");
console.log("a level with no marks:");
await click("mixed");
await ctx.close(); server.close();
