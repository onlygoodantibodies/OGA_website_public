"""Europe PMC annotations, built from the two files the browser extension reads.

Runnable two ways, and the second is the point:

    python manage.py build_europepmc_annotations --check --email you@example.org
    python3 core/europepmc_annotations.py       --check --email you@example.org

**The second needs nothing but Python 3** — the same bargain as
``core/citations_doi.py``, and for the same reason: this reads two JSON files,
calls one public API, and writes files. The person who runs it is a scientist
with a laptop, or a GitHub Actions runner (``.github/workflows/
europepmc-annotations.yml``), and neither should need the Django stack.

WHAT AN ANNOTATION IS HERE
--------------------------
Europe PMC's annotations platform (https://europepmc.org/AnnotationsSubmission)
highlights a span of an article and links it to a resource. The browser
extension does the same thing on a publisher's page: it finds a catalogue
number, looks it up, and colours it with OGA's per-application characterisation
result. This module produces the same marks for Europe PMC's reader — one row
per article, one ``ann`` per printed identifier, one tag per ``ann`` naming the
antibody and its four rungs and linking to the gene page on
onlygoodantibodies.co.uk, which is public and needs no login (a ground rule of
the platform).

**It uses exactly the extension's inputs and never the database.** Which paper
used which reagent comes from the citation snapshot
(``core/data/citeab_papers_*.json``, read through ``core/citations.py``'s
encoding); what OGA found comes from the extension index
(``/extension/index.json`` or the copy under ``browser-extension/data/``). So an
annotation here and a mark in the extension agree by construction, and the
snapshot — not the page text — decides which reagents a paper used. The text is
only searched for the reagents CiteAb already attributed to that paper, which is
what lets this skip every proximity gate the extension needs when it is guessing
from a blind page.

TWO ROWS, BECAUSE THE FACT IS ABOUT THE PAPER AND THE TEXT ONLY SAYS WHERE
-----------------------------------------------------------------------
**Every paper gets a paper-level row** (``src: "MED"``, the PubMed id): one
sentence-based annotation on the title, with one tag per antibody saying the
paper used it, for what if CiteAb recorded that, and what OGA found. CiteAb's
attribution is a fact about the whole paper, and the title is the one sentence
every indexed article has — so this reaches the abstract-only majority, which is
where a reader deciding whether to fetch the paper is standing.

**An open-access paper gets a full-text row as well** (``src: "PMC"``, the PMC
id): a named-entity annotation on each printed catalogue number, RRID or clone,
quoting the article — ``exact`` is the identifier as typeset and
``prefix``/``postfix`` its neighbours — so the mark lands in Methods where the
reagent is. Catalogue numbers live in Methods, so this exists only where the
text can be read; the report counts the papers that got the title alone and
says why (no open-access full text, or open access but nothing printed).

Three assumptions were made without seeing the platform's specifications, and
the run's ``--check`` prints enough to judge each:

* ``position`` is ``<sentence>.<chunk>``, sentences numbered from 1 across the
  whole article in reading order and chunks numbered within the sentence. The
  submission page's example (``1.2`` on a title, ``2.1`` on the first abstract
  sentence) reads that way.
* Rows anchored in full text use ``src: "PMC"`` and the PMC id, since the
  section vocabulary the page lists for ``src=PMC`` is the one full text has.
* Preprints (``PPR`` ids, 3,006 of the snapshot's 29,207 papers) are left out
  and counted, because ``PPR`` is not on the page's list of sources. Ask.

Nothing here is a verdict on a product. The tag names OGA's rungs in the words
the site uses (``core/recommendations.py``) and the linked page carries the
caveats; an annotation is a pointer to characterisation data, which is all the
extension's mark ever was.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

# Importable as `core.europepmc_annotations` and runnable as
# `core/europepmc_annotations.py`, where sys.path[0] is `core/`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.citations import ARTEFACT, unpack  # noqa: E402

SEARCH_ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"

#: Where the extension index is served, and the copy in the repository that a
#: run with no network (or a test) can read instead.
INDEX_URL = "https://onlygoodantibodies.co.uk/extension/index.json"
BUNDLED_INDEX = Path(__file__).resolve().parent.parent / "browser-extension" / "data" / "index.json"

USER_AGENT = "OGA-europepmc-annotations/1.0"

#: Same pacing as the DOI pass: Europe PMC asks for considerate use.
BATCH = 50
PAGE_SIZE = 100
DELAY_SECONDS = 0.34

#: The platform's own limits and vocabularies, from the submission page.
MAX_ROWS_PER_FILE = 9999          # "every file must have less than 10000 rows"
SOURCES = frozenset({"MED", "PMC", "PAT", "AGR", "CBA", "HIR", "CTX", "ETH", "CIT"})
FULL_TEXT_SECTIONS = (
    "Title", "Abstract", "Introduction", "Methods", "Results", "Discussion",
    "Acknowledgments", "References", "Table", "Figure", "Case study",
    "Supplementary material", "Conclusion", "Abbreviations",
    "Competing Interests", "Article",
)
DEFAULT_SECTION = "Article"

#: Characters of the sentence kept either side of the span. The submission
#: page's examples carry about this much; it is context for a reader, not a
#: locator, and the sentence position plus the exact text do the locating.
CONTEXT_CHARS = 40

APPLICATIONS = ("WB", "IP", "IF", "FC")

# ---------------------------------------------------------------------------
# The identifier rules — MIRRORS of browser-extension/src/matcher.js.
#
# A catalogue number is typeset differently by every publisher, and both tools
# already carry the same rules for it (`matcher.js::normaliseIdentifier` and
# `mcp_servers/common/manuscript.py::normalise_identifier`). This is a third copy
# only because the module must run with a bare python3; it adds no rule of its
# own. Widen one and widen the others.
# ---------------------------------------------------------------------------

_DASH_CLASS = "\\-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DASH_RE = re.compile(f"[{_DASH_CLASS}]")
_ZERO_WIDTH = "\u200b\u200c\u200d\ufeff\u00ad"
_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH}]")
_GROUP_SEP = ",\u0020\u00a0\u2009\u200a\u202f"
_GROUP_SEP_LOOKAROUND = f"(?<=\\d)[{_GROUP_SEP}](?=\\d{{3}}(?!\\d))"
_GROUP_SEP_RE = re.compile(_GROUP_SEP_LOOKAROUND)
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

TOKEN_RE = re.compile(
    "[A-Za-z0-9]"
    f"(?:[A-Za-z0-9{_DASH_CLASS}_.]|[{_ZERO_WIDTH}]|{_GROUP_SEP_LOOKAROUND}){{2,}}"
)
RRID_RE = re.compile(
    f"\\bRRID\\s*:?\\s*(AB[_{_DASH_CLASS}]\\d{{3,}})\\b"
    f"|\\b(AB[_{_DASH_CLASS}]\\d{{4,}})\\b",
    re.I,
)

#: Below this many alphanumerics a collapsed key is not compared at all —
#: catalogue numbers are not unique across suppliers. `matcher.js::MIN_COLLAPSED`.
MIN_COLLAPSED = 5


def normalise_identifier(token):
    text = _ZERO_WIDTH_RE.sub("", str(token or ""))
    text = _GROUP_SEP_RE.sub("", text)
    return _DASH_RE.sub("-", text)


def identifier_key(token):
    """The index key for an identifier as a page happened to typeset it."""
    return normalise_identifier(token).strip().lower()


def collapse_identifier(token):
    """Alphanumerics only, or ``""`` when too short to be safe."""
    collapsed = _NON_ALNUM.sub("", str(token or ""))
    return collapsed.lower() if len(collapsed) >= MIN_COLLAPSED else ""


def rrid_key(token):
    text = normalise_identifier(token).strip().upper()
    text = re.sub(r"^RRID:?\s*", "", text)
    return text.replace("-", "_")


def is_weak_identifier(identifier):
    """Too short to stand on its own: all digits, or under ``MIN_COLLAPSED``
    alphanumerics. Cell Signaling's ``#3872`` is the common case. Mirrors
    ``matcher.js::isWeakIdentifier``: such a token is a catalogue number only
    where the sentence says so, and never where it is a dilution or a unit."""
    text = str(identifier or "")
    return text.isdigit() or len(_NON_ALNUM.sub("", text)) < MIN_COLLAPSED


#: Words that make a short number a reagent rather than a number: the sentence
#: names an antibody, a catalogue, a clone, an RRID or the supplier.
_ANTIBODY_CONTEXT_RE = re.compile(
    r"antibod|\banti[-\s]|\bcat(?:alog(?:ue)?)?\b|#|\bclone\b|\bRRID\b|"
    r"\bmAb\b|\bpAb\b|Cell Signaling|\bCST\b|Santa Cruz|Abcam|Sigma|Millipore|"
    r"Thermo|Proteintech|Bio-?Techne|R&D Systems|Novus|BioLegend|Invitrogen",
    re.I)
#: A dilution's denominator or a number carrying a unit is a measurement.
_DILUTION_BEFORE_RE = re.compile(r"1\s*:\s*$")
_UNIT_AFTER_RE = re.compile(
    r"^\s*(?:ng|µg|ug|mg|kg|g|mL|ml|µL|uL|ul|L|nM|µM|mM|nm|mm|cm|µm|um|%|min|hr?s?|"
    r"sec|s|°\s*C|kDa|bp|kb|rpm|U|IU|mmol|mol|M|x\s*g|×\s*g|days?|weeks?)\b")


def looks_like_measurement(sentence, start, end):
    """``1:3872`` or ``3872 rpm``: a number that happens to equal a catalogue
    number and is plainly not one. Mirrors ``matcher.js::looksLikeMeasurement``."""
    return bool(_DILUTION_BEFORE_RE.search(sentence[max(0, start - 6):start])
                or _UNIT_AFTER_RE.match(sentence[end:end + 12]))


def is_distinctive_clone(clone):
    """A clone id worth matching on. The live data holds clones called ``5``
    and ``200``; a bare short number would match every volume in Methods."""
    text = (clone or "").strip()
    if len(text) < 4:
        return False
    if text.isdigit():
        return len(text) >= 6
    return True


# ---------------------------------------------------------------------------
# The rungs — a mirror of how the extension's card reads the index's codes
# (`card.js::CODE_LABEL` / `isLimited`), which is how `core/recommendations.py::
# support()` is published to it. `core/tests_europepmc_annotations.py` pins
# TEMPERING against `QUALIFIER_CODES` so this copy cannot drift.
# ---------------------------------------------------------------------------

CODE_WORDS = {0: "not tested", 1: "not supportive", 2: "supportive"}
LIMITED_WORDS = "limited support"
#: Qualifier codes that hold a negative back to the middle rung.
TEMPERING = frozenset({"ns", "sd", "se", "si"})


def rung(code, qualifier_code=None):
    if code == 1 and qualifier_code in TEMPERING:
        return LIMITED_WORDS
    return CODE_WORDS.get(code, "")


# ---------------------------------------------------------------------------
# Reagents: what the snapshot says a paper used, joined to what the index knows.
# ---------------------------------------------------------------------------

def reagents_for(paper_record, index):
    """``[reagent, ...]`` for one paper — only the RRIDs the index holds.

    ``paper_record`` is ``citations.unpack``'s ``{rrid: {applications,
    ambiguous, untested_terms}}``. An RRID the index lacks has no public record
    to point at, so it is left out; the caller counts those papers.
    """
    antibodies = index.get("antibodies") or {}
    out = []
    for rrid, use in sorted(paper_record.items()):
        record = antibodies.get(rrid)
        if not record:
            continue
        out.append({
            "rrid": rrid,
            "catalogue": (record.get("n") or "").strip(),
            "gene": record.get("g") or "",
            "supplier": record.get("s") or "",
            "clone": (record.get("cl") or "").strip(),
            "codes": record.get("a") or {},
            "qualifiers": record.get("q") or {},
            "discontinued": bool(record.get("d")),
            "used_for": list(use.get("applications") or []),
            "untested_terms": list(use.get("untested_terms") or []),
            "ambiguous": bool(use.get("ambiguous")),
        })
    return out


def lookup_keys(reagents):
    """``{key: (kind, reagent)}`` — every printed form that resolves to one of
    this paper's reagents. Keys two reagents share are dropped: a span that
    could be either is a question, not a coin toss."""
    keys, clashes = {}, set()

    def put(key, kind, reagent):
        # A key whose value half is empty would match every token that also
        # normalises to nothing -- which, for the collapsed form, is every word
        # under MIN_COLLAPSED characters. That put "and" and "the" under a
        # Cell Signaling antibody 2,500 times in one paper.
        if not key or not key[-1]:
            return
        if key in keys and keys[key][1] is not reagent:
            clashes.add(key)
        keys.setdefault(key, (kind, reagent))

    for reagent in reagents:
        put(rrid_key(reagent["rrid"]), "rrid", reagent)
        if reagent["catalogue"]:
            put(("id", identifier_key(reagent["catalogue"])), "catalogue", reagent)
            put(("collapsed", collapse_identifier(reagent["catalogue"])), "catalogue", reagent)
        if is_distinctive_clone(reagent["clone"]):
            put(("id", identifier_key(reagent["clone"])), "clone", reagent)
    for key in clashes:
        keys.pop(key, None)
    return keys


def use_clause(reagent):
    """What CiteAb records this paper used the antibody for, or that it does
    not say. **A looked-up application may narrow a verdict and an ambiguous
    one may not** — the rule the citation layer exists for — so an ambiguous
    attribution names the use and not the application."""
    if reagent["ambiguous"] or not (reagent["used_for"] or reagent["untested_terms"]):
        return "used in this paper (application not recorded by CiteAb)"
    clause = "used in this paper"
    if reagent["used_for"]:
        clause += " for " + ", ".join(reagent["used_for"])
    if reagent["untested_terms"]:
        clause += (" and for " if reagent["used_for"] else " for ") + \
            ", ".join(reagent["untested_terms"]) + " (which OGA does not test)"
    return clause


def tag_for(reagent, base_url):
    """One tag: the antibody, its four rungs, and the page that carries the
    caveats. The URI is the gene page focused on this antibody (``?ab=``), which
    is where the home page search sends a reader who types the same number."""
    parts = [f"{reagent['gene']} antibody {reagent['catalogue'] or reagent['rrid']}"]
    if reagent["supplier"]:
        parts[0] += f" ({reagent['supplier']})"
    parts.append(use_clause(reagent))
    rungs = "; ".join(
        f"{app} {rung(reagent['codes'].get(app, 0), reagent['qualifiers'].get(app))}"
        for app in APPLICATIONS)
    parts.append("OGA knockout-controlled characterisation: " + rungs)
    if reagent["discontinued"]:
        parts.append("supplier lists it as discontinued")
    focus = reagent["catalogue"] or reagent["rrid"]
    uri = f"{base_url.rstrip('/')}/antibodies/{quote(reagent['gene'], safe='')}/?{urlencode({'ab': focus})}"
    return {"name": " · ".join(parts), "uri": uri}


# ---------------------------------------------------------------------------
# The article: JATS full text -> (section, text) in reading order -> sentences.
# ---------------------------------------------------------------------------

_SECTION_CUES = (
    ("supplement", "Supplementary material"),
    ("abbreviation", "Abbreviations"),
    ("competing", "Competing Interests"),
    ("conflict", "Competing Interests"),
    ("acknowledg", "Acknowledgments"),
    ("method", "Methods"),
    ("material", "Methods"),
    ("experimental procedure", "Methods"),
    ("result", "Results"),
    ("discussion", "Discussion"),
    ("conclusion", "Conclusion"),
    ("introduction", "Introduction"),
    ("background", "Introduction"),
    ("case", "Case study"),
)


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text_of(element):
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def section_name(sec_type, title):
    """Europe PMC's section word for a JATS ``<sec>``, from its ``sec-type`` and
    then its title; ``Article`` when neither says. *Results and discussion* is
    Results, because the first cue wins and the list is ordered for it."""
    for source in (sec_type or "", title or ""):
        lowered = source.lower()
        for cue, name in _SECTION_CUES:
            if cue in lowered:
                return name
    return DEFAULT_SECTION


def _walk(element, section, out):
    """Depth-first over a body, emitting ``(section, text)`` per paragraph-like
    block. Tables and figures are their own sections whatever they sit in;
    the reference list is skipped, since a catalogue number there is somebody
    else's paper."""
    name = _local(element.tag)
    if name == "ref-list":
        return
    if name == "sec":
        title = element.find("title")
        derived = section_name(element.get("sec-type"), _text_of(title) if title is not None else "")
        # A subsection that names no section of its own ("Antibodies" under
        # "Materials and methods") is still in its parent's.
        if derived != DEFAULT_SECTION:
            section = derived
    elif name == "table-wrap":
        section = "Table"
    elif name == "fig":
        section = "Figure"
    if name == "tr":
        # A row is the unit, cells joined by a space: `itertext` over a row runs
        # `MAB7778` into `Bio-Techne` as one token, and a cell on its own has no
        # neighbour to be the context the platform requires.
        text = " ".join(t for t in (_text_of(cell) for cell in element) if t)
        if text:
            out.append((section, text))
        return
    if name in ("p", "title", "caption", "label", "list-item"):
        # Leaves. `caption` may itself hold <p>; take the whole block once,
        # here, and do not descend.
        text = _text_of(element)
        if text:
            out.append((section, text))
        return
    for child in element:
        _walk(child, section, out)


