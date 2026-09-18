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
  // The extension refreshes its own citation table on install:
  // `chrome.runtime.onInstalled` fires the moment Playwright loads the unpacked
  // extension into this fresh profile, and calls `refreshCitations`
  // UNCONDITIONALLY -- not through `refreshIfStale`. So it fetches the live
  // snapshot and `storage.local.set`s it over whatever this test seeded, and the
  // fixture's synthetic title key is not in the real record.
  //
  // That made the suite pass or fail on whether the machine running it could
  // reach the site: green on a sandbox with no egress, red on CI, and the
  // browser version it got blamed on had nothing to do with it. Playwright's
  // context.route does NOT reach a worker's fetch -- tried, and the handler was
  // never called -- so the stub goes inside the worker, where the fetch is.
  await worker.evaluate((table) => {
    const real = globalThis.fetch;
    globalThis.fetch = async (input, ...rest) => {
      if (String(input && input.url || input).includes("/extension/citations.json")) {
        return new Response(JSON.stringify(table),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return real(input, ...rest);
    };
  }, CITATIONS);

  await worker.evaluate(async (table) => {
    await chrome.storage.local.set({ citations: table, citationsFetchedAt: Date.now() });
  }, CITATIONS);

  // A fetch already in flight when the stub went up can still land after the
  // seed, so the seed is not assumed -- it is checked, and re-applied. Asserting
  // the table IS the fixture at page-load time is the invariant the three
  // lookup checks below rest on; without it a clobbered run reports three
  // mysterious product failures instead of one plain harness one.
  let settled = false;
  for (let i = 0; i < 20 && !settled; i++) {
    settled = await worker.evaluate(async (key) => {
      const { citations } = await chrome.storage.local.get("citations");
      return !!(citations && citations.by_title && citations.by_title[key] !== undefined);
    }, titleKey);
    if (!settled) {
      await worker.evaluate(async (table) => {
        await chrome.storage.local.set({ citations: table, citationsFetchedAt: Date.now() });
      }, CITATIONS);
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  if (!settled) {
    console.log("\nFAILED: the seeded citation table never stuck -- the extension's "
      + "own refresh keeps overwriting it, so nothing below would be testing the "
      + "fixture.");
    await context.close(); server.close(); process.exit(1);
  }

  // ...and then hold it, because settling is not the same as staying settled.
  // The loop above only re-seeds while the fixture is MISSING, so it stops
  // watching the moment it is there — and a refresh that began before the stub
  // went up is still in flight, on a runner whose network is slower than the
  // two seconds that loop spends. It lands afterwards and overwrites the
  // fixture between here and the page load, which is why CI stayed red through
  // the stub, the re-seed and the guard: all three were about the fetch, and
  // this one arrives as a WRITE.
  //
  // So the write is what is closed off. Any later `citations` key is dropped
  // and every other key still goes through — the table cannot be replaced
  // whatever lands, whenever it lands, which is a guarantee no amount of
  // waiting can give. Driven both ways: with a live-shaped snapshot set 300ms
  // from here, the suite fails exactly as CI does without this and passes with
  // it.
  await worker.evaluate(() => {
    const realSet = chrome.storage.local.set.bind(chrome.storage.local);
    chrome.storage.local.set = (items, cb) => {
      if (items && Object.prototype.hasOwnProperty.call(items, "citations")) {
        const { citations, ...rest } = items;
        if (!Object.keys(rest).length) return cb ? cb() : Promise.resolve();
        return realSet(rest, cb);
      }
      return realSet(items, cb);
    };
  });

  // ...AND the copy held in memory, which is the half both earlier fixes missed
  // and the reason CI stayed red through all of them.
  //
  // `refreshCitations` assigns `cachedCitations` BEFORE it writes storage, and
  // `getCitations` returns that copy without reading storage at all:
  //
  //     cachedCitations = fresh;                       // <- the in-flight fetch
  //     await chrome.storage.local.set({ citations: fresh, ... });   // <- locked above
  //
  // So the lock above does exactly what it claims and the worker goes on
  // answering from the live snapshot regardless. Worse, it made the diagnosis
  // point the wrong way: the probe below reads STORAGE, so a clobbered run
  // printed a healthy fixture and a covered lookup beside three failures it
  // could not explain. Both earlier attempts were reasoning about the copy that
  // was fine.
  //
  // `getCitations` is a top-level function declaration in a classic worker
  // script, so it IS a property of the global object and the message handler
  // resolves it there at call time — replacing it reaches the real caller.
  // Pointed at storage, which the lock above has already made unclobberable,
  // the in-memory copy stops mattering whatever lands and whenever.
  //
  // Driven both ways on Chromium 141: with `cachedCitations` set to a
  // live-shaped table after the seed, the three checks fail exactly as CI
  // prints them without this, and pass with it.
  await worker.evaluate(() => {
    globalThis.getCitations = async () => {
      const { citations } = await chrome.storage.local.get("citations");
      return citations && citations.schema === 1 ? citations : null;
    };
  });

  // AND THE INDEX, for the same reason and by the same route. `onInstalled`
  // calls `refreshIndex()` on the line above `refreshCitations()`, so CI's
  // network replaces the bundled 18-record fixture with the live 1,603-record
  // one — and the live record for this fixture's antibody carries a qualifier
  // where the bundled one does not, which makes the same mark yellow there and
  // red here. Two clobbers, and only one of them had ever been guarded; the
  // second was reading as a product fault in the very check the first one
  // broke. Every assertion below is about the bundled fixture, so it is the
  // bundled fixture that has to be what answers.
  await worker.evaluate(async () => {
    const { index } = await chrome.storage.local.get("index");
    const bundled = index && index.schema === 1 ? index : null;
    globalThis.getIndex = async () => {
      if (bundled) return bundled;
      const resp = await fetch(chrome.runtime.getURL("data/index.json"));
      return resp.json();
    };
  });

  // Record what the content script actually asks for. All three lookup-dependent
  // checks fail together when the paper record does not reach the card, and the
  // failure reads the same whichever half broke: a key computed differently in
  // the tab, or a worker that never answered. This listener runs before the real
  // one and returns false, so it observes and answers nothing.
  await worker.evaluate(() => {
    globalThis.__ogaSeen = [];
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === "oga:get-index") globalThis.__ogaSeen.push(msg.keys);
      return false;
    });
  });

  const page = await context.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("mark.oga-hl", { timeout: 15000 });
  await page.waitForTimeout(800);

  const marks = await page.$$eval("mark.oga-hl", (els) => els.map((el) => ({
    text: el.textContent.trim(),
    cls: [...el.classList].find((c) => c.startsWith("oga-") && c !== "oga-hl"),
    inside: el.closest("p") ? el.closest("p").id : null,
  })));

  // Printed on every run, not only a failing one: the three checks below share
  // one dependency, and which half of it broke is invisible in their messages.
  // A worker that restarted loses the listener above -- which is itself an
  // answer, so it is reported as one rather than as "nothing was asked".
  const asked = await worker.evaluate(() => globalThis.__ogaSeen || null);
  const lookup = await worker.evaluate(async (key) => {
    const stored = await chrome.storage.local.get("citations");
    return {
      titlesInTable: stored.citations && stored.citations.by_title
        ? Object.keys(stored.citations.by_title) : null,
      direct: typeof findPaper === "function"
        ? findPaper(stored.citations, { titleKey: key })
        : "findPaper is not reachable in the worker",
    };
  }, titleKey);
  console.log(`  key this fixture is filed under : ${titleKey}`);
  console.log(`  keys the content script sent    : ${asked === null
    ? "unknown -- the worker restarted and the diagnostic listener went with it"
    : JSON.stringify(asked)}`);
  console.log(`  worker-side lookup on that key  : ${JSON.stringify(lookup)}`);

  check("every mark carries a level class", () => {
    // `LEVEL_CLASS` is the only thing that paints a mark, and a level missing
    // from it yields `class="oga-hl undefined"`: drawn, hoverable, correct in
    // the card, and invisible as a signal on the page. That is how `yellow`
    // shipped unpainted from 0.3.1 to 12 Sep 2026. Checked here over whatever
    // this page produced rather than against a list, so a level added later is
    // covered without anybody remembering this line.
    const unpainted = marks.filter((m) => !m.cls);
    assert.deepEqual(unpainted, [],
      "a mark was drawn with no level class, so it has no colour at all");
  });

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
