/**
 * Service worker: owns the index and the settings.
 *
 * The index ships with the extension so a fresh install works offline and
 * instantly. It is then refreshed from the portal on a timer. The refresh is
 * the only outbound request the extension makes on its own, and it carries no
 * information about what the reader is looking at.
 */

const INDEX_URL = "https://onlygoodantibodies.co.uk/extension/index.json";
const BUNDLED_INDEX = "data/index.json";
const REFRESH_ALARM = "oga-refresh";
const REFRESH_MINUTES = 60 * 24; // daily

/**
 * The citation snapshot: which published papers used which of our antibodies,
 * and for what. It replaces the extension's application GUESS with a lookup on
 * any paper the record covers -- the guess being measured bad away from western
 * blot (IF 20% correct, IP 0 of 3, IHC and FC never named).
 *
 * Three deliberate differences from the index:
 *
 *  - SEPARATE RESOURCE. index.json is downloaded daily by every installed
 *    extension, including versions that know nothing about citations, and both
 *    gates below check `schema === 1` -- so it can be neither enlarged for
 *    everyone nor version-bumped without stranding the field on the bundled
 *    fixture.
 *  - NOT BUNDLED. It is ~1.3 MB, and a fresh install with no copy simply behaves
 *    the way the extension behaved before this feature existed. That is a real
 *    fallback, not a broken state, so it is not worth 1.3 MB in a signed build.
 *  - HELD IN THE WORKER. The page gets at most ONE paper's worth of it, never the
 *    table. Nothing about what the reader is looking at leaves the browser either
 *    way; this is about not structured-cloning a megabyte into every tab.
 */
const CITATIONS_URL = "https://onlygoodantibodies.co.uk/extension/citations.json";

const DEFAULT_SETTINGS = {
  enabled: true,
  showAmber: true,
  showGrey: true,
  patterns: false,      // colour-blind friendly line styles
  allSites: false,      // opt-in: needed for institutional proxy hostnames
  vendorSites: false,   // opt-in: also run on supplier catalogue pages
};

let cached = null;
let cachedCitations = null;

async function loadBundled() {
  const resp = await fetch(chrome.runtime.getURL(BUNDLED_INDEX));
  return resp.json();
}

async function getIndex() {
  if (cached) return cached;
  const stored = await chrome.storage.local.get("index");
  if (stored.index && stored.index.schema === 1) {
    cached = stored.index;
    return cached;
  }
  cached = await loadBundled();
  return cached;
}

/**
 * The citation table, or null.
 *
 * `null` means "we have no table", which the caller must report as *could not be
 * checked* -- never as *this paper is not in the record*. Those are different
 * sentences and only one of them is a fact about the paper.
 */
async function getCitations() {
  if (cachedCitations !== null) return cachedCitations;
  const stored = await chrome.storage.local.get("citations");
  if (stored.citations && stored.citations.schema === 1) {
    cachedCitations = stored.citations;
    return cachedCitations;
  }
  return null;
}

/**
 * Find one paper in the citation table.
 *
 * DELIBERATELY KNOWS NOTHING ABOUT STRINGS. Every key it is handed -- the PubMed
 * id, the DOI, the hashed title -- was computed by the content script from
 * src/paper.js, which is the single copy of those rules. All this does is three
 * dictionary lookups in priority order, so there is no second normaliser here to
 * drift out of step with the builder that wrote the table.
 *
 * It runs in the worker rather than the page because the table is ~1.3 MB and
 * would otherwise be structured-cloned into every tab. What crosses the boundary
 * is one paper's packed record plus the reagent and token dictionaries.
 *
 * The year confirmation is NOT done here. It belongs with the code that knows
 * why a title match needs confirming and a PubMed id does not -- paper.js.
 *
 * (paper.js is not imported here on purpose, and it was tried: MV3 refuses
 * importScripts even for a file present in the package, and a module worker did
 * not attach it either. A worker that throws on its first statement registers
 * with NO listeners, so every page silently stops being marked and nothing says
 * why -- found by driving a real browser, invisible to all three node suites.)
 */
function findPaper(table, keys) {
  if (!table || !table.papers || !table.rrids) {
    return { status: "unavailable", reason: "no-citation-table" };
  }
  if (!keys) return { status: "unavailable", reason: "no-page-keys" };
  const order = [["pmid", table.by_pmid, keys.pmid],
                 ["doi", table.by_doi, keys.doi],
                 ["title", table.by_title, keys.titleKey]];
  for (const [matchedOn, lookup, value] of order) {
    if (!lookup || !value) continue;
    const slot = lookup[value];
    if (slot === undefined) continue;
    return {
      status: "covered", matchedOn,
      year: (table.years || [])[slot] || null,
      packed: table.papers[slot],
      rrids: table.rrids,
      tokens: table.tokens || [],
      source: table.source || null,
    };
  }
  return { status: "not-covered" };
}

