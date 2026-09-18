/**
 * The hover card. Rendered into a shadow root so the publisher's stylesheet
 * cannot alter it and ours cannot leak onto their page.
 */
(function (root) {
  "use strict";

  const PORTAL = "https://onlygoodantibodies.co.uk";
  const APP_LABEL = { WB: "Western blot", IP: "Immunoprecipitation", IF: "Immunofluorescence", FC: "Flow cytometry" };
  const CODE_CLASS = { 0: "u", 1: "n", 2: "r" };
  /* The site's own words for the three codes (`core/recommendations.py`). OGA
     characterises antibodies; it does not validate them, and a gene page is
     headed "characterisation data" — so the answer describes evidence rather
     than issuing advice. The CODES themselves do not move: this file is read by
     an index every install re-downloads daily, and 0/1/2 is the contract. */
  const CODE_LABEL = { 0: "not tested", 1: "not supportive", 2: "supportive" };
  /* The middle rung. A negative the data still says something on-target about
     is its own degree of support, not "not supportive with a footnote" — it
     reads as how much weight the evidence carries rather than as a fault in the
     antibody (owner, 29 Aug 2026). The CODES do not move: `a` is still 0/1/2,
     and this is display only, which is what makes a fourth rung safe. */
  const LIMITED_LABEL = "limited support";

  /* The clause after the verdict, keyed by the two-letter code the index ships
     per application. `core/recommendations.py::QUALIFIER_CODES` is the other
     half of this table and `core/tests_extension_scope.py` pins that the two
     carry the same codes — a code this file cannot read draws nothing, and
     silently, which is the failure mode that pairing is for.

     The wording lives here rather than in the index because that file is a
     megabyte before gzip and is fetched twice per install per day; two letters
     per qualified record is the part that has to travel. */
  const QUALIFIERS = {
    ns: "detects the target, but is not selective",
    sd: "detects the target",
    se: "enriches the target, but not significantly",
    si: "some selective signal",
    sl: "selective",
    xs: "strongly selective",
  };

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

  /**
   * What the verdicts do and do not cover.
   *
   * "Recommended" and "not recommended" are strong words for what is a
   * measurement, and on a card they sit two lines above a knockout blot with
   * nothing saying what conditions produced either. The words stay -- they are
   * what the consortium publishes and what the API's enum carries -- so the
   * nuance is carried beside them: one line, under the verdict, saying that
   * performance is protocol and sample dependent. It used to open "Result from
   * consensus protocols." as well; since 3 Sep 2026 CONDITIONS_FALLBACK says
   * that inside the verdict itself, where a reader who takes only the headline
   * takes it too.
   *
   * It ARRIVES WITH THE DATA (index.json's `scope_short`, built from
   * `core/recommendations.py::SCOPE_SHORT`) rather than living here, because
   * every other surface reads that constant and a bundled copy is the one that
   * goes stale -- the sentence has already been sharpened twice, and a store
   * review sits between a change here and a reader seeing it. Shipped in the
   * index it lands on every install at the next daily refresh.
   *
   * The fallback is for an install whose cached index predates the key. It is
   * deliberately the same words: a card that quietly said something weaker
   * until the next refresh would be the drift this arrangement exists to stop.
   */
  const SCOPE_FALLBACK = "Antibody performance is assay, protocol and sample dependent.";
  /**
   * Bound INSIDE the verdict, because the scope line underneath was not enough.
   *
   * 0.2.5 shipped "Tested and *not* recommended for WB" as its own sentence,
   * with the caveat below it. Two sentences, and the strong one stands alone —
   * a reader who takes only the headline takes an unqualified verdict, which is
   * the thing this was all meant to stop (owner, reading the packaged build).
   *
   * It read "in the conditions tested" until 3 Sep 2026, which qualified the
   * claim without saying whose conditions or which. Naming the protocols is the
   * same move one step further, and it is why `withConditions` links the phrase
   * and `scopeLine` no longer does.
   *
   * Served like the scope sentence (`conditions_qualifier`), so the wording can
   * be sharpened again without a store submission. The fallback is only for an
   * install whose index fetch failed, and matches the served words for the same
   * reason SCOPE_FALLBACK does.
   *
   * Never on "not tested": an application nobody ran is not a result under any
   * conditions, and qualifying it would imply one.
   */
  const CONDITIONS_FALLBACK = "under the consensus protocols";
  let scopeShort = null;
  let protocolsUrl = null;
  let conditions = null;
  let applicationScope = {};
  let served = null;
  let limitedNote = null;

  /**
   * Does this application's result sit on the middle rung?
   *
   * The one reader for that question, because the chip and the headline both
   * ask it and until 3 Sep 2026 only the chip did. `LIMITED_LABEL` reached a
   * headline solely through level `yellow`, which `matcher.js` grants only when
   * there are no passes AND every failure is qualified — so `mixed` and `red`
   * flattened a qualified negative to "not supportive" while the chip directly
   * beneath it read "limited support". Seen live on ab237703 (headline *not
   * supportive for WB* over a WB chip reading *limited support*) and on
   * ab104859. Two lines of one summary contradicting each other, on the rung
   * 0.3.1 exists to keep visible.
   *
   * `sl` and `xs` are excluded because they REINFORCE a verdict rather than
   * hold it back — `core/recommendations.py::QUALIFIER_CODES` carries that as
   * its `tempers` boolean, and this list is the browser's copy of it. They can
   * only ever attach to a supportive verdict server-side (`qualified()` emits
   * them under RECOMMENDED alone), so a *failed* application carrying one is
   * unreachable today; the exclusion is here so this agrees with the chip
   * whatever the builder later emits. Note that `matcher.js::failedAllQualified`
   * asks the weaker question (any qualifier at all) when it picks yellow — the
   * two are equivalent while that server-side rule holds, and not by
   * construction.
   */
  function isLimited(status, app) {
    const code = (status.qualifiers || {})[app];
    return Boolean(clauseFor(code)) && !["sl", "xs"].includes(code);
  }

  /**
   * The failed side of a headline, with the two rungs named apart.
   *
   * The clause ("detects the target") stays on the chips: yellow puts it in the
   * headline because it IS the point of that colour, while here it would run a
   * mixed verdict to three clauses. Naming the rung is what stops the headline
   * contradicting the chip; the clause is detail the chips already carry.
   */
  function failedSide(status, apps, plainLead) {
    const limited = apps.filter((a) => isLimited(status, a));
    const plain = apps.filter((a) => !isLimited(status, a));
    const parts = [];
    // Limited first: it is the softer finding, and leading with the harder one
    // then softening reads as a retraction.
    if (limited.length) parts.push(`${LIMITED_LABEL} for ${esc(limited.join(", "))}`);
    // `plainLead` is red's "data ", which belongs to THIS segment and not to
    // the sentence — "Tested; data limited support for WB" is not English.
    if (plain.length) {
      parts.push(`${plainLead || ""}<em>not</em> supportive for ${esc(plain.join(", "))}`);
    }
    return parts.join(", ");
  }

  /**
   * The clause shared by a set of applications, or the first one if they differ.
   *
   * Every failed application on a yellow mark carries a code — that is what
   * makes it yellow — but WB's and IP's are different sentences, so a record
   * failing both cannot state one clause for the pair. It says the one it can
   * stand behind and the per-application rows carry the rest.
   */
  function qualifierClause(status, apps) {
    const codes = (apps || [])
      .map((a) => (status.qualifiers || {})[a])
      .filter(Boolean);
    const distinct = codes.filter((c, i) => codes.indexOf(c) === i);
    if (!distinct.length) return "";
    /* Two applications with different clauses. Say both rather than reach for
       one sentence that covers them: a generic phrase is exactly what this
       release exists to remove. */
    return distinct.map(clauseFor).filter(Boolean).join("; ");
  }

  /**
   * "recommended for WB" -> "recommended for WB under the consensus protocols
   * tested", with the phrase linked to the protocols themselves.
   *
   * The link is built HERE and never travels in the index: `conditions` is
   * served text and everything served is escaped before it reaches innerHTML.
   * A qualifier that shipped its own markup would be the one string on this
   * card that a change to the builder could turn into a script tag.
   *
   * It replaces the scope line's "Read the protocols" rather than joining it —
   * two links to one URL, a line apart, is the clutter the scope line's own
   * comment warns about. And it reaches strictly further: `scopeLine` draws on
   * green, red, mixed and grey-with-a-verdict, while every verdict that takes a
   * qualifier now carries the way to check it.
   */
  function withConditions(claim) {
    const words = esc(conditions || CONDITIONS_FALLBACK);
    const qualifier = protocolsUrl
      ? `<a href="${esc(protocolsUrl)}" target="_blank" rel="noopener">${words}</a>`
      : words;
    return `${claim} ${qualifier}`;
  }

  /** Called once by the content script when the index arrives. */
  function setScope(index) {
    scopeShort = (index && index.scopeShort) || null;
    protocolsUrl = (index && index.protocolsUrl) || null;
    conditions = (index && index.conditionsQualifier) || null;
    applicationScope = (index && index.applicationScope) || {};
    // Served wording wins where it exists; the baked table stays as the
    // fallback, so a cached index predating the key still draws a sentence.
    served = (index && index.qualifierWords) || null;
    limitedNote = (index && index.limitedSupportNote) || null;
  }

  /** The clause for a code — served if the index carries one, else baked. */
  function clauseFor(code) {
    if (!code) return "";
    return (served && served[code]) || QUALIFIERS[code] || "";
  }

  /**
   * What the result does NOT cover — and only that, since 3 Sep 2026.
   *
   * It used to open "Result from consensus protocols." and end with "Read the
   * protocols". Both halves moved up into the verdict: `CONDITIONS_QUALIFIER`
   * names the protocols inside the claim and `withConditions` links them. A
   * reader told the result depends on the protocol is still given the way to
   * see which protocol — one line higher, bound to the words it qualifies,
   * rather than in a sentence they meet after the strong one.
   *
   * What is left is the half that has nowhere better to be: performance depends
   * on protocol and sample, which is about the dataset and not this reagent.
   */
  function scopeLine() {
    return `<p class="scope">${esc(scopeShort || SCOPE_FALLBACK)}</p>`;
  }

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
   * reader see the characterisation image for only one of them — whichever the card
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
      /* The clause after the verdict, per application — the whole point of the
         layer, and the chips are where a reader compares the four. A negative
         that did the thing takes the yellow chip outright; a supportive verdict
         the data fell short of keeps its green and takes a small tab, exactly
         as the gene page draws them. */
      const clause = clauseFor((status.qualifiers || {})[app]);
      const tempered = isLimited(status, app);
      const shade = code === 1 && tempered ? " chip-q"
                  : code === 2 && tempered ? " chip-tab" : "";
      const rung = code === 1 && tempered ? LIMITED_LABEL : CODE_LABEL[code];
      const words = rung + (clause ? ` — ${clause}` : "");
      return `<button type="button" class="chip chip-${CODE_CLASS[code]}${shade}${on}${here}"
        data-tab="${app}" aria-pressed="${app === selected}"
        title="${esc(APP_LABEL[app])}: ${esc(words)}${esc(note)}">
        <b>${app}</b><i>${esc(rung)}</i></button>`;
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

    // WE HOLD NOTHING FOR WHAT THIS PAPER DID. The fall-through below is a
    // search for a figure to show, and on these two branches it found one for a
    // DIFFERENT assay: a card headed "Not tested for FC" opened on the IP tab
    // with a knockout immunoprecipitation filling most of it. The headline was
    // right and the picture answered a question nobody asked — and a knockout
    // figure is the most persuasive thing on this card, so having it be about
    // another application is the expensive way to be misread.
    //
    //   app-not-tested   the paper named an assessed application this antibody
    //                    has no result for. Its OWN tab is opened, with no
    //                    image, so the panel says "No published FC figure ...
    //                    OGA has not tested it in this application" — the true
    //                    answer, in the place the reader is looking.
    //   app-not-assessed the paper named something OGA does not test at all
    //                    (IHC above all). There is no tab for it, so nothing is
    //                    preselected and no figure is drawn. The `outside`
    //                    paragraph says what WAS tested, and every tab is still
    //                    one click away.
    //
    // Neither hides anything: the chips carry all four verdicts either way.
    if (s.reason === "app-not-tested") return (s.mentioned || [])[0] || null;
    if (s.reason === "app-not-assessed") return null;

    return (s.scopedTo || []).find(hasImage)
      || (s.scopedTo || [])[0]
      || (s.mentioned || []).find(hasImage)
      || (s.passed || []).find(hasImage)
      || (s.failed || []).find(hasImage)
      || (rec.img ? Object.keys(rec.img)[0] : null)
      || (s.mentioned || [])[0]
      || "WB";
  }

  /* ------------------------------------------- declared-target notices */

  /**
   * A reagent whose DECLARED target is not the one people buy it for.
   *
   * THE CARD HAS TO SAY WHICH OF TWO THINGS RED MEANS, because the mark cannot.
   * Red now carries two facts by decision (owner, 12 Sep 2026): *tested and not
   * supportive*, which comes from knockout-controlled data, and *this is an
   * antibody to another protein*, which comes from the supplier's own datasheet
   * and a published review. Nothing on a notice card mentions performance, and
   * nothing on it is a verdict: OGA has not tested any of the listed codes.
   *
   * THREE PARAGRAPHS, IN THIS ORDER, and the order is the argument. The claim
   * about the PAPER first, because that is what the reader is holding and the
   * only part they can act on. Then which antibody is for which protein, which
   * is what makes the first line make sense. Then the supplier's own words,
   * where the datasheet carries a statement, since a manufacturer saying "there
   * is no observed cross reactivity" is stronger evidence than anything we
   * could write. The link to the review goes with the others at the foot.
   */
  function confusionHeadline(status) {
    const words = status.confusion.words;
    return `Antibody to ${esc(words.declared)}, not to ${esc(words.mistaken_protein)}`;
  }

  /**
   * What the published list says about THIS paper. Three states and a fourth.
   *
   * `mistaken` is the documented case and is stated plainly: the reader's own
   * paper is on a list somebody published, and hedging that would be less
   * useful and no kinder.
   *
   * `not_checked` is 72 of the 406 p16 papers, where the reviewer could not
   * reach the full text. It keeps the mark and says the use could not be
   * established rather than borrowing the majority's answer.
   *
   * `as_declared` never gets here: `matcher.js::worthShowing` draws no mark at
   * all on a paper the reviewer recorded as using the reagent correctly.
   *
   * With no verdict the paper is on no list, or the page declared no DOI, and
   * the strongest honest sentence is *may have*. It carries the count, because
   * "may have" with nothing behind it is a suspicion and "may have, and 317 of
   * 406 reviewed papers did" is a reason to check.
   */
  function confusionWhy(status) {
    const c = status.confusion;
    const mistaken = esc(c.words.mistaken_protein);
    if (c.verdict === "mistaken") {
      return `<strong>This paper is on a published list of papers that used it `
        + `as a ${mistaken} antibody.</strong>`;
    }
    if (c.verdict === "not_checked") {
      return `This paper is on a published list of papers citing this antibody. `
        + `Its full text could not be reached, so the review could not say which `
        + `protein it was used for.`;
    }
    const n = c.words.mistaken_papers;
    const tally = n
      ? ` A published review found ${n} papers that used these codes as `
        + `${mistaken} antibodies.`
      : "";
    return `<strong>This paper may have used the wrong antibody.</strong>${tally}`;
  }

  function confusionBlock(status) {
    const c = status.confusion;
    const parts = [`<p class="notice">${esc(c.words.short)}</p>`];
    if (c.statement) {
      // The manufacturer's own sentence, quoted. It is the strongest thing on
      // the card and it is not ours.
      parts.push(`<p class="notice-q">${esc(c.supplier || "The supplier")} states:
        &ldquo;${esc(c.statement)}&rdquo;</p>`);
    }
    parts.push(`<p class="notice-src">Documented in ${esc(c.words.source)}.
      OGA has not tested this antibody; this is about which protein it is raised
      against, not how well it works.</p>`);
    return parts.join("");
  }

  // headline() and explain() take the whole hit, not just the status: blue's
  // wording rests on how the target was attributed, which is a property of the
  // hit and deliberately not of the verdict.
  function headline(hit) {
    const status = hit.status;
    /* What the headline may name.
     *
     * The record, EXCEPT where the citation layer narrowed the verdict — and
     * that exception is the whole of it. A proximity guess never sets
     * `scopedTo`, so an inferred application still cannot shrink the headline:
     * "Recommended for WB" over an antibody that fails IP and IF was true of
     * the page's guess and misleading about the reagent, and the guess was
     * wrong on 17 of the 54 papers it spoke about.
     *
     * A LOOKUP is different, and leaving it out was a bug that shipped. The
     * colour is already scoped when `scopedTo` is set, and the words were not,
     * so a card narrowed to western blot printed "Limited support for WB, IP,
     * IF" over chips reading IP not supportive and IF not supportive —
     * claiming limited support for two applications that have none. Found on a
     * real ab104859 card, 29 Aug 2026. Yellow is where it shows, because
     * `failed` can hold applications the narrowing excluded; the same mismatch
     * is latent in every other case here.
     */
    const named = (list) => {
      const only = status.scopedTo;
      return (only && only.length)
        ? (list || []).filter((a) => only.includes(a))
        : (list || []);
    };
    // Before the switch, not inside it: the level is `red`, and the two things
    // red means need different headlines. `withConditions` is deliberately NOT
    // applied — "under the consensus protocols" qualifies a measurement, and
    // there is no measurement here.
    if (status.reason === "target-confusion" && status.confusion) {
      return confusionHeadline(status);
    }
    switch (status.level) {
      case "green":
        return withConditions(`Characterisation data supports ${esc(named(status.passed).join(", "))}`);
      case "red":
        // A red record whose failures are ALL qualified is yellow, not red, so
        // the limited-only shape here is unreachable under the current level
        // rules. Written to cope with it anyway: a headline must not depend on
        // which level chose it.
        return withConditions(
          "Tested; " + failedSide(status, named(status.failed), "data "));
      // Not supportive, and yet the antibody was seen to do something
      // on-target. The clause is the point of the colour, so it is in the
      // headline rather than only in the rows underneath: a reader who takes
      // the headline alone must not take away "showed nothing".
      case "yellow":
        return withConditions(
          `Limited support for ${esc(named(status.failed).join(", "))} — `
          + esc(qualifierClause(status, named(status.failed))));
      case "mixed":
        return withConditions(`Supports ${esc(named(status.passed).join(", "))} — `
          + failedSide(status, named(status.failed)));
      case "blue":
        // Blue is an OFFER: knockout-controlled antibodies are recommended for
        // this target and the reader can go and use one. Leading with "Not in the
        // dataset" made a mark whose whole value is the alternative read as a
        // verdict on a reagent — and on OGA's own gene pages, where a phrase like
        // "10 APP antibodies" is descriptive text and not a product at all, it
        // read as an accusation about an antibody that does not exist.
        //
        // "read as" is still load-bearing. Blue is the only verdict the
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
  /** Does this record carry any verdict at all, in any application? */
  function hasVerdict(rec) {
    const M = root.OGAMatcher;
    const { passed, failed } = M.recordVerdicts(rec);
    return !!(passed.length || failed.length);
  }

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

    // `failedSide` is the one reader for the failed half of a headline, and the
    // reason to call it rather than write the clause a second time: it names the
    // limited rung apart, so this cannot say "not supportive for WB" over a chip
    // reading "limited support for WB". That is the 0.3.3 defect, and this branch
    // had its own copy of the sentence, which is how it kept both the old
    // vocabulary and the old bug.
    const clause = [];
    if (passed.length) clause.push(`supports ${esc(passed.join(", "))}`);
    if (failed.length) clause.push(failedSide(status, failed));

    // "but" says there is something to see. With a pass and a fail to report
    // there plainly is, and the word only gets in the way.
    // The lead ("Not tested for FC") takes no qualifier -- see
    // CONDITIONS_FALLBACK. The clause after it is a real verdict and does.
    const joiner = clause.length > 1 ? " — " : " — but ";
    return lead + joiner + withConditions(clause.join(", "));
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
    // Resolved here rather than at the top of the file, matching `hasVerdict`
    // and `greyHeadline`: `root.OGAMatcher` is assigned by another script and
    // a module-level const would capture whatever it was at load time.
    const M = root.OGAMatcher;
    const status = hit.status;
    // First, because nothing below it applies: every other branch reasons about
    // which application the page was read as naming, and a notice is not about
    // an application at all.
    if (status.reason === "target-confusion" && status.confusion) {
      return confusionWhy(status);
    }
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
        : `, and the ${esc(M.listOf(doubted))} reading is unreliable — check what the paper actually used.${route}`;

    if (status.reason === "app-not-assessed") {
      const which = (status.unassessed || []).join(", ");
      return `This page mentions ${esc(which)}, which is not one of the four applications assessed under the consensus protocols — nothing below applies to it${shaky} Our results for every assessed application are shown; a result in one never carries over to another.`;
    }
    if (status.reason === "app-not-tested") {
      return `This page mentions ${esc(M.listOf(named))}, which has not been tested${shaky} Our results for every assessed application are shown below.`;
    }
    if (status.reason === "no-application") {
      return `This page doesn't say which application this antibody was used for — all our results are shown below.`;
    }
    if (status.reason === "mentioned") {
      return `This page mentions ${esc(M.listOf(named))}${shaky} Our results for every application are shown; the ones mentioned are marked.`;
    }
    if (status.level === "blue") {
      return blueExplain(hit);
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
   * Blue's evidence, shown because it is worth seeing — not because it is
   * suspect.
   *
   * Blue is the one verdict read out of the page rather than looked up, so
   * naming the words it was read from lets a reader confirm it at a glance.
   * That is the whole purpose: transparency, not a warning.
   *
   * A mark-level audit of 123 marks measured this path directly — 0 of 45
   * blue marks had a wrong gene, and 43 of 45 took the target from an
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
  function blueExplain(hit) {
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

    // The supplier, from the record where there is one and from the notice
    // otherwise. Every other card names it, and on a notice it is the half a
    // reader needs to confirm they are holding the product being described --
    // `AB986` is a Millipore number and an Abcam-shaped one, which is the whole
    // reason the matcher gates that code on the supplier at all.
    const supplier = (rec && rec.s) || (s.confusion && s.confusion.supplier) || "";
    parts.push(`<div class="hd hd-${s.level}">
      <span class="dot"></span>
      <div class="hd-t">
        <strong>${esc(rec ? rec.n : (hit.matched || "").trim())}</strong>
        ${supplier ? `<span class="sup">${esc(supplier)}</span>` : ""}
      </div>
    </div>`);

    parts.push(`<div class="verdict">${headline(hit)}</div>`);

    // Directly under the verdict, and only where there IS one. On grey and
    // blue the card's own first line already says there is no result for this
    // reagent, and a caveat about what a result covers, under a card that holds
    // none, is the third caveat on one screen that teaches a reader to skip all
    // of them.
    if (rec && (s.level === "green" || s.level === "red" || s.level === "mixed"
                || (s.level === "grey" && hasVerdict(rec)))) {
      parts.push(scopeLine());
    }

    const why = explain(hit);
    if (why) parts.push(`<p class="why">${why}</p>`);

    // Between the paper claim and the results, so a card that has BOTH reads in
    // the right order: what this paper may have done, which protein the reagent
    // is raised against, and only then any verdict we hold. No record carries
    // one today — none of the listed codes is in the dataset — and the ordering
    // is what would be wrong if one ever does.
    if (s.confusion) parts.push(confusionBlock(s));

    if (rec) {
      const selected = selectedApp || defaultApp(hit);
      parts.push(`<div class="chips">${appTabs(rec, s, selected)}</div>`);

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
          ? `it was tested, and ${failedSide(s, s.failed)}`
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
          <img src="${esc(rec.img[showApp])}" alt="${esc(APP_LABEL[showApp])} characterisation data" loading="lazy">
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

      // What is true of THIS application and not of the others, under the panel
      // it belongs to rather than in the card-wide caveat. Immunofluorescence is
      // the case that has one: whether an epitope survives depends on how the
      // sample was fixed and permeabilised, so a reader whose protocol differs
      // is owed that beside the figure and not in a sentence about the dataset.
      //
      // Drawn whether or not there IS a figure -- it qualifies the verdict on
      // the open tab, and the chip carries that verdict either way.
      if (showApp && applicationScope[showApp]) {
        parts.push(`<p class="appscope">${esc(applicationScope[showApp])}</p>`);
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
        ? `Knockout-controlled characterisation contributed by ${esc(rec.src || "the supplier")}`
        : `Independently tested by ${esc(rec.src || "YCharOS")} with knockout controls`}</div>`);

      const links = [`<a href="${PORTAL}/antibodies/${encodeURIComponent(rec.g)}/" target="_blank" rel="noopener">All ${esc(rec.g)} antibodies</a>`];
      if (rec.doi) links.push(`<a href="${esc(rec.doi)}" target="_blank" rel="noopener">Report</a>`);
      if (rec.p) links.push(`<a href="${esc(rec.p)}" target="_blank" rel="noopener">Product page</a>`);
      if (hit.rrid) links.push(`<a href="https://www.antibodyregistry.org/${esc(hit.rrid)}" target="_blank" rel="noopener">RRID</a>`);
      parts.push(`<div class="links">${links.join("")}</div>`);
    } else if (s.confusion) {
      // The review is the evidence and the only link that belongs here. No
      // "search the portal" and no "nominate this target": we hold nothing on
      // either protein, and offering a search for a verdict that does not exist
      // is how a card about identity gets read as a card about quality.
      const c = s.confusion;
      const links = [`<a href="${esc(c.words.url)}" target="_blank" rel="noopener">Read the review</a>`];
      if (c.words.mistaken) {
        links.push(`<a href="${PORTAL}/antibodies/" target="_blank" rel="noopener">Antibodies OGA has characterised</a>`);
      }
      parts.push(`<div class="links">${links.join("")}</div>`);
    } else if (s.level === "blue") {
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
  /* The dot is the card's copy of the mark's colour, so it takes the same four
     values. It kept the old orange when amber became blue — the rename moved
     the level string and every other rule, and this one names its colour in
     hex, so nothing caught it. */
  .hd-green  .dot { background: #0f9d58; }
  .hd-yellow .dot { background: #f4b400; }
  .hd-red    .dot { background: #c5221f; }
  .hd-mixed  .dot { background: linear-gradient(90deg,#0f9d58 50%,#c5221f 50%); }
  .hd-blue   .dot { background: #1a73c7; }
  .hd-grey   .dot { background: #9aa6b2; }
  .hd-t strong { font-size: 14px; font-weight: 650; }
  .hd-t .sup { display: block; color: #5d6b7a; font-size: 11.5px; }
  .verdict { font-weight: 600; margin-bottom: 5px; }
  /* Never its own colour: .verdict is green/amber/red by level, and a blue
     link inside it reads as a second signal about the antibody. It says it
     is a link by being underlined. */
  .verdict a { color: inherit; text-decoration: underline; text-underline-offset: 2px; }
  /* The headline takes its rung's colour. Only green had one, so a limited-
     support card stated a middle rung in the same neutral ink as a refusal. */
  .hd-green  ~ .verdict { color: #0b6b3a; }
  .hd-yellow ~ .verdict { color: #8a6100; }
  .why { margin: 0 0 8px; color: #4b5866; font-size: 12px; }
  /* Quieter than .why: .why is about THIS page, .scope is about the dataset,
     and the reader should meet the page's own sentence first. */
  .scope { margin: 0 0 8px; color: #5d6b7a; font-size: 11px; line-height: 1.4; }
  .scope a { color: #12639e; white-space: nowrap; }
  /* Sits with the figure it qualifies, not with the card-wide caveat. */
  .appscope { margin: -4px 0 8px; color: #5d6b7a; font-size: 11px; line-height: 1.4; }
  .chips { display: grid; grid-template-columns: repeat(4,1fr); gap: 4px; margin-bottom: 9px; }
  .chip { border-radius: 6px; padding: 4px 3px; text-align: center; border: 1px solid transparent; }
  .chip b { display: block; font-size: 11px; font-weight: 700; }
  .chip i { display: block; font-style: normal; font-size: 9px; letter-spacing: .01em; }
  .chip-r { background: #eafaf0; color: #0b6b3a; border-color: #b7e0c6; }
  .chip-n { background: #fdeceb; color: #a3231d; border-color: #f3c4c4; }
  .chip-u { background: #f1f4f7; color: #77838f; border-color: #e0e6ec; }
  /* Not supportive, and yet it did the thing the application is for: yellow
     outright, the same three colours the gene page uses. */
  .chip-n.chip-q { background: #fffaea; color: #8a6100; border-color: #f4b400; }
  /* Supportive, and the data fell short on one axis: green KEEPS its colour and
     takes a small yellow tab. The antibody IS supported for the application;
     repainting it would contradict the word inside the chip. */
  .chip-r.chip-tab { position: relative; }
  .chip-r.chip-tab::after { content: ""; position: absolute; top: 0; left: 50%;
    transform: translateX(-50%); width: 18px; height: 3px; background: #f4b400;
    border-radius: 0 0 2px 2px; }
  .chip { font: inherit; cursor: pointer; }
  .chip:focus-visible { outline: 2px solid #12639e; outline-offset: 1px; }
  /* Named on the page / used in this paper. */
  .chip-on { outline: 2px solid #16202b; outline-offset: -2px; }
  /* Which tab is open. Distinct from chip-on, which says something about the
     PAPER: one is what the reader chose, the other is what the record says. */
  .chip-sel { box-shadow: inset 0 -3px 0 #16202b; font-weight: 700; }
  .outside { margin: -4px 0 8px; color: #4b5866; font-size: 11.5px; line-height: 1.45; }
  /* A declared-target notice. Boxed, because it is the one block on any card
     that is not about how well an antibody works, and a reader who skims has to
     be able to see that it is a different kind of statement. */
  .notice { margin: 0 0 6px; color: #16202b; font-size: 11.5px; line-height: 1.45;
            background: #fdeceb; border: 1px solid #f3c4c4; border-radius: 6px;
            padding: 7px 8px; }
  .notice-q { margin: -2px 0 6px; color: #4b5866; font-size: 11px; line-height: 1.45;
              border-left: 2px solid #f3c4c4; padding-left: 7px; }
  .notice-src { margin: 0 0 8px; color: #5d6b7a; font-size: 11px; line-height: 1.4; }
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
    .hd-t .sup, .why, .scope, .appscope, .meta, .prov, .fig figcaption { color: #9fadbb; }
    .chip-r { background: #123020; color: #7ddba3; border-color: #1e5335; }
    .chip-n { background: #3a1a1a; color: #f2a3a3; border-color: #5c2a2a; }
    .chip-u { background: #262d36; color: #94a2b0; border-color: #333d48; }
    .chip-n.chip-q { background: #3a2f10; color: #f0cc72; border-color: #6a5416; }
    .chip-on { outline-color: #e7edf3; }
    .chip-sel { box-shadow: inset 0 -3px 0 #e7edf3; }
    .outside { color: #9fadbb; }
    .notice { background: #3a1a1a; border-color: #5c2a2a; color: #f0dede; }
    .notice-q { border-left-color: #5c2a2a; color: #9fadbb; }
    .notice-src { color: #9fadbb; }
    .nofig { color: #9fadbb; background: #222a33; border-color: #333d48; }
    .prov { border-top-color: #2c343d; }
    .links a, .scope a { color: #6fb6ec; }
    .fig img { border-color: #333d48; background: #11151a; }
  }`;

  root.OGACard = { show, hide, scheduleHide, setScope, get anchor() { return currentAnchor; } };
})(typeof globalThis !== "undefined" ? globalThis : self);
