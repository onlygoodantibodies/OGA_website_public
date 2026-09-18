/**
 * Test runner.  npm test  — or  npm test -- --browser  to include Chromium.
 *
 * Two tiers, deliberately:
 *
 *   always   matcher unit tests and the page-level fixtures. Pure Node plus
 *            jsdom, a couple of seconds, no browser. These are what CI runs on
 *            every push and what should be run before every commit.
 *
 *   --browser  the Playwright suites, which load the real unpacked extension
 *            into Chromium. They need a Chromium binary and a display, so they
 *            are opt-in rather than a broken `npm test` on a fresh checkout.
 *            CHROMIUM_PATH being set is taken as opting in.
 *
 * Chromium only. Playwright can load extensions in Chromium and nowhere else,
 * so Firefox has no automated coverage — see README.md.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

const wantBrowser = process.argv.includes("--browser") || Boolean(process.env.CHROMIUM_PATH);

const ALWAYS = ["matcher.test.mjs", "paper.test.mjs", "pages.test.mjs", "card.test.mjs",
                "confusions.test.mjs", "popup.test.mjs",
                "vocabulary.test.mjs"];
const BROWSER = ["e2e.mjs", "tabs.mjs", "focus.mjs", "modal.mjs", "settings.mjs", "vendor.mjs"];

// xvfb-run, when there is no display. Chrome will not load an extension
// headlessly, so the browser suites need aframebuffer one way or another.
const needsXvfb = wantBrowser && !process.env.DISPLAY && hasBinary("xvfb-run");

function hasBinary(name) {
  return spawnSync("which", [name], { encoding: "utf8" }).status === 0;
}

function run(file, browser) {
  const target = path.join(here, file);
  if (!fs.existsSync(target)) return { file, skipped: "missing" };

  const cmd = browser && needsXvfb ? "xvfb-run" : process.execPath;
  const args = browser && needsXvfb ? ["-a", process.execPath, target] : [target];

  process.stdout.write(`\n─── ${file} ${"─".repeat(Math.max(0, 56 - file.length))}\n`);
  const res = spawnSync(cmd, args, { stdio: "inherit", cwd: path.join(here, "..") });
  return { file, status: res.status === null ? 1 : res.status };
}

const results = ALWAYS.map((f) => run(f, false));

if (wantBrowser) {
  if (!process.env.CHROMIUM_PATH) {
    console.warn("\n[warn] CHROMIUM_PATH is not set; Playwright will look for its own download.");
  }
  results.push(...BROWSER.map((f) => run(f, true)));
} else {
  console.log(`\n[skipped] ${BROWSER.length} browser suites — rerun with --browser or set CHROMIUM_PATH.`);
}

const failed = results.filter((r) => r.status);
const skipped = results.filter((r) => r.skipped);

console.log("\n" + "═".repeat(60));
for (const r of results) {
  console.log(`  ${r.skipped ? "SKIP" : r.status ? "FAIL" : "ok  "}  ${r.file}${r.skipped ? ` (${r.skipped})` : ""}`);
}
console.log("═".repeat(60));

if (failed.length) {
  console.error(`\n${failed.length} suite(s) failed.\n`);
  process.exit(1);
}
console.log(`\n${results.length - skipped.length} suite(s) passed.\n`);
