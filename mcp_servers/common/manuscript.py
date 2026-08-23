"""Identifier RESOLUTION helpers for the manuscript tools.

This module used to hold the server-side text parsing: regex catalogue/RRID
extraction, supplier-proximity gating, gene-symbol matching. All of it is gone.

Why: every defect ever found in this connector lived in that parsing, and none in
the database layer. It missed catalogue numbers whose shape it did not recognise
(a whole tissue-expression claim rested on one it never saw), it could not tell an
isotype control from a primary antibody, it truncated multi-word target names at
the first token, and it read "Kd" as "KD". A model reading the manuscript makes
none of those mistakes, and — unlike a regex — can say what a reagent IS. So the
caller now reads the paper and passes what it found; the server resolves it
against the knockout-controlled dataset, which is the part only the server can do.

What remains here is resolution, not parsing: given an identifier, which forms
should be tried against the database.
"""
from __future__ import annotations

import re
from typing import List

# A Cell Signaling-style catalogue with a pack-size suffix (78896S, 2642T). OGA
# stores CST catalogues WITHOUT the size letter.
_CST_SUFFIXED = re.compile(r"^(\d{4,6})[STP]$")

# --- Typesetting a catalogue number picks up on the way to the page. ----------
#
# The caller is instructed to pass the identifier EXACTLY as printed, and that
# instruction is right: a model that tidies an identifier before sending it is a
# model quietly inventing one. So the tidying belongs here.
#
# It has to, because the printed form is often not the stored form. BMJ sets
# Proteintech's 14060-1-AP as `14 060-1-AP`, using a space as a thousands
# separator; Springer sets it `14,060–1-AP`, with a comma and an en-dash where the
# number has a hyphen; two publishers print Abcam catalogues as `#ab74140`. Five of
# 59 antibodies in the benchmark were reported ABSENT from the dataset when the
# dataset held a not-recommended result for them, on nothing but this.
#
# That failure is worse here than in the extension, which shows nothing when it
# cannot match. This server states `oga_tested: "no"`, and its own note tells the
# reader that absence is not evidence about quality — so a reader was actively
# directed to treat as unknown an antibody with a documented failure.
#
# The rules are deliberately the SAME ones `browser-extension/src/matcher.js`
# applies (``DASH_CLASS``, ``ZERO_WIDTH``, ``GROUP_SEP``). One printed string must
# not resolve in the extension and come back absent from the server.

#: Every dash a typesetter might put where the catalogue number has a hyphen.
_DASH_RE = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")

#: Invisible characters, which mean nothing wherever they turn up.
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff\u00ad]")

#: Digit-group separators, stripped only where one is doing a thousands
#: separator's job: a digit on the left, exactly three digits on the right, and no
#: fourth digit after them. A full stop is deliberately NOT in the class — between
#: digits it is nearly always a decimal point.
_GROUP_SEP_RE = re.compile(
    "(?<=\\d)[,\u0020\u00a0\u2009\u200a\u202f](?=\\d{3}(?!\\d))")

#: A catalogue number printed with a leading hash: `#ab74140`, `cat #2642`.
_LEADING_HASH = re.compile(r"^#\s*")

# --- The LABEL a paper wraps around the number, and the supplier in front of it.
#
# The caller is told to pass the identifier exactly as printed, and papers print
# the number inside a phrase: `cat. no. 14060-1-AP`, `Cat. No. MAB6360`,
# `catalogue #PA1-914`, `Proteintech #66140-1-Ig`, `Affinity BioReagents, PA1-914`.
# Eight benchmark antibodies that ARE in the dataset came back `not_in_dataset` on
# nothing but this — and this server states `oga_tested: "no"` while its own note
# tells the reader absence is not evidence about quality, so a false miss is a
# silent, uncorrectable error in the reader's head.
#
# Deliberately NOT mirrored in ``browser-extension/src/matcher.js``: the extension
# tokenises a page, so it never receives a phrase to peel — `Proteintech` and
# `#66140-1-Ig` reach it as separate tokens, and its ``GENE_THEN_CAT_RE`` already
# reads a `cat#` prefix. The sync rule is about the string that ends up being
# LOOKED UP, and on that the two still agree. ``collapse_identifier`` below IS
# mirrored, because that one is about the number itself.

#: Words a paper puts in front of a catalogue number. Never part of one.
_LABEL_WORD = re.compile(
    r"^(?:cat(?:alogue|alog)?|prod(?:uct)?|item|part|order|ref|no|nos|num|number)"
    r"\b\.?\s*", re.I)

#: What separates a supplier's name from its own catalogue number.
_VENDOR_SPLIT = re.compile(r"\s*[,;]\s*|\s+")

#: Everything that is not a letter or a digit — the last-resort comparison.
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