def sections_from_jats(xml_text):
    """``[(section, text), ...]`` in reading order from a Europe PMC full-text
    XML document. Title first, then the abstract(s), then the body and the
    back matter."""
    root = ElementTree.fromstring(xml_text)
    article = root if _local(root.tag) == "article" else next(
        (el for el in root.iter() if _local(el.tag) == "article"), root)
    out = []
    front = article.find("front")
    if front is not None:
        for title in front.iter():
            if _local(title.tag) == "article-title":
                text = _text_of(title)
                if text:
                    out.append(("Title", text))
                break
        for abstract in front.iter():
            if _local(abstract.tag) == "abstract":
                for block in abstract.iter():
                    if _local(block.tag) == "p":
                        text = _text_of(block)
                        if text:
                            out.append(("Abstract", text))
    body = article.find("body")
    if body is not None:
        _walk(body, DEFAULT_SECTION, out)
    back = article.find("back")
    if back is not None:
        _walk(back, DEFAULT_SECTION, out)
    return out


#: A full stop that does not end a sentence: after a label a methods section
#: uses in front of a catalogue number, or after a single letter (an initial,
#: a figure panel). Not after a digit -- "at 1:1000. Blots were" is a boundary,
#: and a decimal point is never followed by the space `_BOUNDARY` requires.
_NO_BREAK_BEFORE = re.compile(
    r"(?:\b(?:cat|no|nos|fig|figs|ref|refs|et al|e\.g|i\.e|vs|approx|ca|inc|co|ltd|"
    r"corp|dr|prof|mr|mrs|ms|st|sp|spp|[A-Za-z])\.)$", re.I)
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[\"“])")


