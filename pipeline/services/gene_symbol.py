"""How a human gene symbol is spelled — the one reader for its case.

Eight of the 584 targets on file are typed in the wrong case: ``DnaJC18``,
``Kif5a``, ``PTK2b``, ``Rab3C``, ``Rab40AL``, ``Rab44``, ``Rab45`` and
``Rab46`` (twentieth field test, 5 Aug 2026). It is cosmetic on a screen and
not cosmetic anywhere else — a supplier list, a UniProt query or anybody
matching our export against their own reads ``Rab44`` as a different organism's
gene, because mouse symbols *are* written that way and human ones are not.

**Uppercase is the rule and ``orf`` is the exception**, so a bare ``.upper()``
is wrong in the other direction. HGNC writes an open-reading-frame symbol
``C9orf72`` — chromosome, lowercase ``orf``, number — and three targets on file
are correctly spelled that way: ``C9orf72``, ``C9orf16``, ``C14orf119``. That
is the whole reason this is a module rather than one call to ``.upper()``
inline: the app was already uppercasing gene symbols in five places, so a
``C9orf72`` added through UniProt today is stored as ``C9ORF72`` — the same
defect as the eight, arriving through the write path instead of through the
import, and it would be *created* by any naive fix to them.

The pipeline is human-only (``uniprot.lookup_gene`` searches human entries and
feasibility refuses anything else by name), so there is one convention to hold
rather than a per-organism question.

What this deliberately does **not** do is rename anything. ``Rab45`` is an
older name for ``RASEF`` and ``Rab46`` for ``RABGEF``-adjacent entries; which
symbol a target *should* carry is a curation decision with UniProt behind it
(``enrich_targets_from_uniprot``), not a string transform. This changes case
and nothing else, so ``canonical(s).upper() == s.upper()`` always holds — there
is a test asserting exactly that, because a "tidy the symbols" helper that can
also change the letters is one nobody can review by eye.
"""
from __future__ import annotations

import re

# C9orf72, C14orf119, CXorf38 — chromosome (a number, X or Y), lowercase "orf",
# then the number. Matched against the already-uppercased symbol.
_ORF = re.compile(r"^C(\d{1,2}|X|Y)ORF(\d+)$")


def canonical(symbol) -> str:
    """The HGNC spelling of a human gene symbol's case.

    Returns the input stripped and uppercased, with ``orf`` restored to
    lowercase. Never changes which letters are there — only their case — and
    hands back anything blank unchanged.

    Not a resolver: it knows nothing about synonyms, and ``NA`` (the app's word
    for "this row has no gene") is left exactly as it arrived, because
    ``targets.is_not_applicable`` is what reads that and it is case-insensitive
    already.
    """
    text = (symbol or "").strip()
    if not text:
        return text
    upper = text.upper()
    match = _ORF.match(upper)
    if match:
        return f"C{match.group(1)}orf{match.group(2)}"
    return upper


def is_canonical(symbol) -> bool:
    """True when a symbol is already spelled the way :func:`canonical` spells it."""
    text = (symbol or "").strip()
    return bool(text) and text == canonical(text)


def miscased(symbols):
    """``[(as_stored, corrected)]`` for every symbol whose case is wrong.

    Takes an iterable of strings. Blanks are dropped rather than reported: a
    target with no gene is a legitimate record, not a spelling to fix.
    """
    out = []
    for symbol in symbols:
        text = (symbol or "").strip()
        if not text:
            continue
        fixed = canonical(text)
        if fixed != text:
            out.append((text, fixed))
    return out
