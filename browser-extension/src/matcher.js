/**
 * Identifier extraction and verdict resolution.
 *
 * Everything in this file is deterministic string work — regexes and hash
 * lookups against the bundled index. There is no model call and no network
 * call anywhere in the matching path, so scanning a page costs nothing and
 * the page's text never leaves the browser.
 */
(function (root) {
  "use strict";

  // Verdict codes, matching tools/build_extension_index.py.
  const NOT_TESTED = 0;
  const NOT_RECOMMENDED = 1;
  const RECOMMENDED = 2;

  // The four applications assessed under the consensus protocols.
  const ASSESSED = ["WB", "IP", "IF", "FC"];

  /**
   * The applications this detector is actually good at. Measured, per
   * application, on the 100-paper re-run of 2 August 2026:
   *
   *   WB   39 of 45 namings correct (87%)
   *   IF    3 of 15 namings correct (20%)
   *   IP    0 of 3 — named three times on papers that never used it
   *   IHC   never named at all, on 25 papers that used it
   *   FC    never named, on 3
   *
   * So it is a western-blot detector that occasionally says something else, and
   * the something else is usually wrong. A reader is told which of the two they
   * are looking at rather than being given one hedge over everything: hedging the
   * reliable signal too would only teach them to discount it, which is the same
   * reasoning that kept cautionary language OFF the amber card, where 45 of 45
   * attributions were right.
   */
  const RELIABLE_CUES = ["WB"];

  /** The named applications whose detection is not trustworthy. */
  function uncertainApplications(apps) {
    return (apps || []).filter((a) => !RELIABLE_CUES.includes(a));
  }

  /**
   * Application cues, longest/most specific first. IHC is deliberately listed
   * even though it is NOT one of the assessed applications: a paper that used
   * an antibody for IHC must not inherit its Western blot verdict, so we need
   * to recognise IHC in order to refuse to colour it.
   */
  const APP_CUES = [
    // Stems, not whole words: papers say "immunohistochemical staining" at least
    // as often as "immunohistochemistry", and "-istr" only ever spelled the
    // noun. That typo made the IHC cue inert, which is the one cue whose whole
    // job is to REFUSE a verdict — so an antibody a paper used for IHC was
    // being shown its Western blot result instead.
    [/\bimmuno-?histo-?chem(?:istry|ical(?:ly)?)\b|\bIHC(?:-P|-Fr)?\b|\bparaffin\s+section/gi, "IHC"],
    [/\bwestern\s*blot(?:ting|ted|s)?\b|\bimmunoblot(?:ting|ted|s)?\b|\bWB\b/gi, "WB"],
    [/\bco-?immunoprecipitat(?:ion|ed|ing)\b|\bimmunoprecipitat(?:ion|ed|ing)\b|\bco-?IP\b|\bpull-?down\b|\bIP\b/gi, "IP"],
    [/\bimmunofluorescen(?:ce|t(?:ly)?)\b|\bimmuno-?cyto-?chem(?:istry|ical(?:ly)?)\b|\bICC(?:-IF)?\b|\bimmunostain(?:ing|ed)\b|\bconfocal\b|\bIF\b/gi, "IF"],
    [/\bflow\s+cytometr(?:y|ic)\b|\bFACS\b|\bfluorescence-activated\s+cell\s+sorting\b/gi, "FC"],
  ];

  /**
   * A word inside a target phrase.
   *
   * A full stop is allowed only *between* characters: "p38.MAPK" is a name,
   * "Parkin. i" is the end of one sentence and the start of the next. The
   * class used to include a bare `.`, so the four-word capture below ran
   * straight through a sentence boundary — on a real Cell Death & Disease
   * figure legend it swallowed "antibodies against PINK1 and Parkin. i " and
   * ended on a cell line, printing that whole string as the card's headline.
   */
  const TARGET_WORD = "[A-Za-z0-9](?:[A-Za-z0-9\\-/]|\\.(?=[A-Za-z0-9]))";

  // "anti-X", "X antibody", "antibody against/to X" — how papers name a target
  // when they do not give a catalogue number.
  //
  // The `d` flag is on so the capture group's offset is exact: the mark is
  // shrunk to the words that actually resolved, and locating the group by
  // searching for its text would be a guess.
  const TARGET_PATTERNS = [
    new RegExp("\\banti[-\\s](" + TARGET_WORD + "{1,24})\\b", "gdi"),
    // Up to four words, because targets are routinely spelled out in full:
    // "antibodies against amyloid precursor protein (APP, ab101492)". Capturing
    // one word grabbed "amyloid" and resolved nothing, losing the amber.
    new RegExp(
      "\\bantibod(?:y|ies)\\s+(?:against|to|recognis?zing|directed\\s+against)\\s+" +
      "((?:" + TARGET_WORD + "*(?:\\s+|$)){1,4})", "gdi"),
    new RegExp("\\b([A-Z][A-Za-z0-9\\-]{1,15})\\s+antibod(?:y|ies)\\b", "gd"),
  ];

  /**
   * Typesetting a catalogue number picks up on the way to the page.
   *
   * Two of the largest publishers in the match list made the matcher blind,
   * both verified live in the DOM. BMJ sets Proteintech's 14060-1-AP as
   * `14 060-1-AP`, using a space as a thousands separator; Springer sets it
   * `14,060–1-AP`, with a comma and an en-dash where the number has a hyphen.
   * The tokeniser split at the space and at the comma, so neither half was in
   * the index, and even rejoined the en-dash failed the key. Three OGA
   * antibodies were lost on one BMJ page and two on one Springer page; both
   * scored zero marks while listing characterised antibodies. These are house
   * styles at two large publishers, not one-off typos.
   */
  // Every dash a typesetter might put where the catalogue number has a hyphen.
  const DASH_CLASS = "\\-\\u2010\\u2011\\u2012\\u2013\\u2014\\u2015\\u2212";
  const DASH_RE = new RegExp("[" + DASH_CLASS + "]", "g");

  // Invisible characters, which mean nothing wherever they turn up.
  const ZERO_WIDTH = "\\u200b\\u200c\\u200d\\ufeff\\u00ad";
  const ZERO_WIDTH_RE = new RegExp("[" + ZERO_WIDTH + "]", "g");

  /**
   * Digit-group separators. A full stop is deliberately NOT one: between
   * digits it is nearly always a decimal point, and stripping it would turn a
   * concentration into a catalogue number.
   */
  const GROUP_SEP = ",\\u0020\\u00a0\\u2009\\u200a\\u202f";

  /**
   * A separator only counts where it is doing a thousands separator's job: a
   * digit on the left, and exactly three digits on the right that are not
   * themselves followed by another digit.
   *
   * The group-of-three rule is what keeps the widened tokeniser honest.
   * Without it "…reported in 2019 14060-1-AP was used…" tokenises as the
   * single lump `2019 14060-1-AP`, which is in no index — so widening the
   * tokeniser would have lost the very identifier it was widened to find.
   */
  const GROUP_SEP_LOOKAROUND = "(?<=\\d)[" + GROUP_SEP + "](?=\\d{3}(?!\\d))";
  const GROUP_SEP_RE = new RegExp(GROUP_SEP_LOOKAROUND, "g");

  /**
   * Strip typesetting from a candidate identifier — for lookup only.
   *
   * The caller keeps the original offsets, because a mark has to land on the
   * characters the page actually shows: `14 060-1-AP` is highlighted including
   * its space, and looked up as `14060-1-ap`.
   */
  function normaliseIdentifier(token) {
    return String(token == null ? "" : token)
      .replace(ZERO_WIDTH_RE, "")
      .replace(GROUP_SEP_RE, "")
      .replace(DASH_RE, "-");
  }

  /** Index key for an identifier as the page happened to typeset it. */
  function identifierKey(token) {
    return normaliseKey(normaliseIdentifier(token));
  }

  /**
   * The last-resort key: alphanumerics only.
   *
   * normaliseIdentifier handles typesetting a publisher ADDS. It cannot handle
   * one that MOVES the number's own punctuation, and that happens too: Thermo's
   * MA5-11154 is printed MA511154 and Proteintech's 11820-1-AP as 11820-1AP. A
   * hyphen the paper dropped cannot be put back, so both sides collapse to
   * alphanumerics and are compared there.
   *
   * Below MIN_COLLAPSED characters it returns "" and no comparison happens:
   * catalogue numbers are not unique across suppliers, and a short collapsed key
   * would match any of them.
   *
   * Mirrored in `mcp_servers/common/manuscript.py::collapse_identifier`. One
   * printed string must not resolve here and come back absent from the server.
   */
  const MIN_COLLAPSED = 5;

  function collapseIdentifier(token) {
    const collapsed = normaliseKey(normaliseIdentifier(token)).replace(/[^a-z0-9]/g, "");
    return collapsed.length >= MIN_COLLAPSED ? collapsed : "";
  }

  // Tokens that could plausibly be a catalogue number. Deliberately broad —
  // the index Set does the real filtering. A separator survives tokenisation
  // only where it sits between digit groups; zero-width junk always does.
  const TOKEN_RE = new RegExp(
    "[A-Za-z0-9]" +
    "(?:[A-Za-z0-9" + DASH_CLASS + "_.]" +
    "|[" + ZERO_WIDTH + "]" +
    "|" + GROUP_SEP_LOOKAROUND + "){2,}",
    "g"
  );

  // Dash variants are accepted in the RRID prefix for the same reason: a
  // typesetter that turns a hyphen into an en-dash does not stop at catalogue
  // numbers. normaliseRrid folds them all back to the underscore.
  const RRID_RE = new RegExp(
    "\\bRRID\\s*:?\\s*(AB[_" + DASH_CLASS + "]\\d{3,})\\b" +
    "|\\b(AB[_" + DASH_CLASS + "]\\d{4,})\\b",
    "gi"
  );

  /**
   * "NR3C1(#A2164)" — a target named immediately against a catalogue number we
   * do not hold.
   *
   * A catalogue number on its own tells us nothing when it is not in the index:
   * numbers are not unique across suppliers and there is no way to attribute one
   * to a target, so the catalogue pass drops it. That is right in general and
   * wrong for the single commonest way a methods section is written, where the
   * paper hands us the attribution directly. Reading it off the page is not a
   * guess.
   *
   * Group 1 is the target, group 2 the catalogue number. The gap between them
   * has to stay narrow, because width is the only thing separating this from
   * "any number near any gene name":
   *   NR3C1(#A2164)                     bare
   *   BNIP3 (#ab109362)                 spaced
   *   LC3B (E5Q2K) (#83506)             clone in between
   *   TDP-43 (Abcam, cat# ab109535)     supplier and a cat prefix
   * Greek letters are in the target class for "β-actin(#81115)".
   */
  const GENE_THEN_CAT_RE = new RegExp(
    "([A-Za-z\\u0370-\\u03ff][A-Za-z0-9\\u0370-\\u03ff\\-/.]{1,24})" +  // target
    "\\s*(?:\\([^()]{1,20}\\)\\s*)?" +                                 // optional clone
    "\\(\\s*" +
    "(?:[A-Za-z][A-Za-z .&'-]{1,24}[,;]\\s*)?" +                       // optional supplier
    "(?:cat(?:alogue|alog)?\\.?\\s*(?:no\\.?|number)?\\s*)?#?\\s*" +   // optional cat# prefix
    // Santa Cruz numbers every product sc-271462, and the hyphen after the
    // letters made the whole supplier unmatchable. Separators and dash
    // variants are tolerated here on the same terms as TOKEN_RE, so a
    // publisher's typesetting cannot hide an amber either.
    "([A-Za-z]{0,4}[" + DASH_CLASS + "]?[0-9]" +
    "(?:[A-Za-z0-9" + DASH_CLASS + "_.]|" + GROUP_SEP_LOOKAROUND + "){2,})" +  // catalogue number
    "\\s*\\)",
    "g"
  );

  // Four digits in a plausible year range, which the catalogue pattern would
  // otherwise happily read out of "TDP-43 (2019)".
  const YEAR_RE = /^(?:19|20)\d{2}$/;

  // Words that signal we are near an antibody mention. Used to gate weak
  // identifiers (bare numbers, short codes) that would otherwise fire on page
  // numbers, years and concentrations.
  const CONTEXT_RE = /\b(antibod(?:y|ies)|anti-|cat(?:alogue|alog)?\s*(?:no\.?|#|number)?|clone|RRID|monoclonal|polyclonal|immunogen|dilution|host|IgG|Abcam|Thermo|Invitrogen|Cell\s+Signaling|Proteintech|GeneTex|Novus|Bio-?Techne|R&D|Sigma|Merck|Millipore|Santa\s+Cruz|BioLegend|ABclonal|Atlas)\b/i;

  const CONTEXT_WINDOW = 120; // characters either side

  function normaliseKey(s) {
    return String(s || "").trim().toLowerCase();
  }

  function normaliseRrid(s) {
    return normaliseIdentifier(s).trim().toUpperCase()
      .replace(/^RRID:?\s*/i, "").replace(/-/g, "_");
  }

  /** A weak identifier needs antibody-ish words nearby before we trust it. */
  function isWeakIdentifier(token) {
    return /^\d+$/.test(token) || token.replace(/[^A-Za-z0-9]/g, "").length < 5;
  }

  function hasContext(text, start, end) {
    const from = Math.max(0, start - CONTEXT_WINDOW);
    const to = Math.min(text.length, end + CONTEXT_WINDOW);
    return CONTEXT_RE.test(text.slice(from, to));
  }

  /**
   * Words that mean "this is an antibody", as opposed to CONTEXT_RE's looser
   * "this is a reagent someone bought". "cat no." and a supplier name are in
   * CONTEXT_RE and are satisfied by an enzyme, a dye or a cell line, so any
   * mark we *infer* rather than look up has to clear this higher bar instead.
   */
  const ANTIBODY_RE = /\b(?:antibod(?:y|ies)|antiser(?:um|a)|monoclonal|polyclonal|IgG|clone|immunogen)\b|\banti-/i;
  const ANTIBODY_WINDOW = 90;

  function hasAntibodyContext(text, start, end) {
    // Near window: "anti-TDP-43 antibody (Abcam, cat# ab109535)".
    const from = Math.max(0, start - ANTIBODY_WINDOW);
    const to = Math.min(text.length, end + ANTIBODY_WINDOW);
    if (ANTIBODY_RE.test(text.slice(from, to))) return true;

    // Enclosing sentence: the commonest form by far is a methods section that
    // says "the primary antibodies were listed below:" once and then runs
    // twenty reagents together, semicolon-separated. By the twentieth the word
    // is hundreds of characters away, and distance alone throws the whole list
    // away. Bounded by the block so a neighbouring paragraph can never vouch
    // for this one.
    let blockStart = text.lastIndexOf("\n", Math.max(0, start - 1));
    blockStart = blockStart === -1 ? 0 : blockStart + 1;
    let blockEnd = text.indexOf("\n", end);
    if (blockEnd === -1) blockEnd = text.length;

    const sentenceStart = Math.max(blockStart, text.lastIndexOf(".", Math.max(0, start - 1)) + 1);
    let sentenceEnd = text.indexOf(".", end);
    if (sentenceEnd === -1 || sentenceEnd > blockEnd) sentenceEnd = blockEnd;
    return ANTIBODY_RE.test(text.slice(sentenceStart, sentenceEnd + 1));
  }

  /**
   * Units and dilutions, which sit exactly where a catalogue number sits.
   *
   * "A71 antibody (200 ng/ml, 1:1000)" put a real verdict on a concentration:
   * 200 is a clone ID in the dataset, the sentence says "antibody", and every
   * gate passed. What distinguishes the two is not the number, it is what
   * follows it.
   */
  const UNIT_RE = /^\s*(?:ng|µg|ug|mg|kg|g|mL|ml|µL|uL|ul|L|nM|µM|mM|nm|mm|cm|µm|um|%|min|hr?s?|sec|s|°\s*C|kDa|bp|kb|rpm|U|IU|mmol|mol|M|x\s*g|×\s*g|days?|weeks?)\b/;

  function looksLikeMeasurement(text, start, end) {
    if (/1\s*:\s*$/.test(text.slice(Math.max(0, start - 6), start))) return true;  // 1:1000
    return UNIT_RE.test(text.slice(end, end + 12));
  }

  /**
   * Is a clone ID distinctive enough to match on at all?
   *
   * Clone IDs are supplier-assigned and some are barely identifiers: the live
   * data holds "5", "166", "200", "208" and "515". A bare short number carries
   * no information and will collide with concentrations, volumes and figure
   * numbers forever. Long numeric clones (671834) and ordinary alphanumeric
   * ones (EPR5810, D11A10) are safe.
   *
   * Short ones like "DB9" or "4F9" are real but risky, so they are not dropped
   * outright -- they are only trusted where the paper says "clone".
   */
  function cloneIsDistinctive(token) {
    const bare = token.replace(/[^A-Za-z0-9]/g, "");
    if (/^\d+$/.test(bare)) return bare.length >= 5;
    return bare.length >= 4;
  }

  /**
   * Which applications are in play at a given offset. Looks in the enclosing
   * sentence first, then widens to a paragraph-sized window. Returns [] when
   * nothing is stated — the caller then falls back to a whole-antibody verdict.
   */
  function inferApplications(text, offset, blocks) {
    // Blocks are separated by \n when the caller assembles the page text, so a
    // newline marks the end of the paragraph, heading or list item we are in.
    let blockEnd = text.indexOf("\n", offset);
    if (blockEnd === -1) blockEnd = text.length;

    // A sentence cannot start in a previous block, any more than it can end in
    // the next one — `inferTarget` has always scoped itself this way and this did
    // not. It went unnoticed while pages were prose, because the fallback window
    // below reaches the same text anyway. A table has no full stops at all, so
    // "the enclosing sentence" became *everything from the top of the document*,
    // and the first reagent's application was handed to every row beneath it.
    let blockStart = text.lastIndexOf("\n", Math.max(0, offset - 1));
    blockStart = blockStart === -1 ? 0 : blockStart + 1;

    const sentenceStart = Math.max(blockStart, text.lastIndexOf(".", offset - 1) + 1);
    let sentenceEnd = text.indexOf(".", offset);
    if (sentenceEnd === -1 || sentenceEnd > blockEnd) sentenceEnd = blockEnd;

    // Within one sentence, several applications can genuinely apply
    // ("for western blotting and immunofluorescence we used X"), so take them all.
    const inSentence = scanApplications(text.slice(sentenceStart, sentenceEnd + 1));
    if (inSentence.length) return uniqueApps(inSentence);

    // Next, anything this block inherits from where it SITS — a table cell's
    // column header and its own row, a figure caption. The caller supplies it
    // because it is a fact about the layout, which the flattened text has lost.
    //
    // It is consulted before the character window below, and that order is the
    // point: a column header is the page STATING the application, where the
    // window is us guessing from proximity. In a reagent table the window is
    // actively wrong — it reaches the rows above, so it would read a neighbouring
    // reagent's application and produce a confident wrong verdict.
    //
    // Several can apply here for the same reason they can in a sentence: a row
    // reading "WB, IF" means both. Taken whole, not nearest-wins.
    const context = blockContext(blocks, offset);
    if (context) {
      const stated = scanApplications(context);
      if (stated.length) return uniqueApps(stated);
    }

    // Otherwise fall back to surrounding context: methods sections usually name
    // the application once in a heading and list reagents underneath. Look back
    // across block boundaries to reach that heading, but never look forward past
    // the end of the current block — the next heading belongs to the next
    // section and its application is not this reagent's.
    const wideFrom = Math.max(0, offset - 600);
    const nearby = scanApplications(text.slice(wideFrom, blockEnd), wideFrom);
    if (!nearby.length) return [];

    // This is a guess rather than a statement, so trust only the closest cue.
    let best = nearby[0];
    let bestDist = Math.abs(nearby[0].at - offset);
    for (const cue of nearby.slice(1)) {
      const dist = Math.abs(cue.at - offset);
      if (dist < bestDist) { best = cue; bestDist = dist; }
    }
    return [best.app];
  }

  /**
   * The structural context governing `offset`, or "" — binary search, because a
   * long reagent table is thousands of blocks and every mention asks.
   *
   * Optional throughout: `findMentions` is called with strings in the unit tests
   * and by anything that has no DOM, and must behave exactly as before without it.
   */
  function blockContext(blocks, offset) {
    if (!blocks || !blocks.length) return "";
    let lo = 0;
    let hi = blocks.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const b = blocks[mid];
      if (offset < b.start) hi = mid - 1;
      else if (offset >= b.end) lo = mid + 1;
      else return b.context;
    }
    return "";
  }

  /** Returns [{app, at}] with `at` as an absolute offset when `base` is given. */
  /**
   * EVERY occurrence of every cue in the slice, not the first of each.
   *
   * It used to stop at the first, which is fine for the two callers that only
   * want the set of applications named — and quietly wrong for the one that
   * picks the CLOSEST cue, because "closest" was then computed over
   * first-occurrences. On a real methods page the window reaches back into the
   * reagent table above, so WB's recorded position was the table's first row
   * while IF's was its second: the nearer of two distant cues won, and the
   * `<h3>Western blotting</h3>` sitting immediately over the paragraph was never
   * considered at all. An antibody recommended for the blot it was used in was
   * painted red for an immunofluorescence experiment on the other side of the
   * page.
   */
  function scanApplications(slice, base = 0) {
    const found = [];
    for (const [re, app] of APP_CUES) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(slice)) !== null) {
        found.push({ app, at: base + m.index });
        if (re.lastIndex === m.index) re.lastIndex++;   // never on these cues, but
      }
    }
    return found;
  }

  /** The applications named, in cue order, without repeats. */
  function uniqueApps(cues) {
    const seen = [];
    for (const c of cues) if (!seen.includes(c.app)) seen.push(c.app);
    return seen;
  }

  /** Best guess at the target a mention refers to, as an official gene symbol. */
  function inferTarget(text, offset, index) {
    // Never read a target out of a neighbouring block. In a Key Resources table
    // every row is its own text node, so without this an RRID in the anti-mCherry
    // row happily adopts "anti-p62" from the row below and claims SQSTM1 — an
    // amber verdict attached to entirely the wrong reagent.
    let blockStart = text.lastIndexOf("\n", Math.max(0, offset - 1));
    blockStart = blockStart === -1 ? 0 : blockStart + 1;
    let blockEnd = text.indexOf("\n", offset);
    if (blockEnd === -1) blockEnd = text.length;

    const from = Math.max(blockStart, offset - 200);
    const to = Math.min(blockEnd, offset + 120);
    const slice = text.slice(from, to);

    const candidates = [];
    for (const source of TARGET_PATTERNS) {
      // A fresh instance: findMentions may be part-way through iterating these
      // same patterns, and sharing lastIndex across the two would corrupt both.
      const re = new RegExp(source.source, source.flags);
      let m;
      while ((m = re.exec(slice)) !== null) {
        if (m[1]) candidates.push({ raw: m[1], at: Math.abs(from + m.index - offset) });
      }
    }
    // Prefer the candidate physically closest to the identifier.
    candidates.sort((a, b) => a.at - b.at);

    for (const cand of candidates) {
      const named = resolveGenePhrase(cand.raw, index);
      if (named) return { gene: named.gene, raw: named.phrase };
    }
    return null;
  }

  /**
   * Resolve a captured phrase to a gene, and say which words did it.
   *
   * A captured phrase may carry trailing words that are not part of the name
   * ("amyloid precursor protein overnight"). Try the longest form first, then
   * drop words from the end — longest wins, so "amyloid precursor protein"
   * resolves before the useless bare "amyloid".
   *
   * The phrase that resolved is known here and nowhere else, and the amber card
   * quotes it back to the reader as its evidence. Quoting the whole capture
   * instead would print the trailing junk and make a correct inference look
   * broken.
   */
  function resolveGenePhrase(raw, index) {
    if (!raw) return null;
    const cleaned = String(raw).trim().replace(/[).,;:]+$/, "");

    const words = cleaned.split(/\s+/);
    if (words.length > 1) {
      for (let n = words.length; n >= 1; n--) {
        const phrase = words.slice(0, n).join(" ");
        const gene = resolveGeneExact(phrase, index);
        if (gene) return { gene, phrase, words: n };
      }
      return null;
    }
    const gene = resolveGeneExact(cleaned, index);
    return gene ? { gene, phrase: cleaned, words: 1 } : null;
  }

  /**
   * How far into `raw` the first `n` whitespace-separated words reach.
   *
   * Measured against the original text rather than the resolved phrase,
   * because the phrase is rejoined with single spaces and the page may not
   * have used single spaces. The mark has to land on the page's characters.
   */
  function wordPrefixLength(raw, n) {
    const re = /\S+/g;
    let m, end = 0, seen = 0;
    while ((m = re.exec(raw)) !== null) {
      end = m.index + m[0].length;
      if (++seen === n) break;
    }
    return end;
  }

  function resolveGene(raw, index) {
    const named = resolveGenePhrase(raw, index);
    return named ? named.gene : null;
  }

  function resolveGeneExact(cleaned, index) {
    const key = normaliseKey(cleaned);

    if (index.aliasMap.has(key)) return index.aliasMap.get(key);
    if (index.geneMap.has(key)) return index.geneMap.get(key);

    // "TDP-43" vs "TDP43" vs "TDP 43"
    const collapsed = key.replace(/[-\s_]/g, "");
    if (index.aliasCollapsed.has(collapsed)) return index.aliasCollapsed.get(collapsed);
    if (index.geneCollapsed.has(collapsed)) return index.geneCollapsed.get(collapsed);

    return null;
  }

  /**
   * The applications a record actually holds a verdict in.
   *
   * `status.passed` / `status.failed` describe the applications *this page*
   * used, so on the grey branches — the page used it for IHC, or for an
   * assessed application with no result — they are both empty while the
   * record itself may hold published failures. Anything that wants to say
   * what we hold has to come here instead.
   */
  function recordVerdicts(record) {
    if (!record || !record.a) return { passed: [], failed: [] };
    return {
      passed: ASSESSED.filter((a) => record.a[a] === RECOMMENDED),
      failed: ASSESSED.filter((a) => record.a[a] === NOT_RECOMMENDED),
    };
  }

  /**
   * "recommended for WB", "not recommended for WB, IF", or both halves joined.
   * Empty when the record carries no verdict anywhere, which is the one case
   * where "no independent data" is the true thing to say.
   *
   * It lives here because the card and the screen-reader label both need it
   * and were both wrong in the same way: fixing the wording on one surface and
   * not the other is how a reader gets two different answers about one row.
   */
  function verdictClause(record) {
    const { passed, failed } = recordVerdicts(record);
    const parts = [];
    if (passed.length) parts.push(`recommended for ${passed.join(", ")}`);
    if (failed.length) parts.push(`not recommended for ${failed.join(", ")}`);
    return parts.join(", ");
  }

  /**
   * Turn a record plus the applications the paper used into a colour.
   *
   *   green  — recommended for the application used
   *   red    — tested and not recommended for the application used, or (when
   *            the paper does not say which application) not recommended in
   *            every application that was tested
   *   amber  — this antibody is not in the dataset, but its target is, so
   *            independently tested alternatives exist
   *   grey   — target not in the dataset; no signal either way
   */
  function resolveStatus(record, apps, target, index, source) {
    if (!record) {
      const gene = target && target.gene;
      if (gene && index.geneSet.has(gene)) {
        return { level: "amber", gene, applications: apps, reason: "alternatives" };
      }
      return { level: "grey", gene: gene || null, applications: apps, reason: gene ? "target-untested" : "target-unknown" };
    }

    const mentioned = (apps || []).filter((a) => ASSESSED.includes(a));
    const unassessed = (apps || []).filter((a) => !ASSESSED.includes(a));

    // HOW the applications were arrived at, which decides whether they are
    // allowed to narrow the verdict. `via` is one of:
    //
    //   "paper"            the citation record says this paper used the reagent
    //                      for these applications. A LOOKUP, not a reading.
    //   "paper-unrecorded" the record covers this paper and lists this reagent,
    //                      but records no application for it (23% of pairs).
    //   "proximity"        the old behaviour: inferred from where the words sit.
    //
    // The distinction is the whole point of the citation layer and must never be
    // flattened. Proximity is measured at IF 20% correct and IP 0 of 3, so it may
    // not narrow anything; a lookup is somebody else's record of what the paper
    // did, so it may.
    const via = (source && source.via) || "proximity";
    const lookedUp = via === "paper" && mentioned.length > 0;

    // The record's verdict for all four, always — never narrowed to whatever the
    // page happened to say. A re-run of the 100-paper corpus is what settled this:
    // of 76 papers that used western blot the extension named it for 39, of 25
    // that used IHC it named it for NONE, and of 15 immunofluorescence namings
    // only 3 were right. Seventeen of 54 papers were told an application the
    // paper never used. Prose-proximity inference is a western-blot detector, and
    // a verdict filtered through it is wrong roughly a third of the time it
    // speaks at all.
    //
    // So the reagent's whole record is what the card shows and what the colour
    // comes from; a detected application is a HINT that marks its row.
    const passed = ASSESSED.filter((a) => record.a[a] === RECOMMENDED);
    const failed = ASSESSED.filter((a) => record.a[a] === NOT_RECOMMENDED);
    const untested = ASSESSED.filter((a) => (record.a[a] ?? NOT_TESTED) === NOT_TESTED);
    // Which of the page's namings to caution about — see RELIABLE_CUES. A
    // looked-up application is not a doubted reading of the page, so it carries
    // no caution: repeating the proximity hedge over it would teach a reader to
    // discount the one signal here that is actually reliable.
    const uncertain = lookedUp
      ? []
      : uncertainApplications([].concat(mentioned, unassessed));
    const base = {
      record, mentioned, unassessed, uncertain, passed, failed, untested,
      via,
      paperStatus: (source && source.paperStatus) || "unavailable",
      // CiteAb's own token was one its glossary cannot resolve to a single one of
      // our four — bare "IF" does not say whether the preparation was cultured
      // cells or tissue, and OGA's IF verdict is cultured cells ONLY. So this is
      // reported and never narrowed on.
      applicationAmbiguous: Boolean(source && source.ambiguous),
      // Applications the paper used it for that OGA does not test at all, NAMED
      // (IHC above all, then ChIP, ELISA, PLA). "Used for IHC" is a sentence a
      // reader can act on; "used for something we don't test" is not.
      untestedApplications: (source && source.untestedApplications) || [],
    };

    // A LOOKED-UP application narrows the verdict to that application. This is
    // the one behaviour the citation layer exists to add, and it is safe for
    // exactly the reason the proximity version was not: nobody is reading the
    // page and guessing. A paper recorded as using this reagent for IF, on a
    // reagent that failed IF, is a red mark for a reason that can be stated.
    //
    // Three guards, all of which fall back to the whole-record verdict:
    //   - only when the record actually holds a verdict for one of them, so a
    //     looked-up application we never tested cannot manufacture a colour;
    //   - never on an ambiguous token (see `applicationAmbiguous` above);
    //   - never on proximity, which is where the measured harm was.
    // The card draws all four applications as tabs regardless, so narrowing the
    // colour hides nothing — it only decides which one the reader sees first.
    if (lookedUp && !base.applicationAmbiguous) {
      const here = mentioned.filter((a) => passed.includes(a) || failed.includes(a));
      if (here.length) {
        const failedHere = here.filter((a) => failed.includes(a));
        const passedHere = here.filter((a) => passed.includes(a));
        return {
          ...base,
          level: failedHere.length && passedHere.length ? "mixed"
               : failedHere.length ? "red" : "green",
          reason: "paper-application",
          scopedTo: here,
        };
      }
    }

    // Two refusals survive, and only these two. Both are cases where the page
    // names what it did AND we hold nothing that speaks to it, so any colour
    // taken from another application would answer a question nobody asked.
    // Neither is an assertion — grey claims nothing — and neither hides anything
    // any more, because the card lists every application either way. The
    // measured harm was wrong assertions (IF named wrongly 12 times, IP 3); IHC
    // and FC were never once named wrongly, so nothing here rests on the half of
    // the detector that misfires.
    if (unassessed.length && !mentioned.length) {
      return { ...base, level: "grey", reason: "app-not-assessed" };
    }
    if (mentioned.length && mentioned.every((a) => !passed.includes(a) && !failed.includes(a))) {
      return { ...base, level: "grey", reason: "app-not-tested" };
    }

    if (!passed.length && !failed.length) {
      return { ...base, level: "grey", reason: "record-untested" };
    }
    // Worst held result wins, so a failure anywhere is never painted green, and
    // `mixed` covers every record whose applications disagree — 43.1% of the
    // shipped index.
    if (passed.length && failed.length) {
      return { ...base, level: "mixed", reason: mentioned.length ? "mentioned" : "no-application" };
    }
    if (passed.length) {
      return { ...base, level: "green", reason: mentioned.length ? "mentioned" : "no-application" };
    }
    return { ...base, level: "red", reason: mentioned.length ? "mentioned" : "no-application" };
  }

  /**
   * Find every antibody mention in a block of text.
   * Returns hits with absolute offsets so the caller can decorate the DOM.
   */
  /**
   * Is this hit worth showing at all?
   *
   * A reagent we hold no data on is not a finding. Marking every unknown RRID in
   * a Key Resources table grey buries the handful of marks that mean something —
   * one real page produced 25 marks, all of them grey. Grey is kept only where it
   * is genuinely informative: the antibody IS in the dataset, but the page used
   * it in an application nobody assessed.
   */
  function worthShowing(hit) {
    if (hit.record) return true;              // we have data: green/red/mixed, or grey-for-this-application
    return hit.status.level === "amber";      // no data on the antibody, but its target is characterised
  }

  function findMentions(text, index, blocks, paper) {
    const hits = [];
    const claimed = [];

    function overlaps(start, end) {
      return claimed.some((r) => start < r[1] && end > r[0]);
    }

    // RRIDs first — they are unambiguous, so they win any overlap.
    RRID_RE.lastIndex = 0;
    let m;
    while ((m = RRID_RE.exec(text)) !== null) {
      const rrid = normaliseRrid(m[1] || m[2]);
      const start = m.index;
      const end = start + m[0].length;
      const record = index.antibodies[rrid] || null;
      if (!record && !hasContext(text, start, end)) continue;
      const hit = buildHit({ text, start, end, matched: m[0], rrid, record, index, blocks, paper, via: "rrid" });
      if (!worthShowing(hit)) continue;
      claimed.push([start, end]);
      hits.push(hit);
    }

    // Then catalogue numbers.
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(text)) !== null) {
      const token = m[0].replace(/[.,;:)]+$/, "");
      if (!token) continue;
      const start = m.index;
      const end = start + token.length;
      if (overlaps(start, end)) continue;

      // Look the identifier up without its typesetting, but judge it on the
      // text as it appears. The two guards below pull in opposite directions
      // and each needs the form that makes it strict:
      //   looksLikeMeasurement reads what the *page* puts either side, so it
      //   works in original offsets;
      //   isWeakIdentifier has to see the identifier the way the index does,
      //   or `89 718` walks past the context gate that `89718` must clear —
      //   the space alone makes it stop looking like a bare number.
      const key = normaliseIdentifier(token);
      // Exact first, always: a stored number that really contains the character
      // still wins on its own form before the collapsed key is consulted.
      const collapsed = collapseIdentifier(token);
      const rrid = index.catalogue[normaliseKey(key)]
        || (collapsed && index.catalogueCollapsed
            ? index.catalogueCollapsed.get(collapsed) : undefined);
      if (!rrid) continue;
      if (looksLikeMeasurement(text, start, end)) continue;
      if (isWeakIdentifier(key) && !hasContext(text, start, end)) continue;

      const record = index.antibodies[rrid] || null;
      const hit = buildHit({ text, start, end, matched: token, rrid, record, index, blocks, paper, via: "catalogue" });
      if (!worthShowing(hit)) continue;
      claimed.push([start, end]);
      hits.push(hit);
    }

    // Then clone IDs. A monoclonal is very often cited by its clone and nothing
    // else -- "anti-CaMKII (pan) (D11A10) antibody" gives no catalogue number at
    // all -- and the clone identifies the product as precisely as the number
    // does. Always gated on antibody vocabulary nearby: clones like "4F9" or
    // "1M10" are short enough to collide with ordinary text.
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(text)) !== null) {
      const token = m[0].replace(/[.,;:)]+$/, "");
      if (!token) continue;
      const start = m.index;
      const end = start + token.length;
      if (overlaps(start, end)) continue;

      const key = normaliseIdentifier(token);
      const rrid = index.clones[normaliseKey(key)];
      if (!rrid) continue;
      if (looksLikeMeasurement(text, start, end)) continue;
      if (!hasContext(text, start, end)) continue;
      if (!cloneIsDistinctive(key) && !/\bclone\b/i.test(text.slice(Math.max(0, start - 60), end + 20))) continue;

      const record = index.antibodies[rrid] || null;
      const hit = buildHit({ text, start, end, matched: token, rrid, record, index, blocks, paper, via: "clone" });
      if (!worthShowing(hit)) continue;
      claimed.push([start, end]);
      hits.push(hit);
    }

    // Then catalogue numbers we do NOT hold, but which the paper has labelled
    // with a target we do. Amber at most: we are saying "not this one, but this
    // target has tested antibodies", never anything about the reagent itself.
    GENE_THEN_CAT_RE.lastIndex = 0;
    while ((m = GENE_THEN_CAT_RE.exec(text)) !== null) {
      const token = m[2];
      const key = normaliseIdentifier(token);
      if (YEAR_RE.test(key)) continue;

      // Already carries a real verdict — the catalogue pass owns it. Asked the
      // same two ways the catalogue pass asks, or a number it now resolves by its
      // collapsed key would be marked amber here as well as green there.
      const collapsedKey = collapseIdentifier(token);
      if (index.catalogue[normaliseKey(key)]
          || (collapsedKey && index.catalogueCollapsed
              && index.catalogueCollapsed.has(collapsedKey))) continue;

      const start = m.index + m[0].lastIndexOf(token);
      const end = start + token.length;
      if (overlaps(start, end)) continue;

      const named = resolveGenePhrase(m[1], index);
      if (!named) continue;
      // This mark is inferred rather than looked up, so it needs the word
      // "antibody" and not merely "cat no." -- a recombinant enzyme is bought
      // by catalogue number from a named supplier and reads identically:
      // "MMP-7 (cat no. M4565) were purchased from Sigma-Aldrich" is not an
      // antibody at all, and amber there is a claim about the wrong reagent.
      if (!hasAntibodyContext(text, start, end)) continue;

      const hit = buildHit({
        text, start, end, matched: token, rrid: null, record: null, index, blocks,
        via: "gene-adjacent", forcedTarget: { gene: named.gene, raw: named.phrase },
      });
      if (!worthShowing(hit)) continue;
      claimed.push([start, end]);
      hits.push(hit);
    }

    // Finally, antibodies named only by target ("anti-TDP-43 antibody") with no
    // catalogue number. These can only ever be amber or grey.
    for (const re of TARGET_PATTERNS) {
      re.lastIndex = 0;
      while ((m = re.exec(text)) !== null) {
        const named = resolveGenePhrase(m[1], index);
        if (!named) continue;
        const gene = named.gene;

        // Shrink the mark to the words that actually resolved.
        //
        // findMentions used to mark m[0] whole, however much of the capture
        // resolveGene had consumed — so "antibodies against PINK1 and Parkin"
        // resolved PINK1 and then underlined all of it, and with the sentence
        // bug above underlined past the full stop too. Keep whatever the
        // pattern matched *after* the capture, which is " antibody" in the
        // third pattern and nothing in the other two.
        const start = m.index;
        const groupStart = m.indices && m.indices[1]
          ? m.indices[1][0]
          : start + m[0].indexOf(m[1]);
        const trailing = start + m[0].length - (groupStart + m[1].length);
        const end = groupStart + wordPrefixLength(m[1], named.words) + trailing;

        if (overlaps(start, end)) continue;
        if (!hasContext(text, start, end)) continue;
        // "anti-TDP-43 antibody (Abcam ab109535)" is one reagent named twice: the
        // catalogue number immediately follows the phrase and already carries
        // the verdict, so marking the name too would put amber beside green for
        // the same thing. Look forward only, and only far enough to cover the
        // trailing parenthetical — "an additional anti-TDP-43 antibody (Vendor,
        // cat# X)" further down the paragraph is a genuinely different reagent
        // and must keep its amber.
        // The gene-adjacent pass above marks that trailing catalogue number too,
        // so this now has to dedup against an ambered token as well as a
        // record-backed one — otherwise "anti-TDP-43 (Abcam, cat# <unknown>)"
        // draws two amber marks for one reagent.
        if (hits.some((h) => {
          const g = h.record ? h.record.g : (h.target && h.target.gene);
          return g === gene && h.start > start && h.start - start < 60;
        })) continue;
        const hit = buildHit({
          text, start, end, matched: m[0], rrid: null, record: null, index, blocks,
          via: "target", forcedTarget: { gene, raw: named.phrase },
        });
        if (!worthShowing(hit)) continue;
        claimed.push([start, end]);
        hits.push(hit);
      }
    }

    hits.sort((a, b) => a.start - b.start);
    return hits;
  }

  /**
   * How the target was attributed — a different question from `via`, which
   * says how the *identifier* was found, and the only one that matters for
   * amber:
   *
   *   record   the dataset row says so. Certain.
   *   stated   the page put the name against this identifier — "PINK1 (#A2164)",
   *            or the "anti-PINK1 antibody" phrase that IS the mark.
   *   nearby   the closest resolvable target name in the same block. This is
   *            the one that can be wrong: in a Key Resources table it is the
   *            row above or below.
   */
  function buildHit({ text, start, end, matched, rrid, record, index, blocks, paper, via, forcedTarget }) {
    // What the citation record says THIS paper used THIS reagent for, if it
    // covers the paper and lists the reagent. `rrid` is the only key it can be
    // looked up by, so a hit resolved by target name (which carries no rrid)
    // stays on proximity — correctly, since we do not know which reagent it is.
    const cited = (paper && paper.status === "covered" && rrid && paper.reagents)
      ? paper.reagents[rrid] : null;
    const source = {
      paperStatus: (paper && paper.status) || "unavailable",
      ambiguous: Boolean(cited && cited.ambiguous),
      untestedApplications: (cited && cited.untestedApplications) || [],
      // "covered, lists this reagent, records no application" is its own state
      // and NOT the same as "not covered": the first says the record has nothing
      // to add about the use, the second says the record has never seen the
      // paper. Both fall back to proximity; they say different things on the card.
      via: cited
        ? (cited.applications.length ? "paper" : "paper-unrecorded")
        : "proximity",
    };
    const applications = (cited && cited.applications.length)
      ? cited.applications
      : inferApplications(text, start, blocks);

    let target = forcedTarget || null;
    let targetVia = target ? "stated" : null;
    if (!target && record) {
      target = { gene: record.g, raw: record.g };
      targetVia = "record";
    } else if (!target) {
      target = inferTarget(text, start, index);
      targetVia = target ? "nearby" : null;
    }

    const status = resolveStatus(record, applications, target, index, source);
    return { start, end, matched, rrid, record, via, applications, target, targetVia,
             status, applicationsVia: source.via };
  }

  /** Prepare the raw index JSON for fast lookup. */
  function prepareIndex(raw) {
    const geneMap = new Map();
    const geneCollapsed = new Map();
    const geneSet = new Set(raw.genes || []);
    for (const g of raw.genes || []) {
      geneMap.set(normaliseKey(g), g);
      geneCollapsed.set(normaliseKey(g).replace(/[-\s_]/g, ""), g);
    }

    const aliasMap = new Map();
    const aliasCollapsed = new Map();
    for (const [alias, gene] of Object.entries(raw.aliases || {})) {
      aliasMap.set(normaliseKey(alias), gene);
      aliasCollapsed.set(normaliseKey(alias).replace(/[-\s_]/g, ""), gene);
    }

    // Catalogue numbers with all punctuation gone, derived from the shipped index
    // rather than added to it — no index-format change, and the built zip stays
    // exactly what tools/build_extension_index.py produces. First key wins, so a
    // collision between two stored numbers can never displace an exact match:
    // this map is only ever consulted after the exact lookup has failed.
    const catalogueCollapsed = new Map();
    for (const key of Object.keys(raw.catalogue || {})) {
      const collapsed = collapseIdentifier(key);
      if (collapsed && !catalogueCollapsed.has(collapsed)) {
        catalogueCollapsed.set(collapsed, raw.catalogue[key]);
      }
    }

    return {
      schema: raw.schema,
      generated: raw.generated,
      source: raw.source,
      counts: raw.counts || {},
      antibodies: raw.antibodies || {},
      catalogue: raw.catalogue || {},
      clones: raw.clones || {},
      catalogueCollapsed,
      geneSet, geneMap, geneCollapsed, aliasMap, aliasCollapsed,
    };
  }

  root.OGAMatcher = {
    ASSESSED,
    NOT_TESTED, NOT_RECOMMENDED, RECOMMENDED,
    prepareIndex,
    findMentions,
    uncertainApplications,
    RELIABLE_CUES,
    inferApplications,
    inferTarget,
    resolveGene,
    resolveStatus,
    recordVerdicts,
    verdictClause,
    normaliseIdentifier,
    collapseIdentifier,
    normaliseRrid,
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