def sentences(text):
    """Split one block into sentences, conservatively: a wrong split only
    shortens the context either side of a span, never loses one."""
    out, start = [], 0
    for match in _BOUNDARY.finditer(text):
        candidate = text[start:match.start()]
        if _NO_BREAK_BEFORE.search(candidate):
            continue
        out.append(candidate)
        start = match.end()
    tail = text[start:]
    if tail.strip():
        out.append(tail)
    return out


# ---------------------------------------------------------------------------
# Matching a sentence against a paper's reagents.
# ---------------------------------------------------------------------------

def spans_in(sentence, keys):
    """``[(start, end, kind, reagent)]`` for every identifier of this paper's
    printed in the sentence. RRIDs first, so ``RRID:AB_2881555`` is one span and
    not an RRID inside a catalogue-shaped token."""
    found = []
    for match in RRID_RE.finditer(sentence):
        hit = keys.get(rrid_key(match.group(1) or match.group(2)))
        if hit:
            found.append((match.start(), match.end(), hit[0], hit[1]))
    has_context = None
    for match in TOKEN_RE.finditer(sentence):
        token = match.group(0)
        hit = keys.get(("id", identifier_key(token)))
        if not hit:
            collapsed = collapse_identifier(token)
            hit = keys.get(("collapsed", collapsed)) if collapsed else None
        if not hit:
            continue
        if any(s < match.end() and match.start() < e for s, e, _, _ in found):
            continue
        if is_weak_identifier(token):
            # A short number is a catalogue number only where the sentence
            # says antibody, and never where it is a dilution or carries a
            # unit. The snapshot says the paper used the reagent; it does not
            # say that every "3872" in the paper is it.
            if looks_like_measurement(sentence, match.start(), match.end()):
                continue
            if has_context is None:
                has_context = bool(_ANTIBODY_CONTEXT_RE.search(sentence))
            if not has_context:
                continue
        # The tokeniser admits a full stop inside an identifier (`p38.MAPK`),
        # so one ending a sentence rides along: `IIH6C4.` was highlighted with
        # its full stop. The mark lands on the identifier alone.
        end = match.end()
        while end > match.start() + 1 and sentence[end - 1] == ".":
            end -= 1
        found.append((match.start(), end, hit[0], hit[1]))
    found.sort()
    return found


