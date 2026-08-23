"use strict";

function text(el, value) {
  document.getElementById(el).textContent = value;
}

function renderConcerns(concerns) {
  const host = document.getElementById("concerns");
  if (!concerns || !concerns.length) {
    host.innerHTML = `<p class="empty">Nothing on this page is flagged as not recommended for the application it was used in.</p>`;
    return;
  }
  const items = concerns.map((c) => {
    const apps = (c.failed || []).join(", ");
    return `<li><strong>${c.name}</strong>${c.gene ? ` (${c.gene})` : ""} — not recommended${apps ? ` for ${apps}` : ""}</li>`;
  }).join("");
  host.innerHTML = `<ul>${items}</ul>`;
}

function render(summary) {
  const counts = summary.counts || {};
  for (const level of ["green", "red", "mixed", "amber", "grey"]) {
    text("c-" + level, counts[level] || 0);
  }
  renderConcerns(summary.concerns);
  renderHandoff(summary);
  if (summary.indexGenerated) {
    text("stamp", "data " + String(summary.indexGenerated).slice(0, 10));
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
        hint.textContent = `${LEVEL_NAME[level]} — ${res.at} of ${res.count}`;
      });
    });
  }
}

const LEVEL_NAME = {
  green: "Recommended", red: "Not recommended", mixed: "Split verdict",
  amber: "Alternatives exist", grey: "Application untested",
};

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
