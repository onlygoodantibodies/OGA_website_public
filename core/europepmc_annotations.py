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
antibody and linking to its page on onlygoodantibodies.co.uk, which is public
and needs no login (a ground rule of the platform).

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

NAMED ENTITIES ONLY, AND THE NAME IS THE NAME (Europe PMC's review, 28 Sep 2026)
------------------------------------------------------------------------------
The first sample carried a sentence-based annotation on every paper's title,
so that the abstract-only majority got one, with the verdicts written into the
tag. Europe PMC's annotations team reviewed it and asked for two changes, both
now the rule here:

* **Named-entity annotations only.** Each annotation refers to an antibody, a
  named entity, not to a sentence. So an annotation exists only where an
  identifier of the antibody is printed — catalogue number, RRID or clone —
  and it is anchored on those characters with a ``position`` and its
  neighbours in the sentence.
* **The tag's ``name`` is the canonical name of the antibody and nothing
  else.** No applications, no verdicts, no comments: their schema has nowhere
  for them, and everything else belongs on the page the tag links to. The
  canonical spelling used here is the RRID citation form the literature
  already uses — ``Proteintech Cat# 10782-2-AP, RRID:AB_2303286``.

Where the identifiers are looked for is everything Europe PMC shows for the
paper: the **title and abstract** for every paper (a ``src: "MED"`` row on the
PubMed record), and the **full text** for an open-access paper as well (a
``src: "PMC"`` row on the PMC record). A paper CiteAb says used the antibody
but whose text prints no identifier anywhere Europe PMC holds — which is most
of the abstract-only papers, since catalogue numbers live in Methods — gets
nothing, and the report counts and lists it as such. That is the cost of the
named-entity rule and it is stated rather than worked around.

**Two routes since 29 Sep 2026, by what Europe PMC holds** (the owner's rule):
a paper with open-access full text gets the named-entity annotations above; a
paper held as title and abstract only gets, with ``--abstract-route``, **one**
sentence-based annotation on its title whose tags name every antibody the
snapshot says it used. That second kind is the one the review asked us not to
send, so it goes in its own file (the validator checks one kind per file),
``--submit`` never sends it, and it waits on Europe PMC agreeing. **Preprints go
through the journal article they became** (``Preprint of`` on the preprint's
record): ``PPR`` is not a source the platform takes, and 1,529 of the 3,005
preprints with a public record have a PubMed version -- 288 of them articles
the snapshot did not hold.

What the verdicts lose by leaving the tag, the link keeps: the gene page
focused on the antibody is drawn from the live database, so the reader who
clicks sees the current result, whatever the annotation's date.

The platform's own validator settles most of what was assumed (29 Sep 2026).
It is a Python program, not a page -- github.com/EuropePMC/EuropePMC-Annotation-
Validator -- and **it validates against the schema it downloads at run time from
europepmc.org/docs/ne_annotation_schema.json, not the copy in its repository.
The two differ** (read from the owner's browser, 29 Sep 2026; the cloud session
is challenged by Cloudflare there): the live one requires ``position``,
``exact`` and ``tags`` on every annotation and has no ``type`` field, while the
repository's requires ``exact``, ``type`` and ``tags``. Both are vendored --
``core/data/europepmc_ne_annotation_schema.json`` (live, the authority) and
``..._github.json`` -- and a row must satisfy both, which it does by carrying
``position`` *and* ``type``: neither schema forbids a key it does not name. A row
is exactly ``src``, ``id``, ``provider`` and ``anns``; ``src`` is one of nine
codes and ``PPR`` is not among them, so preprints (3,006 of the snapshot's 29,207
papers) are left out and counted rather than guessed at; ``position`` is a free
string whose example is ``1.2``, so ``<sentence>.<chunk>`` -- sentences numbered
from 1 across the article in reading order, chunks within the sentence -- is a
reading it allows rather than one it prescribes; a row that is not full text
takes only the sections Title and Abstract; a prefix or a postfix is mandatory
(both keys present, since the validator's code reads both); and every row's
``provider`` must equal the name the validator is run with. ``validate_row``
mirrors all of that, so a build refuses to write what the validator would refuse.

Two things it does not settle, and the run's ``--check`` prints enough to judge:

* Rows anchored in full text use ``src: "PMC"`` and the PMC id, since the
  section vocabulary the page lists for ``src=PMC`` is the one full text has.
* The ``name`` is the RRID citation form. Their word was "canonical name"; if
  they want the supplier's product name instead, that is one function.

Nothing here is a verdict on a product. The tag is the antibody's name and the
linked page carries the result and its caveats; an annotation is a pointer to
characterisation data, which is all the extension's mark ever was.
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
#: Every source but full text takes only these two (the submission page:
#: "For any other article source the possible values are: Title, Abstract").
ABSTRACT_SECTIONS = ("Title", "Abstract")

#: Characters of the sentence kept either side of the span. The submission
#: page's examples carry about this much; it is context for a reader, not a
#: locator, and the sentence position plus the exact text do the locating.
CONTEXT_CHARS = 40

# The extension index's four keys, which this reads — not
# `core/recommendations.py::APPLICATIONS`: the index carries no IHC yet, so an
# annotation has no IHC result to state.
APPLICATIONS = ("WB", "IP", "IF", "FC")

#: A submission row is exactly these four keys — the platform's schema says
#: ``minProperties: 4, maxProperties: 4`` — so a fifth, however helpful, fails
#: every row in the file.
SUBMISSION_KEYS = ("src", "id", "provider", "anns")

#: The ``type`` every named-entity annotation carries. The schema in the
#: validator's GitHub repository requires it
#: (``core/data/europepmc_ne_annotation_schema_github.json``); the one the
#: validator downloads at run time has no such field and no
#: ``additionalProperties`` rule, so it is allowed there and required here. It is the annotation type
#: registered with the platform for this provider, so this word and the word in
#: the email to annotations@europepmc.org are one word.
ANNOTATION_TYPE = "antibody"

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


def canonical_name(reagent):
    """The antibody's name and nothing else — Europe PMC's rule for the tag.

    The RRID citation form is the one canonical spelling the literature already
    uses (``Proteintech Cat# 10782-2-AP, RRID:AB_2303286``), and it is what a
    reader can paste into a search box. Every other fact — what the paper used
    it for, what OGA found — belongs on the linked page, by the platform's
    review of 28 Sep 2026; their schema has nowhere for it in ``name``.
    """
    head = " ".join(part for part in (
        reagent["supplier"],
        f"Cat# {reagent['catalogue']}" if reagent["catalogue"] else "",
    ) if part)
    return f"{head}, RRID:{reagent['rrid']}" if head else f"RRID:{reagent['rrid']}"


def tag_for(reagent, base_url):
    """One tag: the canonical name, and the page that carries everything else.
    The URI is the gene page focused on this antibody (``?ab=``), which is
    where the home page search sends a reader who types the same number, and
    which is drawn from the live database on every visit."""
    focus = reagent["catalogue"] or reagent["rrid"]
    uri = f"{base_url.rstrip('/')}/antibodies/{quote(reagent['gene'], safe='')}/?{urlencode({'ab': focus})}"
    return {"name": canonical_name(reagent), "uri": uri}


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
                    "type": ANNOTATION_TYPE,
                    "tags": [tag_for(reagent, base_url)],
                })
    return anns, dropped


_MARKUP_RE = re.compile(r"<[^>]+>")


def abstract_blocks(record):
    """``[(section, text)]`` for a PubMed record: the title and the abstract,
    which is all Europe PMC shows for a paper it holds without full text.
    A structured abstract arrives with HTML headings; the tags go, the words
    stay. Same shape as ``sections_from_jats``'s output, so ``annotate``
    reads both."""
    out = []
    for section, key in (("Title", "title"), ("Abstract", "abstract")):
        text = re.sub(r"\s+", " ", _MARKUP_RE.sub(" ", record.get(key) or "")).strip()
        if text:
            out.append((section, text))
    return out


def row_for(src, article_id, provider, anns):
    return {"src": src, "id": str(article_id), "provider": provider, "anns": anns}


def validate_row(row, provider=None):
    """Every rule Europe PMC's own validator applies to a named-entity row, as a
    list of problems; empty means submittable. ``provider`` is the name the
    validator is run with; left ``None`` (a dry run, which has no id yet) the
    row's own is only required to be there.

    The validator (github.com/EuropePMC/EuropePMC-Annotation-Validator) is a
    Python 2 program that checks each line against ``ne_annotation_schema.json``
    — vendored at ``core/data/europepmc_ne_annotation_schema.json`` (the live
    copy it downloads) beside its repository's ``..._github.json``, and the
    test derives the required keys from that file rather than retyping them —
    plus two rules in its code: the ``provider`` on every row must be the one
    supplied, and an annotation must carry a ``prefix`` or a ``postfix``. Run
    over every row before writing, because the platform's own answer comes an
    hour later by email. The first sample lacked ``type`` and passed the
    earlier version of this function; every row would have been refused.
    """
    problems = []
    extra = sorted(set(row) - set(SUBMISSION_KEYS))
    if extra:
        problems.append(f"unexpected key(s) {extra}: a row is exactly {list(SUBMISSION_KEYS)}")
    if row.get("src") not in SOURCES:
        problems.append(f"src {row.get('src')!r} is not one of {sorted(SOURCES)}")
    if not str(row.get("id") or "").strip():
        problems.append("id is empty")
    if not str(row.get("provider") or "").strip():
        problems.append("provider is empty")
    elif provider is not None and row.get("provider") != provider:
        # The validator's first rule in code: every row names the provider it
        # is run with, character for character.
        problems.append(f"provider {row.get('provider')!r} is not the supplied {provider!r}")
    anns = row.get("anns")
    if not anns:
        problems.append("anns is empty")
        return problems
    for i, ann in enumerate(anns):
        where = f"anns[{i}]"
        if not ann.get("exact"):
            problems.append(f"{where}.exact is empty")
        if not str(ann.get("type") or "").strip():
            problems.append(f"{where}.type is empty (every annotation carries {ANNOTATION_TYPE!r})")
        # The validator reads `annotation['prefix'] or annotation['postfix']`,
        # so a missing key is a KeyError there, not a pass: both keys are
        # present on every annotation, and at least one is not empty.
        missing = [k for k in ("prefix", "postfix") if k not in ann]
        if missing:
            problems.append(f"{where} lacks {missing}: the validator reads both keys")
        if not (ann.get("prefix") or ann.get("postfix")):
            problems.append(f"{where} has neither prefix nor postfix")
        # Required by the schema the validator downloads (not by its GitHub copy).
        if "position" not in ann:
            problems.append(f"{where}.position is missing (the live schema requires it)")
        elif not re.fullmatch(r"\d+\.\d+", str(ann.get("position") or "")):
            problems.append(f"{where}.position {ann.get('position')!r} is not <sentence>.<chunk>")
        allowed = FULL_TEXT_SECTIONS if row.get("src") == "PMC" else ABSTRACT_SECTIONS
        if ann.get("section") and ann["section"] not in allowed:
            problems.append(f"{where}.section {ann['section']!r} is not one of {list(allowed)} "
                            f"for src {row.get('src')!r}")
        tags = ann.get("tags")
        if not tags:
            problems.append(f"{where}.tags is empty")
            continue
        for j, tag in enumerate(tags):
            if not str(tag.get("name") or "").strip():
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
    """``{pmid: {pmcid, open_access, title, abstract}}`` out of one search response — keyed on
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
            "abstract": (row.get("abstractText") or "").strip(),
        }
    return out


def search_records(pmids, email):
    query = "(" + " OR ".join(f"EXT_ID:{i}" for i in pmids) + ") AND SRC:MED"
    url = SEARCH_ENDPOINT + "?" + urlencode({"query": query, "format": "json",
                                             "resultType": "core", "pageSize": PAGE_SIZE})
    return records_from(fetch_json(url, email))


def full_text(pmcid, email):
    return fetch_text(FULLTEXT_ENDPOINT.format(pmcid=pmcid), email)


def journal_versions_from(payload):
    """``{preprint id: PubMed id of its journal version, or ""}`` out of one
    search response. Europe PMC links a preprint to the article it became with
    a ``commentCorrection`` of type ``Preprint of`` whose source is ``MED``."""
    out = {}
    for row in (payload.get("resultList") or {}).get("result") or []:
        ppr = (row.get("id") or "").strip()
        if not ppr:
            continue
        links = ((row.get("commentCorrectionList") or {}).get("commentCorrection") or [])
        pmids = [str(c.get("id") or "").strip() for c in links
                 if c.get("type") == "Preprint of" and c.get("source") == "MED"]
        out[ppr] = next((p for p in pmids if p.isdigit()), "")
    return out


def search_preprints(ids, email):
    query = "(" + " OR ".join(f"EXT_ID:{i}" for i in ids) + ") AND SRC:PPR"
    url = SEARCH_ENDPOINT + "?" + urlencode({"query": query, "format": "json",
                                             "resultType": "core", "pageSize": PAGE_SIZE})
    return journal_versions_from(fetch_json(url, email))


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
        self.preprints_path = self.directory / "preprints.json"
        self.preprints = {}
        if self.preprints_path.exists():
            self.preprints = json.loads(self.preprints_path.read_text(encoding="utf-8"))

    def save_preprints(self):
        temporary = self.preprints_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.preprints), encoding="utf-8")
        temporary.replace(self.preprints_path)

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
          base_url=None, say=_say, search=search_records, fetch=full_text, sleep=time.sleep,
          preprint_search=search_preprints, sentence_rows=None):
    """Every submittable row, and a report that names what was left out.

    Returns ``(rows, report)``. ``report["left_out"]`` is a dict of reason ->
    list of identifiers, printed by the caller: a count with no list under it
    invents the noun, and here several different nouns share one count.

    **Two routes, decided by what Europe PMC holds for the paper** (the owner,
    29 Sep 2026). A paper with open-access full text gets named-entity
    annotations wherever an identifier is printed. A paper Europe PMC holds as
    title and abstract only gets, when ``sentence_rows`` is a list, **one**
    sentence-based annotation on its title instead, whose tags name every
    antibody the snapshot says it used -- filled into that list, since the
    platform validates one annotation kind per file. Left ``None``, an
    abstract-only paper is annotated only where its abstract prints an
    identifier, which is what Europe PMC's review of 28 Sep asked for; the
    title route waits on their agreement.

    **A preprint is annotated through the journal article it became**, which
    Europe PMC records as ``Preprint of``: its reagents join that PubMed id's,
    and only an identifier printed in that article is marked, so the
    preprint's attribution never draws a mark on text that does not name the
    reagent. A preprint with no journal version is left out and listed; the
    platform's source list has no ``PPR``.
    """
    base_url = base_url or index.get("source") or "https://onlygoodantibodies.co.uk"
    left_out = {"preprint_no_journal_version": [], "preprint_not_in_europe_pmc": [],
                "no_public_record": [],
                "not_in_europe_pmc": [], "text_fetch_failed": [],
                "identifier_not_printed": [], "no_title_for_abstract_route": []}
    where = {"abstract": [], "full_text": [], "no_open_access_full_text": [],
             "open_access_not_printed": [], "abstract_route": []}
    preprints = {"via_journal_version": [], "journal_version_added": [],
                 "journal_version_already_in_snapshot": [], "lookup_failed": []}
    candidates, pending = {}, []
    for identifier, reagents in papers_with_reagents(artefact, index):
        if not reagents:
            left_out["no_public_record"].append(identifier)
        elif not identifier.isdigit():
            pending.append((identifier, reagents))
        else:
            candidates[identifier] = list(reagents)
    failures = 0

    # Preprints: which journal article each became, asked once and cached.
    unknown = [i for i, _ in pending if i not in cache.preprints]
    if unknown:
        say(f"Asking Europe PMC which journal article {len(unknown):,} preprints became...")
    for batch in _batches(unknown, BATCH):
        try:
            found = preprint_search(batch, email)
        except (HTTPError, URLError, ValueError) as exc:
            failures += 1
            say(f"  preprint batch failed ({exc}); continuing")
            continue
        for ppr in batch:
            # None: Europe PMC holds no such preprint. "": it holds it, and
            # it has not become a journal article. Two facts, listed apart.
            cache.preprints[ppr] = found.get(ppr)
        cache.save_preprints()
        sleep(delay)
    for ppr, reagents in pending:
        if ppr not in cache.preprints:
            preprints["lookup_failed"].append(ppr)
            continue
        pmid = cache.preprints[ppr]
        if pmid is None:
            left_out["preprint_not_in_europe_pmc"].append(ppr)
            continue
        if not pmid:
            left_out["preprint_no_journal_version"].append(ppr)
            continue
        preprints["via_journal_version"].append(ppr)
        if pmid in candidates:
            preprints["journal_version_already_in_snapshot"].append(pmid)
            have = {r["rrid"] for r in candidates[pmid]}
            candidates[pmid] += [r for r in reagents if r["rrid"] not in have]
        else:
            preprints["journal_version_added"].append(pmid)
            candidates[pmid] = list(reagents)
    candidates = list(candidates.items())
    if limit:
        candidates = candidates[:limit]

    # A record cached before abstracts were kept is asked for again.
    unknown = [pmid for pmid, _ in candidates
               if "abstract" not in (cache.records.get(pmid) or {})]
    if unknown:
        say(f"Asking Europe PMC about {len(unknown):,} papers "
            f"({len(cache.records):,} already cached)...")
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
            cache.records[pmid] = found.get(
                pmid, {"pmcid": "", "open_access": False, "title": "", "abstract": ""})
        cache.save_records()
        sleep(delay)

    rows, contextless = [], 0
    for pmid, reagents in candidates:
        record = cache.records.get(pmid)
        if not record:
            left_out["text_fetch_failed"].append(pmid)
            continue
        if not (record.get("title") or record.get("abstract")):
            left_out["not_in_europe_pmc"].append(pmid)
            continue
        pmcid = record.get("pmcid")
        has_full_text = bool(pmcid and record.get("open_access"))
        if not has_full_text and sentence_rows is not None:
            # Abstract only: one link on the title, naming every antibody.
            where["no_open_access_full_text"].append(pmid)
            row = abstract_route_row(record, pmid, provider, reagents, base_url)
            if row is None:
                left_out["no_title_for_abstract_route"].append(pmid)
            else:
                sentence_rows.append(row)
                where["abstract_route"].append(pmid)
            continue
        annotated = False
        # Every paper: the identifiers printed in the title or abstract, on
        # the PubMed record, which is all Europe PMC shows without full text.
        anns, dropped = annotate(abstract_blocks(record), reagents, base_url)
        contextless += dropped
        if anns:
            rows.append(row_for("MED", pmid, provider, anns))
            where["abstract"].append(pmid)
            annotated = True
        # Open-access full text: the identifiers printed anywhere in it, on
        # the PMC record, which is the view a reader of the full text is on.
        if has_full_text:
            xml_text = cache.get_text(pmcid)
            if xml_text is None:
                try:
                    xml_text = fetch(pmcid, email)
                except (HTTPError, URLError) as exc:
                    failures += 1
                    say(f"  {pmcid}: full text failed ({exc}); continuing")
                    xml_text = None
                else:
                    cache.put_text(pmcid, xml_text)
                    sleep(delay)
            blocks = None
            if xml_text is not None:
                try:
                    blocks = sections_from_jats(xml_text)
                except ElementTree.ParseError:
                    blocks = None
            if blocks is None:
                left_out["text_fetch_failed"].append(pmid)
            else:
                anns, dropped = annotate(blocks, reagents, base_url)
                contextless += dropped
                if anns:
                    rows.append(row_for("PMC", pmcid, provider, anns))
                    where["full_text"].append(pmid)
                    annotated = True
                else:
                    where["open_access_not_printed"].append(pmid)
        else:
            where["no_open_access_full_text"].append(pmid)
        if not annotated and pmid not in left_out["text_fetch_failed"]:
            # CiteAb says the paper used it; nothing Europe PMC holds prints
            # an identifier of it. Named-entity annotations need the characters.
            left_out["identifier_not_printed"].append(pmid)

    busiest = max(((len(r["anns"]), r["id"]) for r in rows), default=(0, ""))
    report = {
        "papers_in_snapshot": len(artefact.get("by_pmid") or {}),
        "candidates": len(candidates),
        "papers_annotated": len(set(where["abstract"]) | set(where["full_text"])),
        "rows": len(rows),
        "annotations": sum(len(r["anns"]) for r in rows),
        "abstract_route_rows": len(sentence_rows) if sentence_rows is not None else None,
        # Named, because a total cannot show one paper carrying thousands: the
        # first run put 2,519 on one article and the total read as plausible.
        "most_annotated": {"id": busiest[1], "annotations": busiest[0]},
        "contextless_spans": contextless,
        "where": where,
        "preprints": preprints,
        "left_out": left_out,
        "failures": failures,
    }
    return rows, report


def abstract_route_row(record, pmid, provider, reagents, base_url):
    """One sentence-based row for a paper Europe PMC holds as title and
    abstract only: the title is the sentence, and each antibody the snapshot
    says the paper used is a tag -- its canonical name, and the page that
    carries what OGA found. ``None`` when there is no title to anchor on."""
    title = next((text for section, text in abstract_blocks(record) if section == "Title"), "")
    if not title:
        return None
    tags, seen = [], set()
    for reagent in reagents:
        tag = tag_for(reagent, base_url)
        if tag["uri"] not in seen:
            seen.add(tag["uri"])
            tags.append(tag)
    return row_for("MED", pmid, provider, [{
        "exact": title, "section": "Title", "type": ANNOTATION_TYPE, "tags": tags}])


def validate_sentence_row(row, provider=None):
    """The validator's rules for a sentence-based row, as a list of problems.

    From ``core/data/europepmc_sentence_annotation_schema_github.json`` and
    the validator's code: exactly four keys, ``exact`` and ``tags`` on every
    annotation, ``name`` and ``uri`` on every tag, the provider it is run with,
    and no ``position``, ``prefix`` or ``postfix`` -- the validator's SENTENCE
    branch reports those as the other kind's fields. Which copy of this schema
    it downloads at run time has not been read; the named-entity one differed
    from its repository copy (DECISIONS.md, 29 Sep 2026).
    """
    problems = []
    extra = sorted(set(row) - set(SUBMISSION_KEYS))
    if extra:
        problems.append(f"unexpected key(s) {extra}: a row is exactly {list(SUBMISSION_KEYS)}")
    if row.get("src") not in SOURCES:
        problems.append(f"src {row.get('src')!r} is not one of {sorted(SOURCES)}")
    if not str(row.get("id") or "").strip():
        problems.append("id is empty")
    if not str(row.get("provider") or "").strip():
        problems.append("provider is empty")
    elif provider is not None and row.get("provider") != provider:
        problems.append(f"provider {row.get('provider')!r} is not the supplied {provider!r}")
    anns = row.get("anns")
    if not anns:
        problems.append("anns is empty")
        return problems
    for i, ann in enumerate(anns):
        where = f"anns[{i}]"
        if not ann.get("exact"):
            problems.append(f"{where}.exact is empty")
        for key in ("position", "prefix", "postfix"):
            if key in ann:
                problems.append(f"{where}.{key} belongs to named-entity rows, not sentence rows")
        allowed = FULL_TEXT_SECTIONS if row.get("src") == "PMC" else ABSTRACT_SECTIONS
        if ann.get("section") and ann["section"] not in allowed:
            problems.append(f"{where}.section {ann['section']!r} is not one of {list(allowed)}")
        tags = ann.get("tags")
        if not tags:
            problems.append(f"{where}.tags is empty")
            continue
        for j, tag in enumerate(tags):
            if not str(tag.get("name") or "").strip():
                problems.append(f"{where}.tags[{j}].name is empty")
            if not str(tag.get("uri") or "").startswith("http"):
                problems.append(f"{where}.tags[{j}].uri is not absolute")
    return problems


def print_report(report, say=_say):
    say()
    say(f"  papers in the snapshot        {report['papers_in_snapshot']:>7,}")
    say(f"  with a public OGA record      {report['candidates']:>7,}  (PubMed ids only)")
    where = report["where"]
    say(f"  papers annotated              {report['papers_annotated']:>7,}  (an identifier printed somewhere Europe PMC shows)")
    say(f"    in the title or abstract    {len(where['abstract']):>7,}  (src MED)")
    say(f"    in open-access full text    {len(where['full_text']):>7,}  (src PMC)")
    say(f"  rows                          {report['rows']:>7,}")
    say(f"  annotations                   {report['annotations']:>7,}")
    if report.get("abstract_route_rows") is not None:
        say(f"  abstract-only papers linked   {report['abstract_route_rows']:>7,}  (one title "
            f"link each, in its own file; waits on Europe PMC agreeing to it)")
    pre = report.get("preprints") or {}
    if pre.get("via_journal_version"):
        say(f"  preprints via journal version {len(pre['via_journal_version']):>7,}  "
            f"({len(pre['journal_version_added']):,} journal articles added, "
            f"{len(pre['journal_version_already_in_snapshot']):,} already in the snapshot)")
    if pre.get("lookup_failed"):
        say(f"  preprints not looked up       {len(pre['lookup_failed']):>7,}  (Europe PMC "
            f"could not be asked -- rerun)")
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
        "preprint_no_journal_version": ("preprints with no journal version on Europe PMC -- "
                                        "PPR is not on the platform's source list"),
        "no_public_record": "no antibody with a published OGA figure",
        "not_in_europe_pmc": "Europe PMC returned no record or no title for the PubMed id",
        "text_fetch_failed": "Europe PMC could not be asked, or the text could not be read — rerun",
        "identifier_not_printed": ("CiteAb says the paper used it, but no identifier is printed in "
                                   "any text Europe PMC holds — a named entity needs the characters"),
        "no_title_for_abstract_route": "abstract-only, and Europe PMC holds no title to link on",
        "preprint_not_in_europe_pmc": "preprint ids Europe PMC returned no record for",
    }
    for reason, ids in report["left_out"].items():
        sample = ", ".join(ids[:5]) + (" …" if len(ids) > 5 else "")
        say(f"    {len(ids):>7,}  {words[reason]}" + (f"  [{sample}]" if ids else ""))
        if reason == "identifier_not_printed" and ids:
            no_full = len(set(ids) & set(where["no_open_access_full_text"]))
            say(f"             of which {no_full:,} have no open-access full text, so only their "
                f"title and abstract could be read; {len(ids) - no_full:,} are open access "
                f"and still print none")
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
    endpoint, secure = storage_endpoint(environ.get(ENDPOINT_VAR) or STORAGE_ENDPOINT)
    return driver(endpoint, access_key=user, secret_key=password, secure=secure)


def storage_endpoint(value):
    """``(host[:port], secure)`` from the endpoint as Europe PMC may write it.

    The client takes a bare ``host[:port]`` and raises "path in endpoint is not
    allowed" on ``https://annotations.europepmc.org`` -- the form an email
    naturally gives it in. A scheme decides ``secure``; anything carrying a
    path is refused by name, since guessing which part is the host is how an
    upload goes somewhere nobody chose.
    """
    text = (value or "").strip()
    secure = True
    for scheme, is_secure in (("https://", True), ("http://", False)):
        if text.lower().startswith(scheme):
            text, secure = text[len(scheme):], is_secure
            break
    text = text.rstrip("/")
    if not text or "/" in text or "://" in text:
        raise SubmissionRefused(
            f"{ENDPOINT_VAR} is {value!r}; it must be a host, optionally with a port "
            f"(e.g. {STORAGE_ENDPOINT} or https://{STORAGE_ENDPOINT}:9000). Nothing was sent.")
    return text, secure


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
    anns, dropped = annotate(abstract_blocks(record), reagents, base_url)
    if anns:
        say("\nThe PubMed-record row (identifiers printed in the title or abstract):")
        say(json.dumps(row_for("MED", pmid, "<provider>", anns), indent=2, ensure_ascii=False))
    else:
        say("\nNo identifier of the reagent is printed in the title or abstract, so no "
            "row on the PubMed record; that is the usual case.")
    if not (record["pmcid"] and record["open_access"]):
        say("\nThat paper has no open-access full text, so no in-text spans; the "
            "shape parsed. Run with --limit 20 to see a paper that has.")
        return True
    xml_text = full_text(record["pmcid"], email)
    blocks = sections_from_jats(xml_text)
    say(f"\n{record['pmcid']}: {len(blocks)} blocks, sections "
        f"{sorted(set(s for s, _ in blocks))}")
    anns, dropped = annotate(blocks, reagents, base_url)
    say("\nThe PMC-record row (identifiers printed in the open-access full text):")
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
    parser.add_argument("--abstract-route", action="store_true",
                        help=("Give each abstract-only paper one link on its title, naming its "
                              "antibodies, in a separate file. Not yet agreed with Europe PMC, "
                              "so --submit never sends that file."))
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

    if args.submit and not args.apply:
        print("--submit needs --apply: there is no file to send from a dry run. "
              "Nothing was sent.", file=sys.stderr)
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
    sentences = [] if args.abstract_route else None
    rows, report = build(artefact, index, args.provider or "<provider>", cache,
                         email=args.email, limit=args.limit, delay=args.delay,
                         sentence_rows=sentences)
    print_report(report)

    checked = args.provider if args.apply else None
    problems = [(row["id"], p) for row in rows for p in validate_row(row, checked)]
    problems += [(row["id"], p) for row in sentences or []
                 for p in validate_sentence_row(row, checked)]
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
    stamp = time.strftime('%Y_%m_%d_%H%M')
    written = write_files(rows, out_dir, f"oga_annotations.{stamp}")
    for w in written:
        _say(f"Wrote {w} ({w.stat().st_size:,} bytes)")
    if sentences:
        # Its own file: the validator checks one annotation kind per file.
        for w in write_files(sentences, out_dir, f"oga_abstract_route.{stamp}"):
            _say(f"Wrote {w} ({w.stat().st_size:,} bytes) -- the abstract-only title links. "
                 f"Not sent by --submit; Europe PMC have not agreed to this route yet.")
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