def annotate(blocks, reagents, base_url):
    """``(anns, dropped)`` for one article, in reading order.

    ``blocks`` is ``sections_from_jats``'s output. Sentences are numbered from 1
    across the article, chunks within the sentence, and every annotation quotes
    what the article printed: ``exact`` is the identifier as typeset, so a mark
    can land on it, and the tag is where the normalised lookup shows.

    ``dropped`` counts spans with no text either side — an identifier that is
    the whole of its sentence, which the platform will not take (at least one
    of prefix and postfix is mandatory). Counted rather than silently skipped.
    """
    keys = lookup_keys(reagents)
    if not keys:
        return [], 0
    anns, dropped, sentence_no = [], 0, 0
    for section, text in blocks:
        for sentence in sentences(text):
            sentence_no += 1
            chunk = 0
            for start, end, _kind, reagent in spans_in(sentence, keys):
                prefix = sentence[max(0, start - CONTEXT_CHARS):start]
                postfix = sentence[end:end + CONTEXT_CHARS]
                if not (prefix or postfix):
                    dropped += 1
                    continue
                chunk += 1
                anns.append({
                    "position": f"{sentence_no}.{chunk}",
                    "prefix": prefix,
                    "exact": sentence[start:end],
                    "postfix": postfix,
                    "section": section if section in FULL_TEXT_SECTIONS else DEFAULT_SECTION,
                    "tags": [tag_for(reagent, base_url)],
                })
    return anns, dropped


def title_annotation(title, reagents, base_url):
    """One sentence-based annotation on the title, one tag per antibody.

    CiteAb's attribution is a fact about the whole paper, not about a span of
    it, and the title is the one sentence every indexed article has — so this
    is the annotation every paper gets, whether or not Europe PMC holds its
    full text. It carries no ``position`` and no prefix/postfix: those are the
    named-entity fields, and the submission page's sentence-based example has
    neither. ``exact`` is the title as Europe PMC returned it, since that is
    the text the platform will look for.
    """
    title = re.sub(r"\s+", " ", title or "").strip()
    if not title or not reagents:
        return None
    return {"exact": title, "section": "Title",
            "tags": [tag_for(r, base_url) for r in reagents]}


def row_for(src, article_id, provider, anns):
    return {"src": src, "id": str(article_id), "provider": provider, "anns": anns}


