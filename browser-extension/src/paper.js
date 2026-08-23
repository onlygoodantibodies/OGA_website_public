/**
 * Which paper is this, and does the citation record know it?
 *
 * The rest of the extension answers "which antibody is this". This file answers
 * "which PAPER is this", which is a separate question and the one that unlocks
 * the application. `matcher.js` can only guess what a paper used an antibody FOR
 * — and that guess is measured bad (WB 87% correct, IF 20%, IP 0 of 3, IHC and FC
 * never named), which is why `RELIABLE_CUES` is `["WB"]`. If we can work out
 * WHICH paper is on screen, CiteAb has already recorded what it used each reagent
 * for, and the guess is replaced by a lookup.
 *
 * NOTHING HERE LEAVES THE BROWSER. The lookup table ships with the extension and
 * is held in the service worker; the page's title never crosses a network. That
 * is not incidental — telling a server which paper somebody is reading is a much
 * bigger disclosure than anything this extension has ever made, and the whole
 * design exists to avoid needing to.
 *
 * THREE KEYS, BECAUSE NO ONE KEY IS EVERYWHERE
 * --------------------------------------------
 *   pmid   exact, and free on pubmed.ncbi.nlm.nih.gov / europepmc.org, where it
 *          is in the URL. Tried first: an exact identifier beats a hashed string.
 *   doi    the precise key on an ordinary publisher page. CiteAb stores no DOI at
 *          all, so `by_doi` is empty until a resolution pass against PubMed fills
 *          it. Tried second so that it takes over automatically the day it lands.
 *   title  the fallback that works today on a publisher page, hashed so the table
 *          costs a key rather than a sentence per paper.
 *
 * A TITLE MATCH IS CONFIRMED BY THE YEAR, A PMID MATCH IS NOT
 * -----------------------------------------------------------
 * A PubMed id IS the paper. A title is a string two papers can share, so a title
 * hit is checked against the publication year before it is believed, allowing one
 * year of slack because online-first and print dates routinely differ by one and
 * rejecting a real match over that would be its own bug.
 *
 * FAILING IS SAFE; BEING WRONG IS NOT
 * -----------------------------------
 * Every failure mode here ends in `not-covered`, and `not-covered` puts the
 * extension back to exactly the behaviour it has today. So a title we cannot
 * normalise the same way the builder did, a page with no metadata, a paper CiteAb
 * has never seen — all cost the same thing, which is the improvement and nothing
 * else. What must never happen is a WRONG paper matching, because that attaches
 * another paper's applications to this one and there is no screen on which that
 * looks like an error. Every rule in this file is chosen in that direction.
 */
