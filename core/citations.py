"""Which papers used which of our antibodies, and for what — the one reader.

The browser extension knows what an antibody IS but has to *guess* what the paper
in front of it used that antibody FOR, from where the words sit on the page. That
guess is measured bad — WB 87% correct, **IF 20%, IP 0 of 3**, IHC and FC never
named at all (``browser-extension/CLAUDE.md``) — which is why ``RELIABLE_CUES`` is
``["WB"]`` and a detected application is not allowed to narrow a verdict.

CiteAb has already answered the question for tens of thousands of papers: *this
paper used this product for these applications*. So on a paper CiteAb covers, the
extension can stop guessing and look it up. This module is the one reader for that
lookup table.

**A looked-up application and a guessed one are different facts and must stay
different.** A looked-up one may narrow a verdict; a guessed one may not. Anything
merging them re-creates the measured-bad behaviour with a confident face on it, so
every record here carries how it was arrived at and the extension draws the two
apart.

WHAT THIS IS NOT
----------------
Absence here is not absence from the literature. CiteAb documents its own gaps —
products it could not resolve, publications under closed-access embargo — and the
join behind the artefact is on **catalogue number**, because CiteAb's schema holds
no RRID. So a paper we cannot find is a paper we cannot find, never a paper that
does not cite the reagent. The extension must say the first thing.

HOW A PAPER IS RECOGNISED
-------------------------
Three keys, because no single one is available everywhere:

* **title hash** — every publication in the source carries a title (30,808 of
  30,808), and a publisher page carries ``citation_title``. This is the key that
  works on an ordinary publisher page.
* **PubMed / preprint id** — in the URL on ``pubmed.ncbi.nlm.nih.gov``,
  ``pmc.ncbi.nlm.nih.gov`` and ``europepmc.org``, three sites the extension
  already runs on.
* **DOI** — the precise key, and the one the source does not have. CiteAb stores
  ``pubmedid``/``pprid`` and no DOI at all, so filling ``by_doi`` needs a
  resolution pass against PubMed that the builder does separately. The slot is
  here so adding it later is data, not a schema change.

A hit is confirmed against the **year** before it is believed. A four-byte title
hash over ~30k papers collides with probability far below anything worth worrying
about, but the confirmation costs nothing and turns "vanishingly unlikely" into
"cannot happen silently", which is the standard the rest of this codebase holds.

THE TITLE NORMALISER IS A THIRD PRINTED STRING
----------------------------------------------
``mcp_servers`` and ``browser-extension`` already have to agree about how a
catalogue number is printed. ``normalise_title`` is the same kind of promise: it is
mirrored in ``browser-extension/src/paper.js::normaliseTitle`` and the two are
pinned against **shared vectors** in ``core/data/title_normalisation_vectors.json``
so neither can drift without a test failing. That is deliberately stricter than the
catalogue-number pair, which has no shared vectors and relies on a rule in prose.

**Greek letters are why the vectors exist.** 9.1% of the titles carry one (β 1,343,
α 947, κ 505 …), and a normaliser that merely strips non-alphanumerics deletes them
— so ``β-catenin`` becomes ``catenin`` while a publisher spelling it out gives
``beta catenin``, and the paper is never recognised. Transliterating fixes the
common case. It does not fix every case (a page setting ``TGF-b`` for ``TGF-β``
still misses), and that is acceptable **only because the failure is a miss**: an
unrecognised paper falls back to the old proximity behaviour and loses nothing,
where a wrongly-recognised paper would attach another paper's applications to this
one. Never trade that direction away for a higher match rate.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

# Named for the vintage of CiteAb's data, not the day the file was written: it is a
# snapshot of somebody else's database and the only date that describes it is
# theirs. `run_meta.citeab_updated_*` in the source is where it comes from.
ARTEFACT = DATA_DIR / "citeab_papers_2026_05.json"

SCHEMA = 1

# Bit flags on one (paper, reagent) record. The first four are OGA's applications;
# the last two say why an application is NOT one of the four, which is a different
# statement from "we have no verdict" and reaches the reader as different words.
WB, IP, IF, FC = 1, 2, 4, 8
AMBIGUOUS = 16      # CiteAb's token maps to one of ours but does not say which
                    # preparation -- bare `IF` cannot tell ICC-IF from IHC-IF, and
                    # OGA's IF verdict is cultured cells only.
UNTESTED_APP = 32   # the paper used it for something OGA does not test at all
                    # (IHC above all, then ChIP, ELISA, PLA). The token is named
                    # on the record, because "used for IHC" is a usable sentence
                    # and "used for something we don't test" is not.

APPLICATION_BITS = {"WB": WB, "IP": IP, "IF": IF, "FC": FC}

# Greek letters, to their English names. The set is the whole alphabet rather than
# the eight that happen to appear in this snapshot -- a normaliser that handles the
# letters in today's data and drops tomorrow's is the kind of thing that fails
# quietly a year later. Final sigma folds onto sigma.
_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "ο": "omicron",
    "π": "pi", "ρ": "rho", "σ": "sigma", "ς": "sigma", "τ": "tau",
    "υ": "upsilon", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
}
_GREEK_RE = re.compile("|".join(map(re.escape, _GREEK)))

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_title(title):
    """A paper title reduced to the form both sides hash.

    Mirrored by ``browser-extension/src/paper.js::normaliseTitle``; the two are
    pinned against ``core/data/title_normalisation_vectors.json``. Change one and
    the vectors fail until you change the other.

    The order is the whole of it. Case-fold FIRST so ``Δ`` and ``δ`` reach the
    Greek table as one entry, transliterate SECOND while the letters still exist,
    and only then strip — a stripper that runs first has already deleted what the
    transliteration was for.
    """
    text = unicodedata.normalize("NFKD", title or "").casefold()
    # Substituted in place, NOT padded with spaces. A publisher writing the
    # letter out types it joined -- `NF-kappaB`, not `NF-kappa B` -- so padding
    # gives `nf kappa b` against their `nf kappab` and the paper is never found.
    # Joined also happens to be right for the hyphenated case (`β-catenin` and
    # `beta-catenin` both land on `beta catenin`), which is the common one.
    text = _GREEK_RE.sub(lambda m: _GREEK[m.group(0)], text)
    return " ".join(_NON_ALNUM.sub(" ", text).split())


def normalise_doi(text):
    """A DOI in the one form both sides key on.

    MIRRORS ``browser-extension/src/paper.js::normaliseDoi``, and pinned by the
    same vector file. DOIs are case-insensitive by specification but publishers
    print them in every case, and they arrive wrapped in whatever the page put
    around them (``doi:``, ``https://doi.org/``), so the stored key and the
    looked-up key have to be reduced identically or the map is a map of near
    misses.
    """
    value = (text or "").strip()
    value = re.sub(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", value, flags=re.I)
    return value.lower() or None


def title_key(title):
    """The lookup key for a paper title: FNV-1a (32-bit) over the normalised form.

    FNV-1a rather than a cryptographic digest for one reason: it is a dozen lines
    of arithmetic that give byte-identical answers in Python and JavaScript
    synchronously. ``crypto.subtle.digest`` is async, which would make the
    extension's paper lookup a promise threaded through the matcher for no gain —
    this is a lookup key, not a security boundary, and nothing is being protected
    from an adversary.

    32 bits over ~29k papers is inside the birthday range (about 0.1 expected
    accidental collisions), and that is safe here ONLY because the builder
    **removes** any key two papers share rather than picking one of them. An
    accidental collision therefore costs coverage on two papers and can never
    serve one paper's citations under another's title. The builder counts what it
    removed and prints it, so the cost is never silent.
    """
    key = 0x811C9DC5
    for byte in normalise_title(title).encode("utf-8"):
        key = ((key ^ byte) * 0x01000193) & 0xFFFFFFFF
    return f"{key:08x}"


@lru_cache(maxsize=1)
def load():
    """The artefact, or ``None`` when there isn't one.

    ``None`` is a real answer and callers must render it as one. The extension
    index is built on a machine that may not carry this file, and an index that
    silently ships without citations looks exactly like an index whose citations
    found nothing — the difference being that the first is our mistake and the
    second is a fact about the literature. ``core/extension_index.py`` states
    which it got.
    """
    try:
        with ARTEFACT.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A corrupt or unreadable artefact is NOT an empty one. Raising here would
        # take down the whole index build for a file that is an enhancement, so the
        # caller gets `None` and says the citation layer is missing -- but this must
        # never become a silent `{}` that reads as "no paper cites anything".
        return None
    if data.get("schema") != SCHEMA:
        return None
    return data


@lru_cache(maxsize=1)
def raw_bytes():
    """The artefact exactly as it sits on disk, or ``None``.

    Served verbatim rather than through ``JsonResponse``: parsing 1.3 MB of JSON
    and re-serialising it on every request buys nothing, and re-serialising is how
    a file that was byte-identical on two deploys stops being. The reader wants
    the bytes; give it the bytes.
    """
    try:
        return ARTEFACT.read_bytes()
    except OSError:
        return None


def summary():
    """What a human needs to know about which snapshot this is.

    Read by the extension install page and by anything that has to say out loud
    how old the citation data is. A snapshot that cannot say what it is a snapshot
    OF is not something to publish a verdict from.
    """
    data = load()
    if data is None:
        return None
    return {"source": data.get("source", {}), "counts": data.get("counts", {}),
            "generated": data.get("generated")}


def unpack(packed, rrids, tokens):
    """One packed record -> ``{rrid: {applications, ambiguous, untested_terms}}``.

    MIRRORS ``browser-extension/src/paper.js::unpack``. The encoding is defined
    once, in ``core/management/commands/build_citation_index.py``, and read here
    and there; a third reader would be a third chance to disagree about what a
    flag means.
    """
    out = {}
    for field in (packed or "").split(","):
        if not field:
            continue
        head, _, token_part = field.partition(".")
        index, _, flag_hex = head.partition(":")
        try:
            rrid = rrids[int(index, 16)]
            flags = int(flag_hex, 16)
        except (ValueError, IndexError):
            continue
        out[rrid] = {
            "applications": [a for a in ("WB", "IP", "IF", "FC")
                             if flags & APPLICATION_BITS[a]],
            "ambiguous": bool(flags & AMBIGUOUS),
            "untested_terms": [tokens[int(t, 16)] for t in token_part.split(".")
                               if t and t.isalnum() and int(t, 16) < len(tokens)],
        }
    return out


# Online-first and print years routinely differ by one. Mirrors
# ``browser-extension/src/paper.js``'s YEAR_SLACK, and for the same reason.
YEAR_SLACK = 1


def paper_for(doi=None, pmid=None, title=None, year=None):
    """Find one paper. ``(status, record)`` where status says WHICH of three.

    ``unavailable`` — no snapshot is deployed, or it could not be read. A fact
                      about us, and it must never render as a fact about the
                      paper.
    ``not_covered``  — the snapshot was searched and does not hold this paper.
                      Also not a claim that the paper cites nothing: CiteAb
                      documents its own gaps, and the join behind the snapshot is
                      on catalogue number because CiteAb holds no RRID.
    ``covered``      — ``record`` is ``{rrid: {...}}`` for this paper.

    Keys are tried most-certain first. A DOI and a PubMed id ARE the paper; a
    title is a string two papers can share, so a title hit is confirmed against
    the year before it is believed. Refusing on disagreement is the only safe
    answer — another paper's applications reported as this paper's is invisible
    to whoever reads it.
    """
    data = load()
    if data is None:
        return "unavailable", None
    attempts = (
        ("by_doi", normalise_doi(doi), False),
        ("by_pmid", (str(pmid).strip() if pmid else None), False),
        ("by_title", (title_key(title) if title else None), True),
    )
    for index, value, confirm in attempts:
        if not value:
            continue
        slot = (data.get(index) or {}).get(value)
        if slot is None:
            continue
        stored = (data.get("years") or [None])[slot] if slot < len(data.get("years") or []) else None
        if confirm and stored and year and abs(int(stored) - int(year)) > YEAR_SLACK:
            continue
        return "covered", unpack(data["papers"][slot], data["rrids"],
                                 data.get("tokens") or [])
    return "not_covered", None


def snapshot_note():
    """One sentence naming which snapshot answered, for a reply to carry."""
    data = load()
    if data is None:
        return None
    source = data.get("source") or {}
    return (f"CiteAb citation record as of {source.get('citeab_data_through')}, "
            f"joined on catalogue number ({data.get('counts', {}).get('papers', 0):,} "
            f"papers). An absence here is an absence in that snapshot, not in the "
            f"literature.")
