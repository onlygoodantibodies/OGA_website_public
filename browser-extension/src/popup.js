"use strict";

function text(el, value) {
  document.getElementById(el).textContent = value;
}

/* One vocabulary across this panel and the hover card.
 *
 * This list said "not recommended" while the card said "not supportive", the
 * tiles four centimetres above it said NOT SUPPORTIVE, and every page on the
 * site had moved off the word: OGA characterises antibodies, it does not
 * recommend or validate them, and a panel that recommends is making a different
 * and stronger claim than the data does. It was the last surface still saying
 * it, which is why it survived — nothing contradicted it except everything
 * around it.
 *
 * `not supportive` and `supports` are `card.js::CODE_LABEL`'s words. They are
 * not imported: the popup is its own document and loads only handoff.js and
 * this file, and pulling card.js in to read one table would ship the whole
 * hover card into a panel that never draws one. Kept honest by
 * `popup.test.mjs`, which reads both files and compares them.
 */
function concernLine(c) {
  const name = `<strong>${c.name}</strong>`;
  // A declared-target notice is level `red` and is not a result. Nothing about
  // it may read as a verdict: OGA has tested none of these products.
  if (c.kind === "wrong-target") {
    return `<li>${name} — an antibody to ${c.declared}, not to `
      + `${c.mistakenFor}. Not an OGA result; open the mark for the review.</li>`;
  }
  const failed = (c.failed || []).join(", ");
  const passed = (c.passed || []).join(", ");
  // Both halves on a split verdict, in the card's own order. Printing only the
  // failures turns "supports IP, not supportive for WB" into a flat negative.
  if (passed && failed) {
    return `<li>${name}${c.gene ? ` (${c.gene})` : ""} — supports ${passed}, `
      + `not supportive for ${failed}</li>`;
  }
  return `<li>${name}${c.gene ? ` (${c.gene})` : ""} — not supportive`
    + `${failed ? ` for ${failed}` : ""}</li>`;
}

function renderConcerns(concerns) {
  const host = document.getElementById("concerns");
  if (!concerns || !concerns.length) {
    host.innerHTML = `<p class="empty">Nothing on this page is marked not supportive for the application it was used in.</p>`;
    return;
  }
  host.innerHTML = `<ul>${concerns.map(concernLine).join("")}</ul>`;
}

/**
 * How many of the red marks are not a result at all.
 *
 * The red tile's face says "not supportive" and a declared-target notice is
 * level red, so a page citing one of the eleven products and nothing else drew
 * `1 NOT SUPPORTIVE` where OGA has tested nothing. The tile's tooltip names
 * both meanings of red; a tooltip is not the label, and a count is a claim
 * about the page.
 *
 * Hidden at zero rather than drawn empty — on almost every page this is zero,
 * and a blank line under the tally reads as a caveat that failed to load.
 */
function renderNotices(count) {
  const el = document.getElementById("notices");
  if (!el) return;
  if (!count) { el.hidden = true; el.textContent = ""; return; }
  const many = count !== 1;
  el.textContent = `${count} of the red ${many ? "marks are" : "marks is"} a `
    + `declared-target notice — the product is documented as an antibody to a `
    + `different protein. Not an OGA test result.`;
  el.hidden = false;
}

function render(summary) {
  const counts = summary.counts || {};
  for (const level of ["green", "yellow", "red", "mixed", "blue", "grey"]) {
    text("c-" + level, counts[level] || 0);
  }
  renderConcerns(summary.concerns);
  renderNotices(summary.notices || 0);
  renderHandoff(summary);
  if (summary.indexGenerated) {
    text("stamp", "data " + String(summary.indexGenerated).slice(0, 10));
  }
  // Shown only when the index actually supplied it: an empty line under the
  // tally reads as a caveat that failed to load, which is worse than none.
  const scope = document.getElementById("scope");
  if (scope) {
    // Composed from the two served strings, never a bundled sentence: the card
    // binds `conditions` inside a verdict, and a panel of counts has no verdict
    // to bind it to, so it is restated as the lead of the caveat. "Results" is
    // the only word here that is not served.
    const lead = summary.conditions ? `Results ${summary.conditions}. ` : "";
    scope.textContent = summary.scope ? lead + summary.scope : "";
    scope.hidden = !summary.scope;
  }
}

