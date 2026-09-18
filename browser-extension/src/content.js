/**
 * Page scanner. Walks the document once, asks the matcher what it found, and
 * wraps each mention in a coloured <mark>.
 *
 * All of this is local. The only network traffic the extension ever makes is
 * the periodic index refresh in the service worker, plus characterisation images
 * loaded from the portal when a card is actually opened.
 */
(function () {
  "use strict";

  const M = globalThis.OGAMatcher;
  // Guarded rather than assumed: if paper.js ever fails to load, the extension
  // must fall back to exactly what it did before the citation layer existed, not
  // stop marking antibodies altogether.
  const P = globalThis.OGAPaper || null;
  const Card = globalThis.OGACard;

  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "INPUT", "SELECT", "OPTION",
    "CODE", "PRE", "KBD", "SAMP", "SVG", "MATH", "HEAD", "TITLE",
  ]);

  /**
   * One class per level `resolveStatus` can return. A level missing from here
   * paints NOTHING: `mark.className` becomes `oga-hl undefined`, so the mark
   * has no colour, no underline and no pattern, while `levelEnabled` still
   * returns true and the card behind it is perfectly correct. A signal that is
   * drawn and invisible, which is the worst of the three outcomes.
   *
   * `yellow` was absent from 0.3.1 — the release that introduced the middle
   * rung — until 12 Sep 2026. Every other surface had it: `content.css` paints
   * `mark.oga-yellow` in both themes and gives it a dashed pattern, `card.js`
   * has `.hd-yellow`, the popup has a yellow tile. Only the map that puts the
   * class on the element did not, and 856 of the live index's 1,603 records
   * carry a qualifier, so this was most of the rung.
   *
   * It survived because the bundled 18-record fixture carries no qualifier at
   * all, so no local run could produce a yellow mark; CI produced one only
   * because its network let the live index replace the fixture, and that
   * arrived as one of three failures in a suite already red for another
   * reason. `matcher.test.mjs` pins the pairing now.
   */
  const LEVEL_CLASS = {
    green: "oga-green", yellow: "oga-yellow", red: "oga-red",
    mixed: "oga-mixed", blue: "oga-blue", grey: "oga-grey",
  };

  const hits = [];              // index -> hit, referenced by data-oga
  let index = null;
  let settings = null;
  /**
   * What the citation record knows about THIS paper, resolved once at boot.
   *
   * One of `{status:"covered", …}`, `{status:"not-covered"}` or
   * `{status:"unavailable"}`, and the three are kept apart all the way to the
   * card. "Not covered" is a fact about the citation record; "unavailable" is a
   * fact about us; and neither is a fact about whether the paper cites the
   * reagent. Collapsing them is how a reader comes to believe the record says
   * something it does not.
   *
   * Resolved at boot rather than per scan: `scan` re-runs on every mutation
   * (the observer below), the page's identity does not change between runs, and
   * a lookup per mutation would be pure waste.
   */
  let paper = { status: "unavailable", reason: "not-resolved-yet" };
  let scanScheduled = false;
  let observer = null;

  /* ---------------------------------------------------------------- scan */

  /**
   * The reference list is text about OTHER papers, and it is the one region of
   * an article where a number that looks like a catalogue number reliably is
   * not one.
   *
   * A page range is the case that bit: reference 54 of a PLoS Pathogens paper
   * ends "Virology. 2012;429(2):136-47", and `136-47` normalises to `13647` --
   * a real Cell Signaling antibody, with a real verdict, attached to a
   * bibliography. The reader gets a confident green underline on somebody
   * else's page numbers. Nothing on the page contradicts it, which makes it the
   * harmful direction: an annotation that is wrong where the paper is silent.
   *
   * Two ways in, because neither alone is enough. The selectors are what the
   * publishers we ship for actually mark the list with, and they are exact. The
   * heading walk is for everyone else -- a section titled "References" whose
   * container carries no useful class at all, which is most of the long tail.
   *
   * Skipping too much costs a highlight nobody was going to act on; skipping
   * too little costs a wrong verdict. That asymmetry is why this errs wide.
   */
  const REFERENCE_SELECTOR = [
    "[role='doc-bibliography']", "[role='doc-biblioentry']",
    "#references", "#reference-list", "#refs", "#ref-list", "#bibliography",
    "[id^='ref-list']", "[id^='__ref-list']", "[id^='reference-list']",
    "[id^='Bib']", "[id^='bib']", "[id^='CR']",
    ".ref-list", ".reflist", ".references", ".reference-list", ".ref-listing",
    ".c-article-references", ".article-references", ".article-section__references",
    ".bibliography", ".citation-list", ".citedBySection",
    "section.references", "ol.references", "ul.references",
  ].join(",");

  const REFERENCE_HEADING_RE =
    /^\s*(?:\d+[.)]?\s*)?(?:references?|bibliography|literature\s+cited|works\s+cited|references\s+and\s+notes)\s*:?\s*$/i;

  const HEADING_TAGS = ["H1", "H2", "H3", "H4", "H5", "H6"];

  /**
   * Elements whose text must not be scanned, resolved once per scan.
   *
   * The heading half deliberately takes the heading's FOLLOWING SIBLINGS rather
   * than its parent: "<h2>References</h2><ol>...</ol>" is very often a heading
   * and a list sitting directly inside the article container, so excluding the
   * parent would silently stop marking the whole paper -- the failure this is
   * meant to prevent, one level worse.
   */
  function referenceRegions(rootNode) {
    const regions = [];
    const scope = rootNode.nodeType === Node.ELEMENT_NODE ? rootNode : document.body;
    if (!scope || !scope.querySelectorAll) return regions;

    let marked;
    try {
      marked = scope.querySelectorAll(REFERENCE_SELECTOR);
    } catch (err) {
      marked = [];  // a selector this browser will not parse must not stop the scan
    }
    for (const el of marked) regions.push(el);

    for (const heading of scope.querySelectorAll(HEADING_TAGS.join(","))) {
      if (!REFERENCE_HEADING_RE.test(heading.textContent || "")) continue;
      const level = HEADING_TAGS.indexOf(heading.tagName);
      regions.push(heading);
      for (let el = heading.nextElementSibling; el; el = el.nextElementSibling) {
        const next = HEADING_TAGS.indexOf(el.tagName);
        // A heading of the same or higher rank ends the section; a lower one
        // ("References" then "Further reading") is still inside it.
        if (next !== -1 && next <= level) break;
        regions.push(el);
      }
    }
    return regions;
  }

  function collectTextNodes(rootNode) {
    const nodes = [];
    const skip = referenceRegions(rootNode || document.body);
    const walker = document.createTreeWalker(rootNode, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (SKIP_TAGS.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
        // Few regions and a native containment test, rather than a selector
        // match per text node: a reference list is thousands of them.
        for (const region of skip) {
          if (region === parent || region.contains(parent)) return NodeFilter.FILTER_REJECT;
        }
        if (parent.closest(".oga-card-host, [contenteditable=true]")) {
          return NodeFilter.FILTER_REJECT;
        }
        // Hidden content is deliberately NOT skipped. Publishers pre-render
        // reagent tables inside modals ("Table 2. Summary of the antibodies
        // tested") and reveal them on click by flipping an attribute — which
        // adds no nodes, so a childList observer never fires and the table
        // would stay unmarked. Marking it while hidden costs nothing: the marks
        // are invisible until the reader opens it, and then they are already
        // there.
        // Text already inside one of our marks is kept, so that a rescan still
        // sees the whole sentence. Without it, a second pass would read
        // "anti-TDP-43 antibody (Abcam )" -- the catalogue number hidden inside
        // an existing mark -- and reach a different conclusion than the first.
        // decorate() is what refuses to mark it twice.
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    return nodes;
  }

  /**
   * Concatenate the document's text so the matcher sees full context (an
   * application named in a heading still governs the reagents listed under
   * it), while keeping a map back to the individual text nodes.
   */
  // Elements that do not interrupt a run of text. Publishers split identifiers
  // with these constantly — "10782-<span>2-AP</span>" — and a separator inserted
  // mid-identifier makes the whole mention invisible.
  const INLINE_TAGS = new Set([
    "SPAN", "A", "EM", "STRONG", "I", "B", "U", "S", "SMALL", "SUP", "SUB",
    "ABBR", "CITE", "MARK", "WBR", "FONT", "LABEL", "TT", "Q", "VAR", "BDI",
    "BDO", "TIME", "DATA", "RUBY", "NOBR",
  ]);

  /** Nearest ancestor that actually breaks the line of text. */
  function blockAncestor(node) {
    let el = node.parentElement;
    while (el && INLINE_TAGS.has(el.tagName)) el = el.parentElement;
    return el;
  }

  /**
   * Text that governs a block by where it SITS rather than by what is near it.
   *
   * The matcher reads the application out of the surrounding characters — the
   * enclosing sentence first, then a window reaching back and to the end of the
   * block. That window already covers a section heading above a methods
   * paragraph, and a cue later in the same figure legend.
   *
   * A reagent TABLE defeats it completely, and reagent tables are where methods
   * sections put antibodies. The application is a column, so for the antibody in
   * row 12 the word "Immunofluorescence" sits in the header row — thousands of
   * characters back, behind eleven other reagents. No window reaches it without
   * also dragging in every neighbouring row's application, which is worse than
   * reading none: a wrong application produces a confident wrong verdict, where
   * an absent one produces an honest span across all four.
   *
   * So the cell is given what a reader gets from the layout: its own column's
   * header, the rest of its own row, and the table's caption. `<figcaption>` is
   * here for the same reason — a legend that wraps several blocks is one caption
   * to a reader and several unrelated blocks to a character window.
   *
   * Cached per governing element, so a 200-row table walks each header once.
   */
  const CELL_TAGS = new Set(["TD", "TH"]);
  const contextCache = new WeakMap();

  /**
   * An element's text with its BLOCK children held apart.
   *
   * `textContent` concatenates them, and that is the footnote-marker bug one
   * level up: a row renders as `…ab109535Western blot…`, where the leading `\b`
   * of the Western blot cue has a digit on its left and cannot match. The
   * application would be lost in exactly the layout this pass exists to read.
   *
   * Inline children stay joined, for the same reason the page text does: a cell
   * reading `10782-<span>2-AP</span>` is one string.
   */
  function cellText(el) {
    if (!el) return "";
    let out = "";
    for (const child of el.childNodes) {
      if (child.nodeType === 1 && !INLINE_TAGS.has(child.tagName)) {
        out += ". " + cellText(child) + ". ";
      } else {
        out += child.textContent || "";
      }
    }
    return out.replace(/\s+/g, " ").trim();
  }

  function structuralContext(block) {
    if (!block) return "";
    let el = block;
    while (el && !CELL_TAGS.has(el.tagName) && el.tagName !== "FIGCAPTION") {
      if (el.tagName === "BODY" || el.tagName === "HTML") return "";
      el = el.parentElement;
    }
    if (!el) return "";
    if (contextCache.has(el)) return contextCache.get(el);

    let context = "";
    if (el.tagName === "FIGCAPTION") {
      context = cellText(el);
    } else {
      const row = el.closest && el.closest("tr");
      const table = el.closest && el.closest("table");
      const parts = [];
      // The column's header. Taken from <thead> where there is one, else the
      // table's first row — which is what publishers who skip <thead> produce.
      if (row && table) {
        const head = (table.tHead && table.tHead.rows[0]) || table.rows[0];
        if (head && head !== row) {
          const i = Array.prototype.indexOf.call(row.cells, el);
          const th = i >= 0 && head.cells[i];
          if (th) parts.push(cellText(th));
        }
      }
      // The rest of the row: in a reagent table the application is as often a
      // cell beside the catalogue number as a column heading above it.
      if (row) parts.push(cellText(row));
      if (table) {
        const caption = table.querySelector && table.querySelector("caption");
        if (caption) parts.push(cellText(caption));
      }
      context = parts.filter(Boolean).join(". ");
    }
    contextCache.set(el, context);
    return context;
  }

  /**
   * A footnote or reference marker: digits, optionally bracketed, optionally
   * a list or a range. "1", "1,2", "[14]", "1–3".
   *
   * Starting with a digit is what keeps phospho-sites out — a superscript
   * "S473" is part of the name beside it, "473" is not — and the class stops
   * at digits and separators so "2+" in a charge stays attached too.
   */
  const FOOTNOTE_MARKER_RE = /^\s*[[(]?\s*\d[\d\s,;–—-]*\s*[\])]?\s*$/;

  /**
   * Is this text a footnote marker glued to the word before it?
   *
   * Inline tags are joined with no separator, deliberately, so a catalogue
   * number split across <span>s stays one string. Publishers also hang
   * reference markers off the end of words with the same kind of markup, and
   * the join then makes one token out of two things:
   *
   *   Western blotting<sup>1</sup>   ->  "Western blotting1"
   *   ab109535<sup>1</sup>           ->  "ab1095351"
   *
   * The first costs the application — \b cannot match inside "blotting1" — and
   * the verdict widens to all four assessed applications. The second is worse:
   * the token is in no index, so the antibody is not found at all, which is the
   * same silent miss the BMJ and Springer separators produced.
   *
   * Only <sup> and in-page anchors count. <sub> is deliberately excluded:
   * subscript digits are chemical and protein numbering — separating
   * "IP<sub>3</sub>R" would break a name rather than repair one. An <a> has to
   * point inside the page (href="#…"), which is what a reference link does and
   * what a link wrapping part of an identifier does not.
   */
  function isFootnoteMarker(el) {
    if (!el) return false;
    if (el.tagName === "SUP") return FOOTNOTE_MARKER_RE.test(el.textContent || "");
    if (el.tagName === "A") {
      const href = el.getAttribute("href") || "";
      return href.charAt(0) === "#" && FOOTNOTE_MARKER_RE.test(el.textContent || "");
    }
    return false;
  }

  /** Walk the inline chain, because Nature nests the anchor inside the sup. */
  function insideFootnoteMarker(node) {
    let el = node.parentElement;
    while (el && INLINE_TAGS.has(el.tagName)) {
      if (isFootnoteMarker(el)) return true;
      el = el.parentElement;
    }
    return false;
  }

  function buildDocumentText(nodes) {
    let text = "";
    const spans = [];
    // [{start, end, context}] in document order, one per run of text sharing a
    // block. Only blocks that actually inherit something are kept, so an ordinary
    // page of paragraphs produces an empty list and costs nothing.
    const blocks = [];
    let previousBlock = null;
    // The block record still being extended, or null when the current block
    // inherits nothing. Held explicitly rather than read off the end of `blocks`:
    // a governed block followed by an ordinary one would otherwise have its `end`
    // pushed forward again and swallow the paragraph in between.
    let open = null;
    for (const node of nodes) {
      const block = blockAncestor(node);
      if (block !== previousBlock) {
        if (open) { open.end = text.length; open = null; }
        // Separate blocks so a heading can't fuse with the paragraph under it, but
        // join text within one block exactly as the browser renders it, so an
        // identifier broken up by inline markup is still one string to match.
        if (spans.length) text += "\n";
        const context = structuralContext(block);
        if (context) {
          open = { start: text.length, end: text.length, context };
          blocks.push(open);
        }
      }
      previousBlock = block;

      // Hold a reference marker off the word before it. Added to `text` and not
      // to any span, exactly as the block separator above is, so every offset
      // stays mapped: `start` is taken after the leading space and the trailing
      // one after the span's end is recorded.
      const marker = insideFootnoteMarker(node);
      if (marker) text += " ";

      const start = text.length;
      // A newline INSIDE a text node is source formatting, not a block break —
      // the browser renders it as a space, and publishers wrap paragraphs
      // across source lines constantly. The matcher treats "\n" as the end of
      // the block it is scoping to, so leaving them in meant an application
      // named later in the *same rendered paragraph* was unreachable if the
      // publisher happened to wrap the line between the two. A Springer methods
      // paragraph naming "immunoblotting" three source lines below the reagent
      // fell back to the whole-antibody verdict and painted an antibody that
      // fails Western blot green.
      //
      // Substituted one-for-one so every offset stays exactly where it was:
      // start/end are document coordinates and the decoration maps them back
      // into these same nodes. Anything that genuinely preserves newlines
      // (<pre>, <code>) is in SKIP_TAGS and never reaches here.
      text += node.nodeValue.replace(/[\r\n]/g, " ");
      spans.push({ node, start, end: text.length });
      // Both sides, so a marker that precedes an identifier does not glue to it
      // either.
      if (marker) text += " ";
    }
    if (open) open.end = text.length;
    return { text, spans, blocks };
  }

  function scan(rootNode) {
    if (!index || !settings || !settings.enabled) return;

    const nodes = collectTextNodes(rootNode || document.body);
    if (!nodes.length) return;

    const { text, spans, blocks } = buildDocumentText(nodes);
    if (text.length > 4_000_000) return; // pathological page; bail rather than hang

    const found = M.findMentions(text, index, blocks, paper);
    if (!found.length) return;

    // Decorate back-to-front within each node so earlier offsets stay valid.
    const byNode = new Map();
    for (const hit of found) {
      if (!levelEnabled(hit.status.level)) continue;
      // A mention can straddle several text nodes when inline markup splits it.
      // Decorate each node's slice with the same hit: the underlines abut, so it
      // reads as one mark, and hovering any part opens the same card.
      for (const span of spans) {
        if (span.end <= hit.start) continue;
        if (span.start >= hit.end) break; // spans are in document order
        const from = Math.max(hit.start, span.start);
        const to = Math.min(hit.end, span.end);
        if (to <= from) continue;
        if (!byNode.has(span.node)) byNode.set(span.node, []);
        byNode.get(span.node).push({ hit, offset: from - span.start, length: to - from });
      }
    }

    for (const [node, list] of byNode) {
      list.sort((a, b) => b.offset - a.offset);
      for (const item of list) decorate(node, item);
    }

    // Discard the mutation records our own marks just generated, so inserting
    // highlights does not schedule another scan of the same page.
    if (observer) observer.takeRecords();

    notifySummary(found);
  }

  /**
   * The screen-reader half of card.js::qualifierClause.
   *
   * Yellow's whole meaning is the clause — "not supportive" alone is the red
   * label, and a reader hearing only that has been told the wrong thing. The
   * two tables are duplicated because this file must say the same words as the
   * card, and `core/tests_extension_scope.py` pins that both carry the codes
   * the index ships.
   */
  const QUALIFIER_WORDS = {
    ns: "detects the target, but is not selective",
    sd: "detects the target",
    se: "enriches the target, but not significantly",
    si: "some selective signal",
    sl: "selective",
    xs: "strongly selective",
  };

  function yellowClause(status) {
    const codes = (status.failed || [])
      .map((a) => (status.qualifiers || {})[a])
      .filter(Boolean);
    const distinct = codes.filter((c, i) => codes.indexOf(c) === i);
    return distinct.map((c) => QUALIFIER_WORDS[c]).filter(Boolean).join("; ");
  }

  function levelEnabled(level) {
    if (level === "grey") return settings.showGrey !== false;
    if (level === "blue") return settings.showBlue !== false;
    return true;
  }

  function decorate(node, { hit, offset, length }) {
    if (!node.parentNode) return;
    if (node.parentElement && node.parentElement.closest(".oga-hl")) return; // already marked
    if (offset < 0 || offset + length > node.nodeValue.length) return;

    let target = node;
    if (offset > 0) target = target.splitText(offset);
    if (target.nodeValue.length > length) target.splitText(length);

    const mark = document.createElement("mark");
    mark.className = `oga-hl ${LEVEL_CLASS[hit.status.level]}`;
    if (settings.patterns) mark.classList.add("oga-patterned");
    // One id per mention, reused across every slice of it. Without this an
    // identifier split by inline markup gets two ids and reads as two
    // antibodies when stepping through the page.
    if (hit._id === undefined) hit._id = hits.push(hit) - 1;
    mark.dataset.oga = String(hit._id);
    mark.setAttribute("tabindex", "0");
    mark.setAttribute("role", "button");
    mark.setAttribute("aria-label", ariaLabel(hit));
    target.parentNode.replaceChild(mark, target);
    mark.appendChild(target);
  }

  /**
   * What this page was seen to say — the same sentence the card leads with, so a
   * screen reader is told the page's own words and not only the verdict.
   *
   * It is a separate clause on purpose. The verdict is a hash lookup; this is a
   * reading of the prose, and the two must not sound like one claim.
   */
  // Spelled out, because this is read aloud: "WB" is a chip label on a card a
  // sighted reader can also see, and on its own it is not a word.
  const SPOKEN_APP = {
    WB: "Western blot", IP: "immunoprecipitation", IF: "immunofluorescence",
    FC: "flow cytometry", IHC: "immunohistochemistry",
  };

  function pageClause(status) {
    const named = [].concat(status.mentioned || [], status.unassessed || [])
      .map((a) => SPOKEN_APP[a] || a);
    if (!named.length) return "This page does not say which application it was used for.";
    // The caution travels with the label, not only with the card: a reader who
    // never opens the card is exactly the one who would act on a bad reading.
    const doubted = (status.uncertain || []).map((a) => SPOKEN_APP[a] || a);
    if (doubted.length) {
      const which = doubted.length === named.length
        ? "that reading is" : `the ${M.listOf(doubted)} reading is`;
      // No anchor in an aria-label, but the route still has to be spoken — the
      // card's link is invisible to a reader who never opens it.
      return `This page mentions ${M.listOf(named)}, but ${which} unreliable `
        + `— check what the paper used. More accurate matching is available via `
        + `the OGA AI connector.`;
    }
    return `This page mentions ${M.listOf(named)}.`;
  }

  /**
   * The screen-reader half of card.js's notice block, saying the same things in
   * the same order: the paper, then which protein the reagent is for, then that
   * this is not a performance verdict.
   *
   * The last clause is not padding. Red means two things now, and a listener who
   * hears only "red" would take the commoner one -- so the label has to close
   * the door the colour leaves open.
   */
  function confusionLabel(name, status) {
    const c = status.confusion;
    const mistaken = c.words.mistaken_protein;
    const paper = c.verdict === "mistaken"
      ? `This paper is on a published list of papers that used it as a ${mistaken} antibody.`
      : c.verdict === "not_checked"
        ? `This paper is on a published list of papers citing it; the review could `
          + `not reach the full text, so which protein it was used for is not known.`
        : `This paper may have used the wrong antibody.`;
    return `${name}: antibody to ${c.words.declared}, not to ${mistaken}. ${paper} `
      + `${c.words.short} Documented in ${c.words.source}. OGA has not tested this `
      + `antibody; this is about which protein it is raised against, not how well `
      + `it works.`;
  }

  function ariaLabel(hit) {
    const name = hit.record ? hit.record.n : hit.matched;
    // The record, never the page's guess at which slice of it applies — the
    // guess named an application the paper had not used on 17 of 54 papers.
    const passed = (hit.status.passed || []).join(", ");
    const failed = (hit.status.failed || []).join(", ");
    const page = hit.record ? " " + pageClause(hit.status) : "";
    // Before the switch: the level is `red` and the two things red means are
    // read aloud differently.
    if (hit.status.reason === "target-confusion" && hit.status.confusion) {
      return confusionLabel(name, hit.status);
    }
    switch (hit.status.level) {
      // The same qualifier the card draws. Fixing the wording on one surface
      // and not the other is how a reader gets two different answers about one
      // row -- the reason verdictClause exists at all.
      case "green": return `${name}: characterisation data supports ${passed} ${conditions()}. Knockout-controlled data available.${page}`;
      case "yellow": return `${name}: limited support for ${failed} ${conditions()} — ${yellowClause(hit.status)}.${page}`;
      case "red": return `${name}: tested, data not supportive for ${failed} ${conditions()}.${page}`;
      case "mixed": return `${name}: supports ${passed}, not supportive for ${failed} ${conditions()}.${page}`;
      case "blue": return blueLabel(name, hit);
      default: return greyLabel(name, hit.status);
    }
  }

  /**
   * The screen-reader half of card.js::blueExplain, and it has to say the
   * same thing in the same register.
   *
   * The label used to assert the target with nothing to show for it — "tested
   * alternatives against SQSTM1 exist" — so naming the words it was read from
   * is a real gain. What it must not do is warn: the audit found 0 of 45 blue
   * marks with a wrong gene, and a reader who hears "check it matches" on
   * every one learns to discount all of them.
   */
  function blueLabel(name, hit) {
    const gene = hit.status.gene;
    const raw = hit.target && hit.target.raw;

    let source;
    if (!raw) {
      source = "taken from this page's own text";
    } else if (hit.targetVia === "nearby") {
      source = `taken from "${raw}", the closest target name in this block`;
    } else if (hit.via === "target") {
      source = `taken from the name in this mention, "${raw}"; no catalogue number is given for it`;
    } else {
      source = `taken from "${raw}" next to this catalogue number`;
    }

    return `${name}: ${gene} has knockout-controlled antibodies you can use instead. `
      + `Target ${source}. This exact antibody is not in the dataset, and `
      + `absence is not a verdict on quality.`;
  }

  /**
   * The screen-reader half of the same fix as card.js::greyHeadline. This said
   * "no independent data. Untested, not a verdict on quality." about antibodies
   * that are in the dataset and failed knockout-controlled testing, which is
   * the opposite of what we hold. A reader who only ever hears this label got
   * no other chance to learn it.
   */
  /**
   * The words binding a verdict to what produced it, from the index.
   *
   * `core/recommendations.py::CONDITIONS_QUALIFIER`, with the same fallback
   * card.js carries, for an install whose cached index predates the key.
   */
  function conditions() {
    return (index && index.conditionsQualifier) || "under the consensus protocols";
  }

  function greyLabel(name, status) {
    const clause = M.verdictClause(status.record);
    if (!clause) return `${name}: no independent data. Untested, not a verdict on quality.`;

    const used = (status.mentioned || []).join(", ");
    const lead = used
      ? `not tested in ${used}, the application this page uses it for`
      : `not assessed for the application this page uses it for`;
    return `${name}: ${lead}; tested and ${clause} ${conditions()}.`;
  }

  /* ------------------------------------------------------------ interaction */

  function hitFromEvent(e) {
    const el = e.target && e.target.closest && e.target.closest(".oga-hl");
    if (!el) return null;
    const i = Number(el.dataset.oga);
    return Number.isInteger(i) && hits[i] ? { el, hit: hits[i] } : null;
  }

  document.addEventListener("mouseover", (e) => {
    const found = hitFromEvent(e);
    if (found) Card.show(found.el, found.hit);
  }, true);

  document.addEventListener("mouseout", (e) => {
    if (e.target && e.target.closest && e.target.closest(".oga-hl")) Card.scheduleHide();
  }, true);

  document.addEventListener("focusin", (e) => {
    const found = hitFromEvent(e);
    if (found) Card.show(found.el, found.hit);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") Card.hide();
  });

  /* ---------------------------------------------------------------- summary */

  let lastSummary = null;

  /**
   * The paper's DOI, read off the page's own citation metadata. Used for one
   * thing only: the popup's hand-off button, so a reader can paste the paper
   * into the OGA MCP server and ask it about controls there. It is never sent
   * anywhere from here — the clipboard copy is the reader's own action.
   */
  function pageDoi() {
    for (const name of ["citation_doi", "DC.Identifier", "dc.identifier"]) {
      const el = document.querySelector(`meta[name="${name}" i]`);
      const value = el && el.content && el.content.trim();
      if (value) return value.replace(/^(?:doi:|https?:\/\/(?:dx\.)?doi\.org\/)/i, "");
    }
    return null;
  }

  function notifySummary(found) {
    const counts = { green: 0, red: 0, mixed: 0, blue: 0, grey: 0 };
    for (const h of found) counts[h.status.level] = (counts[h.status.level] || 0) + 1;
    /* How many of the red ones are NOT a test result.
     *
     * The red tile's own face reads "not supportive", and a declared-target
     * notice is level red — so a paper citing `ab9361` and nothing else drew
     * `1 NOT SUPPORTIVE` on a page where OGA has tested nothing at all. Seen on
     * oncotarget 17778, 12 Sep 2026. The tile's tooltip names both meanings of
     * red, and a tooltip is not the label: a count is a claim about the page.
     *
     * Counted rather than split into a seventh tile, which is the owner's
     * decision about the colour (one red, the card's first line separates the
     * two) and it holds for the tally as well. The panel says the number out
     * loud underneath instead. */
    const notices = found.filter(
      (h) => h.status.reason === "target-confusion").length;

    lastSummary = {
      counts,
      notices,
      total: found.length,
      /* WORTH A LOOK is a list of REAGENTS, where the tiles above it are a count
         of MARKS, and the two are different questions. A supplier catalogue
         names one product in every row, so the unfiltered map listed `MA1-510`
         six times under a tile reading 7 — a count and the list it totals
         disagreeing on one screen, which reads as the panel being broken.
         Deduplicated on the reagent, first mark wins.

         It also has to say WHICH of the things red means. A declared-target
         notice is level `red` and carries no failed applications, so it came
         out as a bare "not recommended" — a performance claim about a reagent
         OGA has never tested, in the one place on this panel that makes a
         claim in words. `kind` is what the popup branches on.

         And a `mixed` entry needs both halves. The card's own headline is
         "Supports IP — not supportive for WB", and a list that prints only the
         failures turns a split verdict into a blanket negative. */
      concerns: (() => {
        const seen = new Set();
        const out = [];
        for (const h of found) {
          const confusion = h.status.reason === "target-confusion";
          if (!confusion && h.status.level !== "red"
              && h.status.level !== "mixed") continue;
          const name = h.record ? h.record.n : h.matched;
          const gene = h.record ? h.record.g : (h.status.gene || null);
          const key = `${name}|${gene || ""}|${confusion ? "c" : "v"}`;
          if (seen.has(key)) continue;
          seen.add(key);
          const entry = { name, gene, failed: h.status.failed || [],
                          passed: h.status.passed || [] };
          if (confusion) {
            entry.kind = "wrong-target";
            // The protein it IS raised against, so the line can name it. The
            // popup says nothing about performance on these.
            entry.declared = h.status.confusion.words.declared;
            entry.mistakenFor = h.status.confusion.words.mistaken_protein;
            entry.gene = null;
          }
          out.push(entry);
        }
        return out;
      })(),
      doi: pageDoi(),
      url: location.href,
      indexGenerated: index ? index.generated : null,
      // Sent up so the popup can carry the same caveat as the card without a
      // second copy of the wording (core/recommendations.py::SCOPE_SHORT).
      scope: index ? index.scopeShort : null,
      // BOTH halves, because the popup has no verdict to bind the qualifier to.
      // `SCOPE_SHORT` stopped naming the consensus protocols on 3 Sep 2026 --
      // the card now says that inside each verdict -- and this panel draws
      // counts rather than verdicts, so without this key the popup would be the
      // one surface that dropped the fact entirely, silently.
      conditions: conditions(),
    };

    try {
      chrome.runtime.sendMessage({ type: "oga:summary", summary: lastSummary });
    } catch (_) { /* popup not open */ }
  }

  // Cursor per colour, so repeated clicks on a popup tile walk through that
  // colour's marks in document order and wrap around at the end.
  const focusCursor = {};

  function focusNext(level) {
    const selector = "mark." + LEVEL_CLASS[level];
    // One entry per mention, not per <mark>: an identifier split by inline
    // markup is several abutting marks but one antibody, and stepping through
    // it twice would feel broken.
    const seen = new Set();
    const found = [];
    for (const el of document.querySelectorAll(selector)) {
      const id = el.dataset.oga;
      if (seen.has(id)) continue;
      seen.add(id);
      found.push(el);
    }
    if (!found.length) return { count: 0, at: 0 };

    const next = (focusCursor[level] ?? -1) + 1;
    const i = next >= found.length ? 0 : next;
    focusCursor[level] = i;

    // Only ever one thing pulsing, so clicking two colours in quick succession
    // does not leave the reader with two competing highlights.
    for (const stale of document.querySelectorAll("mark.oga-focus")) {
      stale.classList.remove("oga-focus");
    }

    const el = found[i];
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    // A brief pulse, because scrolling to a one-word underline mid-page
    // otherwise leaves you hunting for what moved.
    el.classList.remove("oga-focus");
    void el.offsetWidth; // restart the animation if it is already running
    el.classList.add("oga-focus");
    setTimeout(() => el.classList.remove("oga-focus"), 1600);

    return { count: found.length, at: i + 1 };
  }

  chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
    if (msg && msg.type === "oga:focus") {
      respond(focusNext(msg.level));
      return true;
    }
    if (msg && msg.type === "oga:get-summary") {
      respond(lastSummary || { counts: {}, total: 0, concerns: [] });
      return true;
    }
    if (msg && msg.type === "oga:rescan") {
      location.reload();
      return false;
    }
    return false;
  });

  /* ------------------------------------------------------------------ boot */

  function scheduleScan(rootNode) {
    if (scanScheduled) return;
    scanScheduled = true;
    // requestIdleCallback keeps us off the critical path on heavy journal pages.
    const run = () => { scanScheduled = false; try { scan(rootNode); } catch (err) { console.debug("[OGA]", err); } };
    if ("requestIdleCallback" in window) requestIdleCallback(run, { timeout: 1500 });
    else setTimeout(run, 300);
  }

  // Wait for the page to settle before rescanning -- but not forever. A
  // publisher page is never quiet: citation counts, Altmetric badges, lazy
  // images and cookie banners keep adding nodes for as long as the tab is open,
  // and a debounce that restarts on every batch never fires at all. Europe PMC
  // injects the article body only when "Free full text" is opened, so the body
  // arrived into a page that was still mutating and was never scanned: zero
  // highlights on a paper naming nine antibodies, with nothing on screen saying
  // the scan had not happened. A reader who opens full text and sees nothing
  // concludes the tool has nothing to say.
  //
  // So the settle timer has a ceiling. Whatever else the page is doing, the
  // first mutation after a scan is followed by a scan within RESCAN_MAX_MS.
  const RESCAN_SETTLE_MS = 500;
  const RESCAN_MAX_MS = 2000;

  function watch() {
    if (observer) return;
    let pending = null;
    let ceiling = null;
    const rescan = () => {
      clearTimeout(pending);
      clearTimeout(ceiling);
      pending = null;
      ceiling = null;
      scheduleScan(document.body);
    };
    observer = new MutationObserver((records) => {
      // Publisher SPAs swap the article body in after load; rescan the additions.
      // Anything we added ourselves is not new content.
      const added = records.some((r) =>
        [...(r.addedNodes || [])].some((n) => {
          const el = n.nodeType === Node.ELEMENT_NODE ? n : n.parentElement;
          return !el || !el.closest(".oga-hl, .oga-card-host");
        }));
      if (!added) return;
      clearTimeout(pending);
      pending = setTimeout(rescan, RESCAN_SETTLE_MS);
      if (ceiling === null) ceiling = setTimeout(rescan, RESCAN_MAX_MS);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // Read ONCE. It was computed twice -- for the message and again to confirm the
  // answer -- and the declared-target notices need a third reading, of the DOI.
  // Three calls parsing the same <meta> tags is how two of them come to disagree
  // after somebody widens the list of tags one of them reads.
  const pageKeys = P ? P.pageKeys(document) : null;

  chrome.runtime.sendMessage({
    type: "oga:get-index",
    // Computed here because only the content script can see the page, and
    // because src/paper.js -- the single copy of the title rules -- is loaded
    // here and deliberately not in the worker. What crosses this boundary is
    // three resolved keys out and at most one paper's record back, never the
    // table.
    keys: pageKeys,
  }, (resp) => {
    if (chrome.runtime.lastError || !resp || !resp.index) return;
    index = M.prepareIndex(resp.index);
    // The caveat travels with the data (core/recommendations.py::SCOPE_SHORT),
    // so the card is told once here rather than being handed it per hit.
    if (Card.setScope) Card.setScope(index);
    settings = resp.settings || { enabled: true };
    // A response with no `paper` key is an older worker, which is "could not be
    // checked" and NOT "this paper is not in the record".
    paper = P
      ? P.confirmPaper(resp.paper, pageKeys)
      : { status: "unavailable", reason: "no-paper-module" };
    // THE THREE WAYS THIS PAGE NAMES ITSELF, carried alongside the citation
    // answer because the declared-target lists are keyed in the index itself and
    // not in the citation table -- so the lookup needs no worker round trip and
    // no second copy of the paper rules.
    //
    // It was the DOI alone until 0.4.2, which is exactly as far as a publisher's
    // own page gets you and no further: four of the PERK papers are e-Century
    // titles that register no DOI with Crossref at all, so a paper the reviewer
    // HAD established was reachable by nothing. The PMID is free on PubMed, PMC
    // and Europe PMC; the title key is what works on the publisher's page, which
    // is where a reader usually is and which rarely declares a PMID.
    //
    // All four fields, absent where the page declares nothing, and a notice with
    // no paper match falls back to its product half exactly as before.
    if (paper && pageKeys) {
      paper.pageDoi = pageKeys.doi || null;
      paper.pagePmid = pageKeys.pmid || null;
      paper.pageTitleKey = pageKeys.titleKey || null;
      paper.pageYear = pageKeys.year || null;
    }
    if (!settings.enabled) return;
    scheduleScan(document.body);
    watch();
  });
})();