(function (root) {
  "use strict";

  /* ------------------------------------------------------- the shared string */

  // Greek letter to its English name. Mirrors core/citations.py::_GREEK, and the
  // two are pinned against core/data/title_normalisation_vectors.json.
  //
  // This exists because 9.1% of the titles in the citation record carry a Greek
  // letter (β 1,343, α 947, κ 505 ...) and a normaliser that only strips
  // punctuation DELETES them -- so "β-catenin" becomes "catenin" while a
  // publisher spelling it out gives "beta catenin", and the paper is never
  // recognised. The whole alphabet is here rather than the letters that happen to
  // appear today, because a table that covers this year's data and not next
  // year's fails in the one way nobody looks for.
  const GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta",
    "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu",
    "ν": "nu", "ξ": "xi", "ο": "omicron", "π": "pi",
    "ρ": "rho", "σ": "sigma", "ς": "sigma", "τ": "tau",
    "υ": "upsilon", "φ": "phi", "χ": "chi", "ψ": "psi",
    "ω": "omega",
  };
  const GREEK_RE = new RegExp(Object.keys(GREEK).join("|"), "g");

  /**
   * A paper title reduced to the form both sides hash.
   *
   * MIRRORS core/citations.py::normalise_title. The order is the whole of it:
   * case-fold FIRST so an uppercase Delta and a lowercase delta reach the table
   * as one entry, transliterate SECOND while the letters still exist, strip
   * LAST -- a stripper that runs first has already deleted what the
   * transliteration was for.
   */
  function normaliseTitle(title) {
    let text = (title || "").normalize("NFKD").toLowerCase();
    // Substituted in place, NOT padded with spaces -- see the note in
    // core/citations.py::normalise_title. `NF-kappaB` is how a publisher spells
    // it, so padding would give `nf kappa b` against their `nf kappab`.
    text = text.replace(GREEK_RE, (ch) => GREEK[ch]);
    return text.replace(/[^a-z0-9]+/g, " ").trim().replace(/\s+/g, " ");
  }

  /**
   * FNV-1a (32-bit) over the normalised title, as eight hex digits.
   *
   * MIRRORS core/citations.py::title_key. Chosen over a cryptographic digest
   * because it is arithmetic that gives byte-identical answers in both languages
   * SYNCHRONOUSLY -- crypto.subtle.digest is async, and threading a promise
   * through the matcher to compute a lookup key would be complexity bought for
   * nothing. Nothing here is being protected from an adversary.
   *
   * Math.imul is doing the real work: the FNV prime multiply overflows 32 bits,
   * and `*` on a JS number silently loses the low bits to float precision, which
   * would diverge from Python for a fraction of titles -- the worst kind of bug
   * this could have, because it would look like patchy CiteAb coverage.
   */
  function titleKey(title) {
    const text = normaliseTitle(title);
    let key = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) {
      // The normalised form is ASCII by construction, so a char code IS its
      // UTF-8 byte and this agrees with Python's .encode("utf-8").
      key = Math.imul(key ^ text.charCodeAt(i), 0x01000193) >>> 0;
    }
    return key.toString(16).padStart(8, "0");
  }

  /* ------------------------------------------------------ reading the page */

  function metaContent(doc, names) {
    for (const name of names) {
      const el = doc.querySelector(`meta[name="${name}" i]`);
      const value = el && el.content && el.content.trim();
      if (value) return value;
    }
    return null;
  }

  /** Strip the ways a DOI gets printed, and lowercase: DOIs are case-insensitive. */
  function normaliseDoi(text) {
    if (!text) return null;
    return text.trim()
      .replace(/^(?:doi:|https?:\/\/(?:dx\.)?doi\.org\/)/i, "")
      .toLowerCase() || null;
  }

  /**
   * The keys this page can be looked up by.
   *
   * Computed HERE, in the page, because this file is the single copy of the
   * rules -- the title normaliser and the FNV-1a key have to match the builder
   * exactly, and the worker deliberately holds no copy of them. What goes to the
   * worker is three already-resolved keys, which it can only do dictionary
   * lookups with.
   *
   * `document.title` is deliberately NOT a title source. Publishers append the
   * journal, the section, sometimes the whole site name, so it normalises to
   * something the builder never saw -- and a title that fails to match is
   * indistinguishable from a paper the record has not got. `citation_title` is
   * the declared one and the only one worth hashing.
   */
  function pageKeys(doc, url) {
    const href = url || (doc.location && doc.location.href) || "";
    let pmid = metaContent(doc, ["citation_pmid", "citation_pubmed_id"]);
    if (!pmid) {
      // pubmed.ncbi.nlm.nih.gov/12345678/ and europepmc.org/article/MED/12345678
      const m = href.match(/pubmed\.ncbi\.nlm\.nih\.gov\/(\d{4,9})/i)
             || href.match(/europepmc\.org\/(?:article|abstract)\/MED\/(\d{4,9})/i);
      if (m) pmid = m[1];
    }
    const date = metaContent(doc, [
      "citation_publication_date", "citation_date", "citation_year",
      "citation_online_date", "dc.date",
    ]);
    const yearMatch = date && date.match(/\b(19|20)\d{2}\b/);
    const title = metaContent(doc, ["citation_title"]);
    return {
      pmid: pmid ? (pmid.replace(/\D/g, "") || null) : null,
      doi: normaliseDoi(metaContent(doc, ["citation_doi", "DC.Identifier", "dc.identifier"])),
      titleKey: title ? titleKey(title) : null,
      year: yearMatch ? Number(yearMatch[0]) : null,
    };
  }

  /* ----------------------------------------------------------- the answer */

  // Online-first and print years routinely differ by one, and rejecting a real
  // match over that would be a bug of our own making. Two years apart is a
  // different paper.
  const YEAR_SLACK = 1;

  const FLAG = { WB: 1, IP: 2, IF: 4, FC: 8, AMBIGUOUS: 16, UNTESTED_APP: 32 };
  const APPS = ["WB", "IP", "IF", "FC"];

  /** One packed record -> {RRID: {applications, ambiguous, untestedApplications}}. */
  function unpack(packed, rrids, tokens) {
    const out = {};
    for (const field of (packed || "").split(",")) {
      if (!field) continue;
      const [head, ...tokenParts] = field.split(".");
      const [rrIndex, flagHex] = head.split(":");
      const rrid = (rrids || [])[parseInt(rrIndex, 16)];
      if (!rrid) continue;
      const flags = parseInt(flagHex, 16) || 0;
      out[rrid] = {
        applications: APPS.filter((a) => flags & FLAG[a]),
        // CiteAb's token maps to one of ours but does not say which preparation.
        // Bare "IF" cannot tell cultured cells from tissue, and OGA's IF verdict
        // is cultured cells ONLY -- so this may not narrow a verdict, only be
        // reported.
        ambiguous: Boolean(flags & FLAG.AMBIGUOUS),
        // Used for something OGA does not test at all. NAMED, because "used for
        // IHC" is a sentence a reader can act on and "used for something we
        // don't test" is not.
        untestedApplications: tokenParts
          .map((t) => (tokens || [])[parseInt(t, 16)])
          .filter(Boolean),
      };
    }
    return out;
  }

  /**
   * Turn the worker's answer into what the matcher uses, confirming it first.
   *
   * The confirmation is why this is not just `unpack`. A PubMed id and a DOI ARE
   * the paper; a title is a string two papers can share, so a title hit is
   * checked against the publication year before it is believed. Refusing is the
   * only safe answer when they disagree -- another paper's applications attached
   * to this one is invisible to the reader, where a refusal costs nothing but the
   * improvement.
   */
  function confirmPaper(answer, keys) {
    if (!answer || answer.status !== "covered") {
      return answer || { status: "unavailable", reason: "no-answer" };
    }
    if (answer.matchedOn === "title" && answer.year && keys && keys.year
        && Math.abs(answer.year - keys.year) > YEAR_SLACK) {
      return { status: "not-covered", reason: "year-disagreed" };
    }
    return {
      status: "covered",
      matchedOn: answer.matchedOn,
      year: answer.year || null,
      source: answer.source || null,
      reagents: unpack(answer.packed, answer.rrids, answer.tokens),
    };
  }

  root.OGAPaper = {
    normaliseTitle, titleKey, normaliseDoi, pageKeys, confirmPaper, unpack,
    APPS, FLAG, YEAR_SLACK,
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