def validate_row(row):
    """Every mandatory field on the submission page, as a list of problems.
    Empty means the row is submittable. Run over every row before writing,
    because the platform's answer comes an hour later by email."""
    problems = []
    if row.get("src") not in SOURCES:
        problems.append(f"src {row.get('src')!r} is not one of {sorted(SOURCES)}")
    if not str(row.get("id") or "").strip():
        problems.append("id is empty")
    if not str(row.get("provider") or "").strip():
        problems.append("provider is empty")
    anns = row.get("anns")
    if not anns:
        problems.append("anns is empty")
        return problems
    for i, ann in enumerate(anns):
        where = f"anns[{i}]"
        if not ann.get("exact"):
            problems.append(f"{where}.exact is empty")
        # A named-entity annotation carries a position; a sentence-based one
        # carries neither position nor context. Both are on the page.
        if "position" in ann:
            if not (ann.get("prefix") or ann.get("postfix")):
                problems.append(f"{where} has neither prefix nor postfix")
            if not re.fullmatch(r"\d+\.\d+", str(ann.get("position") or "")):
                problems.append(f"{where}.position {ann.get('position')!r} is not <sentence>.<chunk>")
        if ann.get("section") and ann["section"] not in FULL_TEXT_SECTIONS:
            problems.append(f"{where}.section {ann['section']!r} is not in the vocabulary")
        tags = ann.get("tags")
        if not tags:
            problems.append(f"{where}.tags is empty")
            continue
        for j, tag in enumerate(tags):
            if not tag.get("name"):
                problems.append(f"{where}.tags[{j}].name is empty")
            if not str(tag.get("uri") or "").startswith("http"):
                problems.append(f"{where}.tags[{j}].uri is not absolute")
    return problems


# ---------------------------------------------------------------------------
# Europe PMC: which of our papers it holds as open-access full text, and the text.
# ---------------------------------------------------------------------------

def _agent(email):
    return USER_AGENT + (f" (mailto:{email})" if email else "")


