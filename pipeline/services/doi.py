"""A published record's DOI: one reader, and it never invents a link.

``Report.zenodo_doi`` and ``Report.f1000_doi`` are ``URLField``s, and a URLField
validates **nothing** on ``save()`` — validators run in ``full_clean()``, which no
write path here calls. So the column accepts any string at all, and the board's
patch endpoint put whatever was typed straight into it.

The thirteenth field test typed ``definitely not a doi 12345`` into the Zenodo
cell to see what would happen. All of it happened: the value saved, the gene
flipped to **completed** (``target_board.completed_report_q`` reads
``zenodo_doi__gt=""`` and asks nothing about what is in there), the gene page
grew a green **Reported** badge, and the board turned the text into an ``<a
href>`` — which, not being an absolute address, resolved against our own site and
landed on a Not Found page. The cell then stopped being editable, because the
board drew a link *instead of* an editable cell once the field had anything in
it, so there was no way to correct or clear it from any screen in the app.

So: convert what we understand, and refuse what we do not, by name — the same
shape as ``services/concentration.py`` and ``services/c_number.py``. Three
readers, and the second and third exist because of what is already on file:

``parse``    what a typed cell is allowed to store, and the canonical form of it.
``link``     the ``href``, and **empty unless the value is an absolute address**.
             A bare ``10.5281/…`` was already reachable through Carl's workbook
             (``target_list_io._is_url`` accepted a bare DOI and stored it
             unchanged), so rows predating this can hold something no browser can
             follow. A value that cannot be linked is drawn as text.
``display``  what the cell prints — and it is always a value ``parse`` accepts,
             so what the board shows is a thing you can retype into it. That is
             the rule ``cell_lines.wild_type_options`` learned one board over:
             offering the app's own rendering back to a parser that refuses it is
             how a cell comes to reject its own contents.

Nothing here refuses a **blank**: on this field blank is how a typo is undone,
and the board's patch endpoint clears the column when it arrives empty. That is
the cell-edit path only. The fill-only-blank rule that governs the bulk importers
is untouched — a blank cell in a spreadsheet still clears nothing.
"""
from __future__ import annotations

import re

# ``10.`` + registrant + ``/`` + suffix. The suffix is deliberately loose (DOIs
# may contain almost anything) but may not contain whitespace, which is what
# separates a DOI from a sentence with a number in it.
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")

# A pasted address with the scheme left off — browsers hide ``https://`` in the
# bar, so ``zenodo.org/records/16812915`` is an ordinary thing to paste. A dotted
# host followed by a path, and no whitespace anywhere.
_BARE_HOST = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*$", re.I)

# Prefixes people paste in front of a DOI. Order matters: the longest first.
_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
             "http://dx.doi.org/", "doi.org/", "dx.doi.org/", "doi:", "doi ")

EXAMPLE = "10.5281/zenodo.16812915"

ACCEPTED = (f"the DOI ({EXAMPLE}) or the full address of the record "
            f"(https://…)")


def _strip_prefix(text: str) -> str:
    low = text.lower()
    for p in _PREFIXES:
        if low.startswith(p):
            return text[len(p):].strip()
    return text


def parse(raw, *, field="DOI") -> tuple[str, str]:
    """``(url, error)``.

    ``("", "")`` for a blank cell — on this field blank is how a wrong value is
    taken back out, so it is never a refusal. ``("", message)`` when the cell says
    something that is neither a DOI nor an address; the message quotes what was
    typed and says what is accepted, because "that is wrong" without "here is what
    is right" is half a message. Otherwise ``(url, "")`` — always absolute, so
    whatever is stored can be linked.
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""

    if text.lower().startswith(("http://", "https://")):
        # An address is taken as given: Zenodo record URLs, F1000 article URLs and
        # doi.org links are all legitimate here and we are not in a position to
        # judge a host.
        rest = text.split("://", 1)[1]
        if not rest or " " in text:
            return "", _refusal(text, field)
        return text, ""

    naked = _strip_prefix(text)
    if _DOI.match(naked):
        return f"https://doi.org/{naked}", ""
    if _BARE_HOST.match(text):
        return f"https://{text}", ""
    return "", _refusal(text, field)


def _refusal(text: str, field: str) -> str:
    return f"'{text}' is not a {field} — give {ACCEPTED}."


def link(value) -> str:
    """The ``href`` for a stored value, or ``""`` when it cannot be one.

    A relative ``href`` is the harmful half of storing free text in a URL column:
    the browser resolves it against this site, so the link works, goes nowhere
    useful, and reports a Not Found page that reads as the record being missing.
    """
    text = str(value or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else ""


def display(value) -> str:
    """What a cell prints — short, and always something ``parse`` accepts back.

    ``https://doi.org/10.5281/zenodo.1`` prints as ``10.5281/zenodo.1``, which is
    what is written in a paper and what somebody would type. An ``http://`` link
    keeps its scheme: it is worth seeing, and dropping it would make the printed
    value parse back to a *different* (https) address.
    """
    text = str(value or "").strip()
    low = text.lower()
    for p in ("https://doi.org/", "https://dx.doi.org/"):
        if low.startswith(p):
            return text[len(p):]
    if low.startswith("https://"):
        return text[len("https://"):]
    return text