/**
 * Hand off to the MCP server. Deliberately makes no claim about this page —
 * the extension stopped guessing at controls in 0.1.7 and must not imply it
 * still knows. All this does is put the paper on the clipboard.
 */
function renderHandoff(summary) {
  const ref = summary.doi || summary.url;
  if (!ref) return;
  const section = document.getElementById("handoff");
  const button = document.getElementById("copyref");
  // One source for the claim and the destination — see src/handoff.js. The
  // card says the same thing about the same two questions.
  const H = globalThis.OGAHandoff;
  if (H) {
    document.getElementById("handoff-why").textContent = H.WHY;
    const link = document.getElementById("handoff-link");
    link.href = H.URL;
    link.textContent = H.LABEL;
  }
  section.hidden = false;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(ref);
      button.textContent = summary.doi ? "DOI copied" : "Link copied";
    } catch (_) {
      // A copy that silently failed reads as a copy that worked, and the reader
      // finds out by pasting nothing into the chat.
      button.textContent = "Could not copy";
    }
    setTimeout(() => { button.textContent = "Copy paper reference"; }, 2000);
  });
}

for (const id of ["options", "opensettings"]) {
  document.getElementById(id).addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });
}

/**
 * No content script answered, so this page was never looked at.
 *
 * Told apart from a page with no antibodies on it by whether the tab replies
 * at all — never by a mark count of zero, which is the correct answer on a
 * page that cites nothing characterised. Showing five zeroes over a page
 * nobody scanned is the silence the benchmark measured: on a default install
 * roughly one paper in three from that corpus produced no marks, no
 * explanation, and nothing to distinguish it from a clean result.
 */
function showNotRunning() {
  document.getElementById("scanned").hidden = true;
  document.getElementById("notrunning").hidden = false;
}

/**
 * Clicking a tile walks through that colour's marks on the page. Repeated
 * clicks advance and wrap; tiles with a count of zero are disabled rather than
 * silently doing nothing.
 */
function wireTiles(tabId, counts) {
  const hint = document.getElementById("hint");
  for (const button of document.querySelectorAll(".t[data-level]")) {
    const level = button.dataset.level;
    const total = counts[level] || 0;
    button.disabled = total === 0;
    if (total === 0) continue;

    button.addEventListener("click", () => {
      chrome.tabs.sendMessage(tabId, { type: "oga:focus", level }, (res) => {
        if (chrome.runtime.lastError || !res || !res.count) return;
        hint.textContent = `${tileName(button, level)} — ${res.at} of ${res.count}`;
      });
    });
  }
}

/**
 * What to call a level in the hint: the tile's own visible label.
 *
 * There was a second table here — `LEVEL_NAME` — and it was wrong twice over.
 * It said "Recommended" and "Not recommended" where the tile beneath the
 * cursor said SUPPORTS and NOT SUPPORTIVE and the hover card said supportive
 * and not supportive, so clicking a tile renamed its own verdict. And it had no
 * `yellow` at all, the rung 0.3.1 was released for, so clicking the
 * limited-support tile wrote `undefined — 1 of 2` into the hint. The same
 * omission as `content.js::LEVEL_CLASS`, in the same release, one table over.
 *
 * Reading the label off the button removes both by construction: there is one
 * string per level, in popup.html, and it is the one the reader just clicked.
 * A level added later needs nothing here. The fallback is the level's own name,
 * which is a poor word and still not `undefined`.
 */
function tileName(button, level) {
  const label = button.querySelector("span");
  const text = label && label.textContent.trim();
  if (!text) return level;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const tab = tabs[0];
  if (!tab || !tab.id) return showNotRunning();
  chrome.tabs.sendMessage(tab.id, { type: "oga:get-summary" }, (summary) => {
    // lastError is what a tab with no content script in it looks like. Reading
    // it also clears it, which is why it is checked before anything else.
    if (chrome.runtime.lastError || !summary) return showNotRunning();
    render(summary);
    wireTiles(tab.id, summary.counts || {});
  });
});
