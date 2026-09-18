"use strict";

const FIELDS = ["enabled", "showBlue", "showGrey", "patterns", "allSites", "vendorSites"];

/**
 * Supplier catalogue pages. Kept behind an explicit opt-in: showing a verdict
 * on a vendor's own product page is useful at the point of purchase, but it is
 * a deliberate choice the reader makes rather than something we switch on for
 * them at install time.
 *
 * Read from the manifest so the list exists in exactly one place — what we ask
 * for can never drift from what we declare. The all-sites pattern lives in the
 * same manifest key but is a separate, separately-granted setting, so it is
 * filtered out here: ticking "supplier sites" must never request everything.
 */
const ALL_SITES = ["*://*/*"];
const VENDOR_HOSTS = (chrome.runtime.getManifest().optional_host_permissions || [])
  .filter((h) => !ALL_SITES.includes(h));

const VENDOR_SCRIPT_ID = "oga-vendor-sites";
const ALL_SITES_SCRIPT_ID = "oga-all-sites";

function setStatus(msg, ms = 2200) {
  const el = document.getElementById("status");
  el.textContent = msg;
  if (ms) setTimeout(() => { el.textContent = ""; }, ms);
}

/**
 * Call a browser API without caring whether it answers with a promise or a
 * callback, and without ever throwing.
 *
 * Chrome and Firefox do not agree on which of the two `chrome.*` uses for which
 * API, and a mismatch here is invisible: the call rejects, the click handler
 * unwinds, and the settings write that came after it never happens. The Save
 * button appears to do nothing at all. Never let a permissions or scripting
 * call decide whether a preference is remembered.
 */
function call(fn, arg) {
  return new Promise((resolve) => {
    let settled = false;
    const ok = (value) => { if (!settled) { settled = true; resolve({ ok: true, value }); } };
    const bad = (error) => { if (!settled) { settled = true; resolve({ ok: false, error }); } };
    // Some paths answer neither way; a stuck settings page is worse than a
    // reported failure.
    setTimeout(() => bad(new Error("timed out")), 5000);
    try {
      const out = fn(arg, ok);
      if (out && typeof out.then === "function") out.then(ok, bad);
    } catch (error) {
      bad(error);
    }
  });
}

/**
 * Everything, on request. Institutional proxies rewrite the hostname of every
 * journal — nature.com becomes nature-com.ezproxy.<institution> — so no list of
 * publisher domains can reach them. This is the only way to cover that, which
 * is why it exists; it stays opt-in and off by default.
 */
async function registerScript(id, matches) {
  const existing = await call((a, cb) => chrome.scripting.getRegisteredContentScripts(a, cb), { ids: [id] });
  if (existing.ok && existing.value && existing.value.length) return { ok: true };

  const res = await call((a, cb) => chrome.scripting.registerContentScripts(a, cb), [{
    id,
    matches,
    js: ["src/matcher.js", "src/card.js", "src/content.js"],
    css: ["src/content.css"],
    runAt: "document_idle",
  }]);
  if (!res.ok) return res;

  // Ask the browser what it actually holds rather than trusting the call to
  // have done what it said. This is the one setting whose effect is invisible
  // until you happen to visit a supplier page, so "it registered" has to mean
  // the browser agrees, not that no error was thrown.
  const confirm = await call((a, cb) => chrome.scripting.getRegisteredContentScripts(a, cb), { ids: [id] });
  if (confirm.ok && confirm.value && confirm.value.length) return { ok: true };
  return { ok: false, error: new Error("the browser did not keep the registration") };
}

function unregisterScript(id) {
  return call((a, cb) => chrome.scripting.unregisterContentScripts(a, cb), { ids: [id] });
}

async function load() {
  const settings = await chrome.runtime.sendMessage({ type: "oga:get-settings" });
  for (const key of FIELDS) {
    document.getElementById(key).checked = Boolean(settings[key]);
  }
  const { indexFetchedAt } = await chrome.storage.local.get("indexFetchedAt");
  if (indexFetchedAt) {
    document.getElementById("stamp").textContent =
      "Characterisation data last refreshed " + new Date(indexFetchedAt).toLocaleString();
  }
}

/**
 * Turn one site-access setting on or off.
 *
 * permissions.request has to be called synchronously from the click, so this
 * runs before anything is awaited. Returns what was actually achieved, which is
 * not always what was asked for -- the user can decline the browser's prompt.
 */
function requestSiteAccess(wanted, origins, scriptId) {
  if (!wanted) {
    return { granted: false, work: (async () => {
      await unregisterScript(scriptId);
      await call((a, cb) => chrome.permissions.remove(a, cb), { origins });
      return { ok: true };
    })() };
  }
  const pending = call((a, cb) => chrome.permissions.request(a, cb), { origins });
  return { granted: true, work: (async () => {
    const res = await pending;
    if (!res.ok) return { ok: false, error: res.error };
    if (res.value === false) return { ok: false, declined: true };
    return registerScript(scriptId, origins);
  })() };
}

document.getElementById("save").addEventListener("click", async () => {
  const wanted = {};
  for (const key of FIELDS) wanted[key] = document.getElementById(key).checked;

  // Both requests are started here, still inside the click, because Firefox
  // refuses a permissions prompt raised after an await.
  const all = requestSiteAccess(wanted.allSites, ALL_SITES, ALL_SITES_SCRIPT_ID);
  const vendor = requestSiteAccess(wanted.vendorSites, VENDOR_HOSTS, VENDOR_SCRIPT_ID);

  const [allRes, vendorRes] = await Promise.all([all.work, vendor.work]);

  // Site access can fail; the preference itself is ours to keep either way, and
  // it is written before anything is allowed to report a problem.
  const settings = { ...wanted };
  if (wanted.allSites && !allRes.ok) settings.allSites = false;
  if (wanted.vendorSites && !vendorRes.ok) settings.vendorSites = false;

  const saved = await call((a, cb) => chrome.runtime.sendMessage(a, cb),
    { type: "oga:set-settings", settings });

  for (const key of FIELDS) document.getElementById(key).checked = Boolean(settings[key]);

  if (!saved.ok || !(saved.value && saved.value.ok)) {
    setStatus("Could not save — see the browser console.", 6000);
    console.error("[OGA] settings save failed:", saved.error || (saved.value && saved.value.error));
    return;
  }
  const notes = [];
  if (wanted.allSites && !allRes.ok) {
    notes.push(allRes.declined ? "running everywhere needs site access" : "site access failed");
    console.error("[OGA] all-sites permission:", allRes.error);
  }
  if (wanted.vendorSites && !vendorRes.ok) {
    notes.push(vendorRes.declined ? "supplier sites need site access" : "supplier site access failed");
    console.error("[OGA] vendor permission:", vendorRes.error);
  }
  setStatus(notes.length
    ? `Saved, but ${notes.join(" and ")}.`
    : "Saved. Reload any open pages to apply.", notes.length ? 6000 : 2200);
});

document.getElementById("refresh").addEventListener("click", async () => {
  setStatus("Refreshing…", 0);
  const resp = await chrome.runtime.sendMessage({ type: "oga:refresh-index" });
  setStatus(resp && resp.generated ? `Updated (data ${String(resp.generated).slice(0, 10)}).` : "Could not reach the portal — using the bundled copy.", 3500);
  const { indexFetchedAt } = await chrome.storage.local.get("indexFetchedAt");
  if (indexFetchedAt) {
    document.getElementById("stamp").textContent =
      "Characterisation data last refreshed " + new Date(indexFetchedAt).toLocaleString();
  }
});

load();