async function refreshCitations() {
  try {
    const resp = await fetch(CITATIONS_URL, { cache: "no-cache" });
    // A 404 is the ordinary state before the first snapshot is deployed, not an
    // error worth discarding a good stored copy over.
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const fresh = await resp.json();
    if (fresh && fresh.schema === 1 && fresh.papers && fresh.rrids) {
      cachedCitations = fresh;
      await chrome.storage.local.set({ citations: fresh, citationsFetchedAt: Date.now() });
    }
  } catch (err) {
    // Keeping the stored copy is right: a stale citation record is still a
    // correct record of older papers, and losing it would silently turn every
    // looked-up application back into a guess.
    console.debug("[OGA] citation refresh failed:", err.message);
  }
}

async function refreshIndex() {
  try {
    const resp = await fetch(INDEX_URL, { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const fresh = await resp.json();
    if (fresh && fresh.schema === 1 && fresh.antibodies) {
      cached = fresh;
      await chrome.storage.local.set({ index: fresh, indexFetchedAt: Date.now() });
    }
  } catch (err) {
    // Stale data is fine; the bundled copy is always a valid fallback.
    console.debug("[OGA] index refresh failed:", err.message);
  }
}

/**
 * Settings live in sync storage so they follow the reader between machines, and
 * are mirrored to local storage as a fallback.
 *
 * storage.sync is the more fragile of the two -- it needs an add-on ID, it has
 * quotas, and it can fail for reasons that have nothing to do with us. A
 * preference that silently fails to persist is indistinguishable from a broken
 * Save button, so the mirror is what guarantees the setting survives.
 */
async function getSettings() {
  let synced = {};
  try {
    synced = (await chrome.storage.sync.get("settings")).settings || {};
  } catch (err) {
    console.debug("[OGA] storage.sync unavailable:", err.message);
  }
  let local = {};
  try {
    local = (await chrome.storage.local.get("settings")).settings || {};
  } catch (err) {
    console.debug("[OGA] storage.local unavailable:", err.message);
  }
  return { ...DEFAULT_SETTINGS, ...local, ...synced };
}

async function setSettings(settings) {
  const results = await Promise.allSettled([
    chrome.storage.local.set({ settings }),
    chrome.storage.sync.set({ settings }),
  ]);
  // One surviving store is enough; both failing is a real error worth surfacing.
  if (results.every((r) => r.status === "rejected")) {
    throw results[0].reason || new Error("could not write settings");
  }
}

/**
 * Refresh now if the stored copy has aged past the refresh interval.
 *
 * The alarm alone is not enough. chrome.alarms.create replaces any alarm of the
 * same name and restarts its clock, and we recreate it on every browser start —
 * so anyone who closes their browser overnight resets the 24-hour timer each
 * morning and it never fires. Their copy would sit on the snapshot it was
 * installed with indefinitely, looking no different from an up-to-date one.
 */
async function refreshIfStale() {
  const { indexFetchedAt } = await chrome.storage.local.get("indexFetchedAt");
  if (!indexFetchedAt || Date.now() - indexFetchedAt >= REFRESH_MINUTES * 60 * 1000) {
    await refreshIndex();
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: REFRESH_MINUTES });
  refreshIndex();
  refreshCitations();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: REFRESH_MINUTES });
  refreshIfStale();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === REFRESH_ALARM) {
    refreshIndex();
    refreshCitations();
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (!msg) return false;

  if (msg.type === "oga:get-index") {
    // `msg.keys` is what the content script computed from the page -- the PubMed
    // id, the DOI and the HASHED title. The hashing happens there because that is
    // where src/paper.js lives; the lookup happens here so the ~1.3 MB table
    // stays in the worker and only one paper's record crosses into the tab.
    Promise.all([getIndex(), getSettings(), getCitations()])
      .then(([index, settings, citations]) => respond({
        index,
        settings,
        paper: findPaper(citations, msg.keys),
      }))
      .catch(() => respond(null));
    return true; // async
  }

  if (msg.type === "oga:get-settings") {
    getSettings().then(respond);
    return true;
  }

  if (msg.type === "oga:set-settings") {
    setSettings(msg.settings)
      .then(() => respond({ ok: true }))
      .catch((err) => respond({ ok: false, error: String(err && err.message || err) }));
    return true;
  }

  if (msg.type === "oga:refresh-index") {
    Promise.all([refreshIndex(), refreshCitations()])
      .then(() => Promise.all([getIndex(), getCitations()]))
      .then(([index, citations]) => respond({
        ok: true,
        generated: index.generated,
        // Named apart from the index's own date: they are refreshed together but
        // are snapshots of different things, and a reader owed a caveat about how
        // old the citation record is cannot get it from the index's date.
        citationsThrough: citations && citations.source
          ? citations.source.citeab_data_through : null,
      }));
    return true;
  }

  return false;
});