def fetch_json(url, email):
    request = Request(url, headers={"User-Agent": _agent(email), "Accept": "application/json"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url, email):
    request = Request(url, headers={"User-Agent": _agent(email)})
    with urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8")


def records_from(payload):
    """``{pmid: {pmcid, open_access, title}}`` out of one search response — keyed on
    the id we asked with, as ``citations_doi.dois_from`` is. THIS IS THE
    FUNCTION TO CHANGE if ``--check`` shows a different shape."""
    out = {}
    for row in (payload.get("resultList") or {}).get("result") or []:
        pmid = (row.get("pmid") or "").strip()
        if not pmid:
            continue
        out[pmid] = {
            "pmcid": (row.get("pmcid") or "").strip(),
            "open_access": str(row.get("isOpenAccess") or "").upper() == "Y",
            "title": (row.get("title") or "").strip(),
        }
    return out


def search_records(pmids, email):
    query = "(" + " OR ".join(f"EXT_ID:{i}" for i in pmids) + ") AND SRC:MED"
    url = SEARCH_ENDPOINT + "?" + urlencode({"query": query, "format": "json",
                                             "resultType": "lite", "pageSize": PAGE_SIZE})
    return records_from(fetch_json(url, email))


def full_text(pmcid, email):
    return fetch_text(FULLTEXT_ENDPOINT.format(pmcid=pmcid), email)


class Cache:
    """What Europe PMC has already answered, on disk, so a run that stops
    part-way does not ask again. One JSON of records; one XML file per PMC id."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.records_path = self.directory / "records.json"
        self.texts = self.directory / "fulltext"
        self.texts.mkdir(exist_ok=True)
        self.records = {}
        if self.records_path.exists():
            self.records = json.loads(self.records_path.read_text(encoding="utf-8"))

    def save_records(self):
        temporary = self.records_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.records), encoding="utf-8")
        temporary.replace(self.records_path)

    def text_path(self, pmcid):
        return self.texts / f"{pmcid}.xml"

    def get_text(self, pmcid):
        path = self.text_path(pmcid)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def put_text(self, pmcid, xml_text):
        self.text_path(pmcid).write_text(xml_text, encoding="utf-8")


def _say(*args, **kwargs):
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def _batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def papers_with_reagents(artefact, index):
    """``[(identifier, reagents)]`` for every paper in the snapshot that used
    at least one antibody the index holds. Preprints are returned too; the
    caller decides what to do with them."""
    out = []
    for identifier, slot in (artefact.get("by_pmid") or {}).items():
        record = unpack(artefact["papers"][slot], artefact["rrids"], artefact.get("tokens") or [])
        reagents = reagents_for(record, index)
        out.append((identifier, reagents))
    return out


def build(artefact, index, provider, cache, email=None, limit=0, delay=DELAY_SECONDS,
          base_url=None, say=_say, search=search_records, fetch=full_text, sleep=time.sleep):
    """Every submittable row, and a report that names what was left out.

    Returns ``(rows, report)``. ``report["left_out"]`` is a dict of reason ->
    list of identifiers, printed by the caller: a count with no list under it
    invents the noun, and here four different nouns share one count.
    """
    base_url = base_url or index.get("source") or "https://onlygoodantibodies.co.uk"
    left_out = {"preprint": [], "no_public_record": [], "not_in_europe_pmc": [],
                "text_fetch_failed": []}
    full_text = {"anchored": [], "no_open_access_full_text": [], "not_printed_in_text": []}
    candidates = []
    for identifier, reagents in papers_with_reagents(artefact, index):
        if not reagents:
            left_out["no_public_record"].append(identifier)
        elif not identifier.isdigit():
            left_out["preprint"].append(identifier)
        else:
            candidates.append((identifier, reagents))
    if limit:
        candidates = candidates[:limit]

    unknown = [pmid for pmid, _ in candidates if pmid not in cache.records]
    if unknown:
        say(f"Asking Europe PMC about {len(unknown):,} papers "
            f"({len(cache.records):,} already cached)...")
    failures = 0
    for batch in _batches(unknown, BATCH):
        try:
            found = search(batch, email)
        except (HTTPError, URLError, ValueError) as exc:
            failures += 1
            say(f"  batch failed ({exc}); continuing")
            continue
        for pmid in batch:
            # A paper Europe PMC did not return at all is recorded as such, so
            # the next run does not ask again; there is nothing to anchor to.
            cache.records[pmid] = found.get(pmid, {"pmcid": "", "open_access": False, "title": ""})
        cache.save_records()
        sleep(delay)

    rows, contextless = [], 0
    for pmid, reagents in candidates:
        record = cache.records.get(pmid)
        if not record:
            left_out["text_fetch_failed"].append(pmid)
            continue
        # Every paper: the attribution, on the title, on the abstract record.
        paper_level = title_annotation(record.get("title"), reagents, base_url)
        if paper_level is None:
            left_out["not_in_europe_pmc"].append(pmid)
            continue
        rows.append(row_for("MED", pmid, provider, [paper_level]))
        # Open-access full text: the printed identifiers as well, on the PMC
        # record, which is the view a reader of the full text is on.
        pmcid = record.get("pmcid")
        if not (pmcid and record.get("open_access")):
            full_text["no_open_access_full_text"].append(pmid)
            continue
        xml_text = cache.get_text(pmcid)
        if xml_text is None:
            try:
                xml_text = fetch(pmcid, email)
            except (HTTPError, URLError) as exc:
                failures += 1
                say(f"  {pmcid}: full text failed ({exc}); continuing")
                left_out["text_fetch_failed"].append(pmid)
                continue
            cache.put_text(pmcid, xml_text)
            sleep(delay)
        try:
            blocks = sections_from_jats(xml_text)
        except ElementTree.ParseError:
            left_out["text_fetch_failed"].append(pmid)
            continue
        anns, dropped = annotate(blocks, reagents, base_url)
        contextless += dropped
        if not anns:
            full_text["not_printed_in_text"].append(pmid)
            continue
        full_text["anchored"].append(pmid)
        rows.append(row_for("PMC", pmcid, provider, anns))

    busiest = max(((len(r["anns"]), r["id"]) for r in rows), default=(0, ""))
    report = {
        "papers_in_snapshot": len(artefact.get("by_pmid") or {}),
        "candidates": len(candidates),
        "papers_annotated": sum(1 for r in rows if r["src"] == "MED"),
        "rows": len(rows),
        "annotations": sum(len(r["anns"]) for r in rows),
        # Named, because a total cannot show one paper carrying thousands: the
        # first run put 2,519 on one article and the total read as plausible.
        "most_annotated": {"id": busiest[1], "annotations": busiest[0]},
        "contextless_spans": contextless,
        "full_text": full_text,
        "left_out": left_out,
        "failures": failures,
    }
    return rows, report


def print_report(report, say=_say):
    say()
    say(f"  papers in the snapshot        {report['papers_in_snapshot']:>7,}")
    say(f"  with a public OGA record      {report['candidates']:>7,}  (PubMed ids only)")
    say(f"  papers annotated on the title {report['papers_annotated']:>7,}  (src MED; every paper Europe PMC returned)")
    full = report["full_text"]
    say(f"  of which also in the text     {len(full['anchored']):>7,}  (src PMC; the printed identifiers)")
    for reason, words_ in (("no_open_access_full_text", "no open-access full text, title only"),
                           ("not_printed_in_text", "open access, but no identifier of the reagent printed — title only")):
        ids = full[reason]
        sample = ", ".join(ids[:5]) + (" …" if len(ids) > 5 else "")
        say(f"    {len(ids):>7,}  {words_}" + (f"  [{sample}]" if ids else ""))
    say(f"  rows                          {report['rows']:>7,}")
    say(f"  annotations                   {report['annotations']:>7,}")
    most = report.get("most_annotated") or {}
    if most.get("id"):
        say(f"  most on one article           {most['annotations']:>7,}  ({most['id']} — more "
            f"than a few dozen means something matched that is not a reagent)")
    if report.get("contextless_spans"):
        say(f"  spans not annotated           {report['contextless_spans']:>7,}  "
            f"(an identifier alone in its sentence, e.g. a table cell — the "
            f"platform needs text on one side)")
    say("  left out:")
    words = {
        "preprint": "preprint ids — PPR is not on the platform's source list; ask",
        "no_public_record": "no antibody with a published OGA figure",
        "not_in_europe_pmc": "Europe PMC returned no record or no title for the PubMed id",
        "text_fetch_failed": "Europe PMC could not be asked, or the text could not be read — rerun",
    }
    for reason, ids in report["left_out"].items():
        sample = ", ".join(ids[:5]) + (" …" if len(ids) > 5 else "")
        say(f"    {len(ids):>7,}  {words[reason]}" + (f"  [{sample}]" if ids else ""))
    if report["failures"]:
        say(f"  {report['failures']} request(s) failed; rerun to retry them")


def write_files(rows, out_dir, stem):
    """One JSON object per line, under 10,000 rows a file, and a ``.tar.gz``
    holding the parts when there is more than one — the platform takes a
    single upload either way. Returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for n, chunk in enumerate(_batches(rows, MAX_ROWS_PER_FILE), 1):
        path = out_dir / f"{stem}.part{n:03d}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in chunk:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        parts.append(path)
    if len(parts) <= 1:
        if parts:
            single = out_dir / f"{stem}.jsonl"
            parts[0].replace(single)
            return [single]
        return []
    archive = out_dir / f"{stem}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in parts:
            tar.add(path, arcname=path.name)
    return parts + [archive]


# ---------------------------------------------------------------------------
# Submission: the private storage Europe PMC provides.
#
# It is MinIO, an S3-compatible object store. A submission is one object put
# into the `submissions` bucket; the platform polls it hourly, emails twice
# (loading started; succeeded or failed with the reason), and writes the same
# text to `Log_<file>.txt` beside the upload and `Result_<file>.txt` in
# `results`. Uploading is the act that publishes -- every annotation on the
# platform is public domain by its ground rules -- so nothing here uploads
# unless told to, and credentials come from the environment, never the command
# line, where they would land in shell history and Actions logs.
#
# The `minio` driver is imported only inside these functions, so the build
# half stays standard-library and a laptop with a bare python3 can still run it.
# ---------------------------------------------------------------------------

#: The programmatic endpoint from the submission page. NOT the URL for the
#: browser, which Europe PMC gives out with the credentials.
STORAGE_ENDPOINT = "annotations.europepmc.org"
SUBMISSIONS_BUCKET = "submissions"
RESULTS_BUCKET = "results"
CREDENTIAL_VARS = ("EUROPEPMC_STORAGE_USER", "EUROPEPMC_STORAGE_PASSWORD")
ENDPOINT_VAR = "EUROPEPMC_STORAGE_ENDPOINT"


class SubmissionRefused(Exception):
    """Nothing was sent, and the message says what was missing."""


def submission_file(written):
    """The one file the platform takes: the archive when there is one, else the
    single part. ``write_files`` returns the parts as well, for inspection."""
    archives = [p for p in written if str(p).endswith(".tar.gz")]
    if archives:
        return archives[0]
    return written[0] if written else None


def storage_client(environ=None, driver=None):
    """A client for the storage, or ``SubmissionRefused`` naming what is missing.

    ``driver`` is the ``Minio`` class; left ``None`` it is imported here, so a
    checkout without the package can still build files and is told exactly
    what to install when it tries to send one.
    """
    environ = os.environ if environ is None else environ
    user, password = (environ.get(v) for v in CREDENTIAL_VARS)
    if not (user and password):
        raise SubmissionRefused(
            f"no storage credentials: set {CREDENTIAL_VARS[0]} and "
            f"{CREDENTIAL_VARS[1]} in the environment (the username and password "
            f"Europe PMC sent with the provider id). Nothing was sent.")
    if driver is None:
        try:
            from minio import Minio as driver  # noqa: N813
        except ImportError:
            raise SubmissionRefused(
                "the storage driver is not installed: pip install minio. "
                "Nothing was sent.") from None
    endpoint = environ.get(ENDPOINT_VAR) or STORAGE_ENDPOINT
    return driver(endpoint, access_key=user, secret_key=password, secure=True)


def submit(path, client, say=_say):
    """Put one file into ``submissions`` and read it back.

    Returns the object name, which is what the emails and the result files
    will carry. The read-back is the point: an upload that raised nothing is
    not the same as the file being there at the size it left here.
    """
    path = Path(path)
    if not path.exists():
        raise SubmissionRefused(f"{path} does not exist; nothing was sent.")
    name = path.name
    say(f"Uploading {name} ({path.stat().st_size:,} bytes) to {SUBMISSIONS_BUCKET}/ ...")
    client.fput_object(SUBMISSIONS_BUCKET, name, str(path),
                       content_type="application/octet-stream")
    stored = client.stat_object(SUBMISSIONS_BUCKET, name)
    size = getattr(stored, "size", None)
    if size != path.stat().st_size:
        raise SubmissionRefused(
            f"{name} was sent but reads back at {size} bytes against "
            f"{path.stat().st_size:,} here. Do not trust this upload; send it again.")
    say(f"Stored: {SUBMISSIONS_BUCKET}/{name}, {size:,} bytes, read back.")
    say("Europe PMC polls the folder about hourly and emails twice: 'Loading ... "
        "starting', then 'performed successfully' or 'failed' with the reason. "
        f"The same text lands as Log_{name}.txt beside the upload and "
        f"Result_{name}.txt in {RESULTS_BUCKET}/; --results {name} fetches both.")
    return name


def _object_text(client, bucket, name):
    """The object's text, or ``None`` when it is not there."""
    try:
        response = client.get_object(bucket, name)
    except Exception as exc:  # the driver's S3Error, or a fake's KeyError
        if isinstance(exc, KeyError) or getattr(exc, "code", None) in ("NoSuchKey", "NoSuchBucket"):
            return None
        raise
    try:
        return response.read().decode("utf-8", "replace")
    finally:
        for closer in ("close", "release_conn"):
            method = getattr(response, closer, None)
            if callable(method):
                method()


def results(client, name="", say=_say):
    """What the platform said. With a submission name, its Result_ and Log_
    text; without one, every result on file, newest last."""
    if name:
        found = False
        for bucket, prefix in ((RESULTS_BUCKET, "Result_"), (SUBMISSIONS_BUCKET, "Log_")):
            text = _object_text(client, bucket, f"{prefix}{name}.txt")
            say(f"--- {bucket}/{prefix}{name}.txt ---")
            if text is None:
                say("(not there yet -- the platform runs about hourly)")
            else:
                found = True
                say(text.rstrip())
        return found
    names = sorted(getattr(o, "object_name", str(o)) for o in client.list_objects(RESULTS_BUCKET))
    if not names:
        say(f"Nothing in {RESULTS_BUCKET}/ yet.")
    for n in names:
        say(f"  {n}")
    return bool(names)


def load_index(path=None, email=None, say=_say):
    """The extension index: a local file if given, else the served one, else
    the bundled copy — saying which, since the bundled copy is a fixture-sized
    stand-in and a run over it annotates almost nothing."""
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8")), str(path)
    try:
        return fetch_json(INDEX_URL, email), INDEX_URL
    except (HTTPError, URLError, ValueError) as exc:
        say(f"Could not fetch {INDEX_URL} ({exc}); using the bundled copy, which "
            f"holds {len(json.loads(BUNDLED_INDEX.read_text())['antibodies'])} antibodies.")
        return json.loads(BUNDLED_INDEX.read_text(encoding="utf-8")), str(BUNDLED_INDEX)


def check(artefact, index, email, say=_say):
    """One paper end to end, printed, so the API shape and the annotation can
    be judged before a full run. Picks the first PubMed paper with a public
    record; prints the raw search row, then every annotation it would emit."""
    candidate = next(((i, r) for i, r in papers_with_reagents(artefact, index)
                      if r and i.isdigit()), None)
    if candidate is None:
        say("No paper in the snapshot uses an antibody this index holds. Is this "
            "the bundled index rather than the served one?")
        return False
    pmid, reagents = candidate
    say(f"Asking Europe PMC about PubMed id {pmid} "
        f"({', '.join(r['catalogue'] or r['rrid'] for r in reagents)})...\n")
    records = search_records([pmid], email)
    say(json.dumps(records, indent=2))
    record = records.get(pmid)
    if not record:
        say(f"\nNo record came back keyed on {pmid}. Compare against records_from().")
        return False
    base_url = index.get("source") or "https://onlygoodantibodies.co.uk"
    paper_level = title_annotation(record.get("title"), reagents, base_url)
    if paper_level is None:
        say("\nThe record carries no title, so there is nothing to put the paper-level "
            "annotation on. Compare against records_from().")
        return False
    say("\nThe paper-level row (every paper gets one):")
    say(json.dumps(row_for("MED", pmid, "<provider>", [paper_level]), indent=2, ensure_ascii=False))
    if not (record["pmcid"] and record["open_access"]):
        say("\nThat paper has no open-access full text, so no in-text spans; the "
            "shape parsed. Run with --limit 20 to see a paper that has.")
        return True
    xml_text = full_text(record["pmcid"], email)
    blocks = sections_from_jats(xml_text)
    say(f"\n{record['pmcid']}: {len(blocks)} blocks, sections "
        f"{sorted(set(s for s, _ in blocks))}")
    anns, dropped = annotate(blocks, reagents, base_url)
    say("\nThe full-text row (open-access papers get this as well):")
    say(json.dumps(row_for("PMC", record["pmcid"], "<provider>", anns), indent=2, ensure_ascii=False)[:6000])
    if dropped:
        say(f"\n{dropped} span(s) not annotated: an identifier alone in its sentence.")
    if not anns:
        say("\nThe text was read but none of the reagent's identifiers were printed "
            "in it. That is a real outcome (CiteAb's join is on catalogue number, "
            "and the number may be in a supplementary table), not a shape problem.")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build Europe PMC annotation files from the citation snapshot and the extension index.")
    parser.add_argument("--artefact", default=None, help=f"Citation snapshot. Default: {ARTEFACT}")
    parser.add_argument("--index", default=None,
                        help=f"Extension index JSON. Default: fetch {INDEX_URL}")
    parser.add_argument("--provider", default=None,
                        help="The provider id Europe PMC assigned. Required to write files.")
    parser.add_argument("--email", default=None,
                        help="Contact address, sent in the User-Agent. Europe PMC asks for one.")
    parser.add_argument("--check", action="store_true",
                        help="Annotate ONE paper, print everything, and stop.")
    parser.add_argument("--limit", type=int, default=0, help="Only this many papers. For a trial.")
    parser.add_argument("--cache", default=None, help="Cache directory. Default: <out>/cache")
    parser.add_argument("--out", default="europepmc_annotations", help="Output directory.")
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS)
    parser.add_argument("--apply", action="store_true",
                        help="Write the files. Without it, fetch, report, and write nothing.")
    parser.add_argument("--submit", action="store_true",
                        help=(f"After --apply, upload the file to Europe PMC's storage. Needs "
                              f"{CREDENTIAL_VARS[0]} and {CREDENTIAL_VARS[1]} in the environment "
                              f"and the minio package. This is the act that publishes."))
    parser.add_argument("--submit-file", default=None, metavar="PATH",
                        help="Upload an already-built file and stop.")
    parser.add_argument("--results", nargs="?", const="", default=None, metavar="NAME",
                        help="Print what the platform said about NAME (or list every result) and stop.")
    args = parser.parse_args(argv)

    if args.submit_file is not None or args.results is not None:
        try:
            client = storage_client()
            if args.results is not None:
                return 0 if results(client, args.results) else 1
            submit(args.submit_file, client)
            return 0
        except SubmissionRefused as exc:
            print(exc, file=sys.stderr)
            return 2

    path = Path(args.artefact) if args.artefact else ARTEFACT
    if not path.exists():
        print(f"No citation snapshot at {path}. Run build_citation_index first.", file=sys.stderr)
        return 2
    artefact = json.loads(path.read_text(encoding="utf-8"))
    if not args.email:
        _say("No --email given. Europe PMC asks for a contact address so they can "
             "reach whoever is making the requests; please supply one.\n")
    index, index_from = load_index(args.index, args.email)
    _say(f"Index: {index_from} ({len(index.get('antibodies') or {}):,} antibodies with public records)")

    if args.check:
        try:
            return 0 if check(artefact, index, args.email) else 1
        except (HTTPError, URLError) as exc:
            print(f"Could not reach Europe PMC: {exc}", file=sys.stderr)
            return 2

    if args.apply and not args.provider:
        print("--apply needs --provider: the id Europe PMC assigned when you emailed "
              "annotations@europepmc.org. Nothing is written without it.", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    cache = Cache(args.cache or out_dir / "cache")
    rows, report = build(artefact, index, args.provider or "<provider>", cache,
                         email=args.email, limit=args.limit, delay=args.delay)
    print_report(report)

    problems = [(row["id"], p) for row in rows for p in validate_row(row)]
    if problems:
        _say(f"\n{len(problems)} row(s) would fail the platform's rules; nothing written:")
        for article_id, problem in problems[:20]:
            _say(f"  {article_id}: {problem}")
        return 1
    if not args.apply:
        _say(f"\nDry run: nothing written. Re-run with --apply --provider <id> to write "
             f"into {out_dir}/. Europe PMC's answers are cached in {cache.directory}/, "
             f"so that costs no more requests.")
        return 0
    stem = f"oga_annotations.{time.strftime('%Y_%m_%d_%H%M')}"
    written = write_files(rows, out_dir, stem)
    for w in written:
        _say(f"Wrote {w} ({w.stat().st_size:,} bytes)")
    to_send = submission_file(written)
    if not args.submit:
        _say(f"Not sent. Upload {to_send.name} to the 'submissions' folder of the "
             f"private storage Europe PMC provided, or re-run with --submit-file "
             f"{to_send} and the credentials in the environment.")
        return 0
    try:
        submit(to_send, storage_client())
    except SubmissionRefused as exc:
        print(f"{exc}\nThe files above are written and can be uploaded by hand "
              f"or with --submit-file {to_send}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