#: Below this, an alphanumerics-only form is too easily another supplier's
#: catalogue number. Catalogue numbers are not unique across suppliers and this
#: layer has never disambiguated by supplier, so the short ones stay out of the
#: punctuation-insensitive pass entirely.
MIN_COLLAPSED = 5

#: Novus prints a sample size as a trailing `SS` (``NBP2-24630SS``); OGA stores the
#: bare number. Same shape as the Cell Signaling rule above.
_NOVUS_SIZED = re.compile(r"^(NBP?\d*-?\d+)SS$", re.I)


def normalise_identifier(identifier: str) -> str:
    """Strip printing conventions from an identifier, for LOOKUP only.

    Never store or echo the result: what the paper printed is what the reader will
    search for, and a reply that silently renames their reagent is harder to check
    than one that does not match.
    """
    text = (identifier or "").strip()
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _LEADING_HASH.sub("", text)
    text = _GROUP_SEP_RE.sub("", text)
    return _DASH_RE.sub("-", text).strip()


def _strip_labels(text: str) -> str:
    """Peel ``cat.`` / ``no.`` / ``#`` off the front, one at a time.

    A strip is only accepted when what is left still holds a digit AND starts on
    an alphanumeric, because `no` is also how a catalogue number can start:
    ``NO-1234`` would otherwise be filed as ``-1234``.
    """
    changed = True
    while changed and text:
        changed = False
        for rx in (_LEADING_HASH, _LABEL_WORD):
            m = rx.match(text)
            if not m:
                continue
            rest = text[m.end():].strip()
            if not rest or not rest[0].isalnum():
                continue
            if not any(ch.isdigit() for ch in rest):
                continue
            text, changed = rest, True
    return text


def _strip_vendor(text: str) -> str:
    """Drop leading words that carry no digit — the supplier in front of its own
    number. ``Affinity BioReagents, PA1-914`` -> ``PA1-914``.

    Runs AFTER ``normalise_identifier``, which has already closed up a digit-group
    space, so Synaptic Systems' house-style ``107 102`` is one token by the time it
    gets here and keeps both halves.
    """
    parts = [p for p in _VENDOR_SPLIT.split(text) if p]
    while len(parts) > 1 and not any(ch.isdigit() for ch in parts[0]):
        parts.pop(0)
    return " ".join(parts)


def strip_printing(identifier: str) -> str:
    """``normalise_identifier`` plus the label and the supplier's name."""
    text = normalise_identifier(identifier)
    prev = None
    while prev != text and text:
        prev = text
        text = _strip_vendor(_strip_labels(text))
    return text


def collapse_identifier(identifier: str) -> str:
    """Alphanumerics only — the form that survives a publisher MOVING punctuation.

    ``MA5-11154`` is printed as ``MA511154`` and ``11820-1-AP`` as ``11820-1AP``;
    neither is derivable by cleaning one side, because the missing hyphen cannot be
    put back. So both sides collapse to alphanumerics and are compared there — the
    stored side via ``portal._typeset_tolerant``'s ``_cat_collapsed``.

    Returns ``""`` below ``MIN_COLLAPSED`` characters, which is what keeps this
    from becoming a way to match any short number against any other.

    Mirrored in ``browser-extension/src/matcher.js::collapseIdentifier``.
    """
    collapsed = _NON_ALNUM.sub("", identifier or "")
    return collapsed if len(collapsed) >= MIN_COLLAPSED else ""


def resolution_variants(identifier: str) -> List[str]:
    """Forms to try when matching an identifier against the DB.

    In order: the identifier exactly as given; the same with publisher typesetting
    normalised away; the same again with the label and the supplier's name peeled
    off the front (``cat. no. 14060-1-AP`` -> ``14060-1-AP``); and — for a number
    carrying a pack-size suffix — the bare number, because OGA stores Cell
    Signaling catalogues without the size letter (``64196S`` -> ``64196``) and
    Novus without the sample-size ``SS`` (``NBP2-24630SS`` -> ``NBP2-24630``).

    The punctuation-insensitive form is NOT here: ``collapse_identifier`` is
    compared against a separately collapsed database column, not against the
    stored string, so it is not a form you can look up by equality.

    **As-given is always first, so every later form can only ADD a match, never
    override a real one.** That is what makes the list safe to extend: a stored
    catalogue that genuinely contains one of these characters still wins on its own
    exact form before any normalised variant is tried.
    """
    identifier = (identifier or "").strip()
    forms = [identifier] if identifier else []

    for extra in (normalise_identifier(identifier), strip_printing(identifier)):
        if extra and extra not in forms:
            forms.append(extra)

    # Applied to every form: a CST number can be printed with a hash, a supplier's
    # name or an odd dash just as readily as any other.
    for form in list(forms):
        for rx in (_CST_SUFFIXED, _NOVUS_SIZED):
            m = rx.match(form)
            if m and m.group(1) not in forms:
                forms.append(m.group(1))
    return forms
