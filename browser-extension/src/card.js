/**
 * The hover card. Rendered into a shadow root so the publisher's stylesheet
 * cannot alter it and ours cannot leak onto their page.
 */
(function (root) {
  "use strict";

  const PORTAL = "https://onlygoodantibodies.co.uk";
  const APP_LABEL = { WB: "Western blot", IP: "Immunoprecipitation", IF: "Immunofluorescence", FC: "Flow cytometry" };
  const CODE_CLASS = { 0: "u", 1: "n", 2: "r" };
  const CODE_LABEL = { 0: "not tested", 1: "not recommended", 2: "recommended" };

  let host = null;
  let shadow = null;
  let hideTimer = null;
  let currentAnchor = null;

  /**
   * Which application's data the card is showing, and which hit it belongs to.
   *
   * These live at module scope because there is ONE card, reused for every mark
   * on the page, and `show()` rewrites its innerHTML on every mouseover. A
   * selection held inside render() would be destroyed by the next mouse move —
   * the tab would appear to work and then silently reset, which is worse than
   * having no tabs.
   *
   * `currentKey` is what decides "same hit": moving to a different antibody must
   * start again at that antibody's own default rather than inheriting whichever
   * tab was open on the last one.
   */
  let currentHit = null;
  let currentKey = null;
  let selectedApp = null;

  function ensureHost() {
    if (host && host.isConnected) return;
    host = document.createElement("div");
    host.className = "oga-card-host";
    // The card must sit above publisher chrome but must not affect layout.
    host.style.cssText = "position:absolute;top:0;left:0;width:0;height:0;z-index:2147483647;";
    shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `<style>${CARD_CSS}</style><div class="card" part="card"></div>`;
    document.documentElement.appendChild(host);

    const card = shadow.querySelector(".card");
    card.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    card.addEventListener("mouseleave", scheduleHide);

    // ONE delegated listener, bound here with the host rather than per render.
    // Re-binding on every mouseover would stack a listener per hover.
    card.addEventListener("click", (e) => {
      const tab = e.target.closest && e.target.closest("[data-tab]");
      if (!tab || !currentHit) return;
      selectedApp = tab.getAttribute("data-tab");
      // Redrawn in place and deliberately NOT repositioned: the card is under
      // the pointer, and moving it would fire the mark's mouseout and hide the
      // thing the reader just clicked.
      card.innerHTML = render(currentHit);
    });
  }

  /** "1 application" / "3 applications". Verbs agree with counts, and counts
   *  are often 1. */
  function plural(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /**
   * The four applications, as tabs.
   *
   * They were four read-only chips, which stated every verdict but let the
   * reader see the validation image for only one of them — whichever the card
   * happened to pick. The data for the other three was in the record and on no
   * screen. Now the chip IS the control: click it and the figure below is that
   * application's.
   *
   * `used` marks the applications this paper actually used the reagent for, and
   * the badge WORDING says how we know — a looked-up application is somebody
   * else's record of what the paper did, a proximity one is a guess off this
   * page, and a reader who cannot tell them apart cannot weigh either.
   */
  function appTabs(record, status, selected) {
    const used = status.mentioned || [];
    const lookedUp = status.via === "paper";
    return ["WB", "IP", "IF", "FC"].map((app) => {
      const code = record.a[app] ?? 0;
      const on = used.includes(app) ? " chip-on" : "";
      const here = app === selected ? " chip-sel" : "";
      const note = used.includes(app)
        ? (lookedUp ? " — used in this paper" : " — named on this page")
        : "";
      return `<button type="button" class="chip chip-${CODE_CLASS[code]}${on}${here}"
        data-tab="${app}" aria-pressed="${app === selected}"
        title="${esc(APP_LABEL[app])}: ${CODE_LABEL[code]}${esc(note)}">
        <b>${app}</b><i>${CODE_LABEL[code]}</i></button>`;
    }).join("");
  }

  /**
   * Which tab opens first.
   *
   * The application the PAPER used, when we know it — that is the question the
   * reader has. Falling back, in order, to whichever application has a figure to
   * show, because a tab opening on an empty panel wastes the one glance this
   * card gets.
   */
  function defaultApp(hit) {
    const s = hit.status;
    const rec = s.record;
    if (!rec) return null;
    const hasImage = (a) => rec.img && rec.img[a];
    return (s.scopedTo || []).find(hasImage)
      || (s.scopedTo || [])[0]
      || (s.mentioned || []).find(hasImage)
      || (s.passed || []).find(hasImage)
      || (s.failed || []).find(hasImage)
      || (rec.img ? Object.keys(rec.img)[0] : null)
      || (s.mentioned || [])[0]
      || "WB";
  }

  // headline() and explain() take the whole hit, not just the status: amber's
  // wording rests on how the target was attributed, which is a property of the
  // hit and deliberately not of the verdict.
  function headline(hit) {
    const status = hit.status;
    switch (status.level) {
      // Every headline now states the record, never the slice of it that matched
      // an inferred application. "Recommended for WB" over an antibody that fails
      // IP and IF was true of the page's guess and misleading about the reagent,
      // and the guess was wrong on 17 of the 54 papers it spoke about.
      case "green":
        return `Recommended for ${esc((status.passed || []).join(", "))}`;
      case "red":
        return `Tested and <em>not</em> recommended for ${esc((status.failed || []).join(", "))}`;
      case "mixed":
        return `Recommended for ${esc((status.passed || []).join(", "))} — `
          + `<em>not</em> recommended for ${esc((status.failed || []).join(", "))}`;
      case "amber":
        // Amber is an OFFER: knockout-controlled antibodies are recommended for
        // this target and the reader can go and use one. Leading with "Not in the
        // dataset" made a mark whose whole value is the alternative read as a
        // verdict on a reagent — and on OGA's own gene pages, where a phrase like
        // "10 APP antibodies" is descriptive text and not a product at all, it
        // read as an accusation about an antibody that does not exist.
        //
        // "read as" is still load-bearing. Amber is the only verdict the
        // extension infers rather than looks up, and it was 26% of every mark it
        // drew across the benchmark; the provenance line underneath says which
        // words it was read from.
        return `Characterised antibodies available for ${esc(status.gene)}`;
      default:
        return greyHeadline(status);
    }
  }

  /**
   * Grey must mean *we hold nothing on this reagent*.
   *
   * It also meant "we hold a failure you cannot see". On 7 of 95 benchmark
   * pages the target antibody was headed "No independent data" when it is in
   * the dataset and failed knockout-controlled testing — ab4193, ab15954,
   * 14060-1-AP, 870502 — because the page used it for IHC, or for an assessed
   * application with no result, and this switch fell through to its default.
   * The explain() line underneath was already correct, so the card contradicted
   * itself: a reader skimming an IHC paper was told there was no data on an
   * antibody with published failures.
   *
   * The trailing clause comes from the record, not from status.passed /
   * status.failed, which are empty on exactly these branches.
   */
  function greyHeadline(status) {
    const M = root.OGAMatcher;
    const { passed, failed } = M.recordVerdicts(status.record);
    if (!passed.length && !failed.length) {
      // The record genuinely holds no verdict anywhere, or there is no record.
      // This is the one case where the original wording is the true one.
      return "No independent data";
    }

    const used = (status.mentioned || []).join(", ");
    const lead = used
      ? `Not tested for ${esc(used)}`
      : `Not assessed for ${esc((status.unassessed || []).join(", "))}`;

    const clause = [];
    if (passed.length) clause.push(`recommended for ${esc(passed.join(", "))}`);
    if (failed.length) clause.push(`<em>not</em> recommended for ${esc(failed.join(", "))}`);

    // "but" says there is something to see. With a pass and a fail to report
    // there plainly is, and the word only gets in the way.
    const joiner = clause.length > 1 ? " — " : " — but ";
    return lead + joiner + clause.join(", ");
  }

  /**
   * What this PAGE told us, in plain words — a separate claim from the verdict.
   *
   * The re-run's finding was not that the detector is bad at one application; it
   * is that it is a western-blot detector. IHC was used in 25 papers and named in
   * none. So the card stops implying the page said something it did not: it says
   * outright which applications were mentioned, or that none were, and every
   * result is shown either way.
   */
  function explain(hit) {
    const status = hit.status;
    const named = [].concat(status.mentioned || [], status.unassessed || [])
      .map((a) => APP_LABEL[a] || a);

    // Anything the page was read as OTHER than western blot is a guess we know
    // to be poor: 3 of 15 immunofluorescence namings were right, and the three
    // immunoprecipitation namings were all on papers that never used it. Western
    // blot is 87% and gets no hedge — hedging the reliable signal alongside the
    // unreliable one teaches a reader to discount both.
    // A clause, not a sentence: it has to hang off the naming it doubts, or a
    // reader meets "Immunofluorescence." and has already believed it.
    //
    // Naming the application a second time when it is the only one named just
    // says it twice — "that reading" does the same work.
    // The remedy travels WITH the doubt. The hand-off under the chips says the
    // same thing, but a reader who has just been told to go and check needs the
    // route in that sentence, not two elements further down past the results.
    const H = root.OGAHandoff;
    const route = H
      ? ` <a href="${H.URL}" target="_blank" rel="noopener">More accurate matching via the AI connector</a>.`
      : "";

    const doubted = (status.uncertain || []).map((a) => APP_LABEL[a] || a);
    const shaky = !doubted.length ? "."
      : doubted.length === named.length
        ? `, but that reading is unreliable — check what the paper actually used.${route}`
        : `, and the ${esc(doubted.join(" and "))} reading is unreliable — check what the paper actually used.${route}`;

    if (status.reason === "app-not-assessed") {
      const which = (status.unassessed || []).join(", ");
      return `This page mentions ${esc(which)}, which is not one of the four applications assessed under the consensus protocols — nothing below applies to it${shaky} Our results for every assessed application are shown; a result in one never carries over to another.`;
    }
    if (status.reason === "app-not-tested") {
      return `This page mentions ${esc(named.join(" and "))}, which has not been tested${shaky} Our results for every assessed application are shown below.`;
    }
    if (status.reason === "no-application") {
      return `This page doesn't say which application this antibody was used for — all our results are shown below.`;
    }
    if (status.reason === "mentioned") {
      return `This page mentions ${esc(named.join(" and "))}${shaky} Our results for every application are shown; the ones mentioned are marked.`;
    }
    if (status.level === "amber") {
      return amberExplain(hit);
    }
    if (status.level === "grey" && status.reason === "target-untested") {
      return `${esc(status.gene)} has not been characterised in this dataset yet. This says nothing about the antibody either way.`;
    }
    if (status.level === "grey") {
      return `No independent knockout-controlled data. This says nothing about the antibody either way.`;
    }
    return "";
  }

  /**
   * Amber's evidence, shown because it is worth seeing — not because it is
   * suspect.
   *
   * Amber is the one verdict read out of the page rather than looked up, so
   * naming the words it was read from lets a reader confirm it at a glance.
   * That is the whole purpose: transparency, not a warning.
   *
   * A mark-level audit of 123 marks measured this path directly — 0 of 45
   * amber marks had a wrong gene, and 43 of 45 took the target from an
   * explicit "anti-X" or "antibodies against X" phrase sitting inside the
   * marked span. The mis-attribution this copy was first written to defend
   * against did not occur once. So the provenance is stated plainly and the
   * claim is made outright: no "check it matches", no "not necessarily this
   * reagent's", and no conditional in front of the alternatives, which telling
   * a reader to distrust a reliable signal would only cost them.
   *
   * The caveat that *this* antibody is untested is a different claim and
   * stays, so the reader does not take "alternatives exist" as a result about
   * the reagent in front of them. What went with it on 7 Aug 2026 was the
   * explanation — untested is untested, and the sentence saying so does not
   * need a second one defending it.
   */
  function amberExplain(hit) {
    const gene = esc(hit.status.gene);
    // Page-derived text, so it is escaped like anything else off the document.
    const raw = hit.target && hit.target.raw ? esc(hit.target.raw) : null;

    let source;
    if (!raw) {
      source = `Target taken from this page's own text.`;
    } else if (hit.targetVia === "nearby") {
      // Still worth distinguishing — it is the one attribution nothing tied to
      // this identifier except proximity — but stated as a fact, not a caveat.
      source = `Target taken from &ldquo;${raw}&rdquo;, the closest target name in this block.`;
    } else if (hit.via === "target") {
      // The mark IS the target phrase, so there is no catalogue number here to
      // say which product it is. That is about the reagent, not the target.
      source = `Target taken from the name in this mention, &ldquo;${raw}&rdquo;; `
        + `no catalogue number is given for it.`;
    } else {
      source = `Target taken from &ldquo;${raw}&rdquo; next to this catalogue number.`;
    }

    // Availability first: it is what the mark is FOR and the only part a reader
    // can act on. The card still says the antibody itself is untested, which is
    // a different claim — but that is no longer the opening line of a card whose
    // reason for existing is that a characterised option exists.
    return `${gene} has knockout-controlled antibodies you can use instead. `
      + `${source} This exact antibody has not been tested.`;
  }

  function render(hit) {
    const s = hit.status;
    const rec = s.record;
    const parts = [];

    parts.push(`<div class="hd hd-${s.level}">
      <span class="dot"></span>
      <div class="hd-t">
        <strong>${esc(rec ? rec.n : (hit.matched || "").trim())}</strong>
        ${rec && rec.s ? `<span class="sup">${esc(rec.s)}</span>` : ""}
      </div>
    </div>`);

    parts.push(`<div class="verdict">${headline(hit)}</div>`);

    const why = explain(hit);
    if (why) parts.push(`<p class="why">${why}</p>`);

    if (rec) {
      const selected = selectedApp || defaultApp(hit);
      parts.push(`<div class="chips">${appTabs(rec, s, selected)}</div>`);

      // Applications the paper used it for that OGA does not test AT ALL. Named,
      // and kept out of the tab strip: there is no verdict behind them, and a
      // fifth tab that opens on "we have nothing" reads as a gap in the data
      // rather than as the boundary of what was ever claimed.
      // Applications the paper used it for that OGA does not test AT ALL. Named,
      // and kept out of the tab strip: there is no verdict behind them, and a
      // fifth tab that opens on "we have nothing" reads as a gap in the data
      // rather than as the boundary of what was ever claimed.
      //
      // NAMED IS NOT ENOUGH -- it has to say what IS known. "Used for IHC, no
      // verdict" leaves a reader with nothing; "used for IHC, no tissue verdict,
      // and it failed the three applications that were tested" is something they
      // can act on. Tissue is the usual case and the reason is worth stating:
      // OGA's IF result is ICC-IF, cultured cells, and whether an epitope
      // survives depends on how the antigen is presented -- so IHC-IF is tissue
      // despite its name, and a cultured-cell result does not carry over.
      const outside = s.untestedApplications || [];
      if (outside.length) {
        const tested = []
          .concat(s.passed || [], s.failed || [])
          .filter((a, i, all) => all.indexOf(a) === i);
        const known = (s.failed || []).length && !(s.passed || []).length
          ? `it was tested and <strong>not recommended</strong> in ${plural(s.failed.length, "application")} (${esc(s.failed.join(", "))})`
          : tested.length
            ? `it was tested in ${esc(tested.join(", "))} — see the tabs above`
            : "OGA has not tested it in any application either";
        parts.push(`<p class="outside">This paper also used it for
          ${esc(outside.join(", "))}, which OGA does not test — antigen
          presentation differs, so a cultured-cell result does not carry over to
          tissue. There is no verdict for that use. For context, ${known}.</p>`);
      }
      if (s.applicationAmbiguous) {
        parts.push(`<p class="outside">The citation record says immunofluorescence
          without saying whether that was cultured cells or tissue. OGA's IF result
          is cultured cells only, so it is shown but not applied to this use.</p>`);
      }

      // The hand-off sits under the chips, where the reader has just met the
      // limit it answers: either a marked chip that is only a guess from the
      // prose, or four unmarked ones because the page named nothing.
      //
      // It is placed here and not in the links row on purpose — a bare link
      // among "Report" and "Product page" would read as a fifth reference, and
      // this is the one line on the card that says what the extension CANNOT do.
      const H = root.OGAHandoff;
      if (H) {
        // When the status line above doubted a reading it already carried the
        // link, so this drops to the half that has not been said and does not
        // repeat the route.
        parts.push((s.uncertain || []).length
          ? `<p class="handoff">${esc(H.WHY_CONTROLS)}</p>`
          : `<p class="handoff">${esc(H.WHY)}
              <a href="${H.URL}" target="_blank" rel="noopener">${esc(H.LABEL)}</a></p>`);
      }

      const showApp = selected;
      if (showApp && rec.img && rec.img[showApp]) {
        parts.push(`<figure class="fig">
          <img src="${esc(rec.img[showApp])}" alt="${esc(APP_LABEL[showApp])} validation data" loading="lazy">
          <!-- Not "knockout vs wild-type": some targets are assessed against a
               knockdown, and that caption contradicted the figure's own legend.
               "Genetic control" covers both and is still the meaningful claim —
               it is what separates a selectivity control from a pseudo-control
               like peptide competition. The legend names the exact cell lines. -->
          <figcaption>${esc(APP_LABEL[showApp])} — genetic control vs wild-type</figcaption>
        </figure>`);
      } else if (showApp) {
        // An empty panel would look like a broken tab, and leaving the previous
        // application's figure up would caption one experiment with another's
        // name -- the worst of the three options by some distance.
        parts.push(`<p class="nofig">No published ${esc(APP_LABEL[showApp])} figure
          for this antibody. ${esc(CODE_LABEL[rec.a[showApp] ?? 0] === "not tested"
            ? "OGA has not tested it in this application."
            : "The verdict above comes from the report.")}</p>`);
      }

      const meta = [];
      // Named first when that is what was matched: the header shows the
      // catalogue number, so hovering "D11A10" would otherwise produce a card
      // headed "4436" with nothing on it tying the two together.
      if (rec.cl) meta.push(`clone ${esc(rec.cl)}`);
      if (rec.h) meta.push(esc(rec.h));
      if (rec.c) meta.push(esc(rec.c));
      if (hit.rrid) meta.push(`RRID ${esc(hit.rrid)}`);
      if (rec.d) meta.push(`<span class="disc">discontinued</span>`);
      if (meta.length) parts.push(`<div class="meta">${meta.join(" · ")}</div>`);

      parts.push(`<div class="prov">${rec.ind === false
        ? `Knockout validation contributed by ${esc(rec.src || "the supplier")}`
        : `Independently tested by ${esc(rec.src || "YCharOS")} with knockout controls`}</div>`);

      const links = [`<a href="${PORTAL}/antibodies/${encodeURIComponent(rec.g)}/" target="_blank" rel="noopener">All ${esc(rec.g)} antibodies</a>`];
      if (rec.doi) links.push(`<a href="${esc(rec.doi)}" target="_blank" rel="noopener">Report</a>`);
      if (rec.p) links.push(`<a href="${esc(rec.p)}" target="_blank" rel="noopener">Product page</a>`);
      if (hit.rrid) links.push(`<a href="https://www.antibodyregistry.org/${esc(hit.rrid)}" target="_blank" rel="noopener">RRID</a>`);
      parts.push(`<div class="links">${links.join("")}</div>`);
    } else if (s.level === "amber") {
      parts.push(`<div class="links">
        <a href="${PORTAL}/antibodies/${encodeURIComponent(s.gene)}/" target="_blank" rel="noopener">See tested ${esc(s.gene)} antibodies</a>
      </div>`);
    } else {
      parts.push(`<div class="links">
        <a href="${PORTAL}/" target="_blank" rel="noopener">Search the portal</a>
        <a href="${PORTAL}/contact/" target="_blank" rel="noopener">Nominate this target</a>
      </div>`);
    }

    return parts.join("");
  }

  function position(anchor) {
    const card = shadow.querySelector(".card");
    const r = anchor.getBoundingClientRect();
    const sx = window.scrollX;
    const sy = window.scrollY;

    card.style.visibility = "hidden";
    card.style.display = "block";
    const cw = card.offsetWidth;
    const ch = card.offsetHeight;

    let left = sx + r.left;
    let top = sy + r.bottom + 8;

    // Flip above the mention if there is not room below.
    if (r.bottom + ch + 16 > window.innerHeight && r.top - ch - 8 > 0) {
      top = sy + r.top - ch - 8;
    }
    // Keep inside the viewport horizontally.
    const maxLeft = sx + window.innerWidth - cw - 12;
    if (left > maxLeft) left = Math.max(sx + 12, maxLeft);

    card.style.left = left + "px";
    card.style.top = top + "px";
    card.style.visibility = "visible";
  }

  function show(anchor, hit) {
    ensureHost();
    clearTimeout(hideTimer);
    currentAnchor = anchor;
    const key = `${hit.rrid || hit.matched}|${hit.start}`;
    if (key !== currentKey) {
      currentKey = key;
      selectedApp = null;      // a new antibody opens on its OWN default
    }
    currentHit = hit;
    const card = shadow.querySelector(".card");
    card.innerHTML = render(hit);
    card.style.display = "block";
    position(anchor);
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 180);
  }

  function hide() {
    if (!shadow) return;
    shadow.querySelector(".card").style.display = "none";
    currentAnchor = null;
  }

  const CARD_CSS = `
  :host { all: initial; }
  .card {
    display: none; position: absolute; box-sizing: border-box;
    width: 330px; max-width: calc(100vw - 24px);
    font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #16202b; background: #fff; border: 1px solid #d3dbe4; border-radius: 10px;
    box-shadow: 0 10px 34px rgba(16,32,48,.20); padding: 12px 13px; text-align: left;
  }
  .card * { box-sizing: border-box; }
  .hd { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 7px; }
  .hd .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 4px; flex: 0 0 auto; }
  .hd-green .dot { background: #1c8f4b; }
  .hd-red   .dot { background: #c62f2f; }
  .hd-mixed .dot { background: linear-gradient(90deg,#1c8f4b 50%,#c62f2f 50%); }
  .hd-amber .dot { background: #d98217; }
  .hd-grey  .dot { background: #9aa6b2; }
  .hd-t strong { font-size: 14px; font-weight: 650; }
  .hd-t .sup { display: block; color: #5d6b7a; font-size: 11.5px; }
  .verdict { font-weight: 600; margin-bottom: 5px; }
  .hd-green ~ .verdict { color: #14713b; }
  .why { margin: 0 0 8px; color: #4b5866; font-size: 12px; }
  .chips { display: grid; grid-template-columns: repeat(4,1fr); gap: 4px; margin-bottom: 9px; }
  .chip { border-radius: 6px; padding: 4px 3px; text-align: center; border: 1px solid transparent; }
  .chip b { display: block; font-size: 11px; font-weight: 700; }
  .chip i { display: block; font-style: normal; font-size: 9px; letter-spacing: .01em; }
  .chip-r { background: #e6f5ec; color: #14713b; border-color: #b7e0c6; }
  .chip-n { background: #fdeaea; color: #a82525; border-color: #f3c4c4; }
  .chip-u { background: #f1f4f7; color: #77838f; border-color: #e0e6ec; }
  .chip { font: inherit; cursor: pointer; }
  .chip:focus-visible { outline: 2px solid #12639e; outline-offset: 1px; }
  /* Named on the page / used in this paper. */
  .chip-on { outline: 2px solid #16202b; outline-offset: -2px; }
  /* Which tab is open. Distinct from chip-on, which says something about the
     PAPER: one is what the reader chose, the other is what the record says. */
  .chip-sel { box-shadow: inset 0 -3px 0 #16202b; font-weight: 700; }
  .outside { margin: -4px 0 8px; color: #4b5866; font-size: 11.5px; line-height: 1.45; }
  .nofig { margin: 0 0 8px; color: #5d6b7a; font-size: 11.5px; line-height: 1.45;
           background: #f6f8fa; border: 1px solid #e2e8ee; border-radius: 6px; padding: 7px 8px; }
  .fig { margin: 0 0 8px; }
  .fig img { width: 100%; border-radius: 6px; border: 1px solid #e2e8ee; display: block; background: #fafcfe; }
  .fig figcaption { color: #5d6b7a; font-size: 11px; margin-top: 3px; }
  .meta { color: #5d6b7a; font-size: 11.5px; margin-bottom: 6px; }
  .meta .disc { color: #a82525; }
  .handoff { margin: 8px 0 0; color: #4b5866; font-size: 11.5px; line-height: 1.45; }
  .handoff a { white-space: nowrap; }
  .prov { color: #45525f; font-size: 11.5px; border-top: 1px solid #eef2f6; padding-top: 7px; margin-bottom: 7px; }
  .links { display: flex; flex-wrap: wrap; gap: 4px 10px; }
  .links a { color: #12639e; text-decoration: none; font-size: 12px; font-weight: 550; }
  .links a:hover { text-decoration: underline; }
  @media (prefers-color-scheme: dark) {
    .card { background: #1b2027; color: #e7edf3; border-color: #333d48; box-shadow: 0 10px 34px rgba(0,0,0,.5); }
    .hd-t .sup, .why, .meta, .prov, .fig figcaption { color: #9fadbb; }
    .chip-r { background: #123020; color: #7ddba3; border-color: #1e5335; }
    .chip-n { background: #3a1a1a; color: #f2a3a3; border-color: #5c2a2a; }
    .chip-u { background: #262d36; color: #94a2b0; border-color: #333d48; }
    .chip-on { outline-color: #e7edf3; }
    .chip-sel { box-shadow: inset 0 -3px 0 #e7edf3; }
    .outside { color: #9fadbb; }
    .nofig { color: #9fadbb; background: #222a33; border-color: #333d48; }
    .prov { border-top-color: #2c343d; }
    .links a { color: #6fb6ec; }
    .fig img { border-color: #333d48; background: #11151a; }
  }`;

  root.OGACard = { show, hide, scheduleHide, get anchor() { return currentAnchor; } };
})(typeof globalThis !== "undefined" ? globalThis : self);
