"""What a knockout line's confirmation actually says — the tick *and* the reason.

``CellLine.ko_validated`` is one bit and ``CellLine.ko_validation_notes`` is the
lab's own verdict in words, and the two disagree. The table below counts **54**,
straight off the Access export this app was built from
(``access_csvs/CellLines.csv``, 744 rows) — so it describes the *import*, not the
database. Live it was **46** on 16 Aug 2026 (6 + 40), because the board draws a
disagreement as a question and people have been settling them. Expect it to keep
falling, and re-count before quoting a number at anybody:

======  ==========================================  =========================
count   ``KOconfirmed``                             ``KOInfo``
======  ==========================================  =========================
541     0                                           *(blank)*
102     1                                           Confirmed
48      **0**                                       **Confirmed**
11      1                                           *(blank)*
11      0                                           Failed-Truncated protein
5       0                                           Failed-No difference from WT
5       0                                           Failed-Decreased protein level
**4**   **1**                                       **Failed-Decreased protein level**
4       0                                           Unknown-Absence of protein …
**1**   **1**                                       **Inconclusive-Absence of specific Ab**
**1**   **1**                                       **Failed-Truncated protein**
======  ==========================================  =========================

The board drew the bit alone, as a green pill reading *validated*, with the
words underneath it. So six rows carried a green tick directly above their own
record saying the validation **failed**, and the twelfth field test found one
by opening the board and filtering to *Validated* — SKNFI / ACE, green, over
*Failed-Decreased protein level*. Green does not read as "somebody looked"; it
reads as "the knockout is good", and an unconfirmed knockout makes every result
that used it provisional.

Forty-eight rows had the opposite: the reason says *Confirmed* and the tick is
off, so the screen said **not** validated over a record stating it was.

The rule this module holds is the conservative one, and it is the only one that
does not require guessing which of the two fields is stale:

    **A disagreement between the tick and the reason is never resolved in
    favour of "confirmed".**

So a row whose two halves disagree is ``DISPUTED`` — drawn as a question, not
as a verdict, on a board where one click settles it — and every reader that
*counts* confirmations (``services/gene_progress.py``, ``views/dashboard.py``,
the gene page's cell-line table) counts ``confirmed()``, which a disputed row
fails. That is the same direction the rest of this app leans: a Data Note leaves
``[lysis buffer]`` rather than printing a plausible default, and a report drops
a session with no readings rather than claiming three antibodies were
characterised. Over-claiming is what gets published.

**Reading the note is a prefix test, not an inference.** The lab writes its
verdict first and the explanation after a hyphen — ``Failed-Truncated
protein``, ``Inconclusive-Absence of specific Ab`` — and every one of the 192
non-blank notes on file is in that form. Anything that does *not* open with one
of those words says nothing about the outcome and is left to the tick, for the
same reason ``services/c_number.py`` refuses ``C-RUN11-01`` rather than finding
an 11 in it: finding a word in a string is not reading a verdict.

Nothing here writes. The disputed rows are deliberately untouched — the screens
are right without a write, and correcting live records is a separate decision
with a
backup in front of it (the same call the ``NA`` placeholder targets got).
"""
from __future__ import annotations

from django.db.models import Q

# The five answers a screen can give about a knockout.
CONFIRMED = "confirmed"
FAILED = "failed"
INCONCLUSIVE = "inconclusive"
DISPUTED = "disputed"
UNCHECKED = "unchecked"

# The verdict words the lab writes at the front of a reason, mapped to what they
# mean. `Unknown-…` is the lab's word for "we could not tell", which is what
# `Inconclusive-…` says too — two spellings of one outcome, and no reader needs
# to tell them apart.
_VERDICT_WORDS = {
    "confirmed": CONFIRMED,
    "failed": FAILED,
    "inconclusive": INCONCLUSIVE,
    "unknown": INCONCLUSIVE,
}

# The ones that are *not* a confirmation. Named rather than derived so the Q
# builders below and `note_verdict` cannot drift apart.
_NOT_CONFIRMED_WORDS = tuple(w for w, v in _VERDICT_WORDS.items() if v != CONFIRMED)

# What each outcome puts on a badge. `tone` is one of `board.js`'s TONES keys —
# the app's own vocabulary, the same way `status_label` already crosses from the
# server to a pill.
_BADGE = {
    CONFIRMED:    ("KO confirmed", "good"),
    FAILED:       ("KO failed", "bad"),
    INCONCLUSIVE: ("inconclusive", "warn"),
    DISPUTED:     ("check this row", "warn"),
    # Never the bare word "validated": greyed or not, a column of rows all
    # reading "validated" scans as a column of validated rows, and the grey is
    # the only thing carrying the negative.
    UNCHECKED:    ("not confirmed", "flat"),
}

# Why the two halves disagree, said in the direction it disagrees — a reader
# needs to know which of the two to fix, and "these disagree" does not say.
_DISPUTE = {
    True: "ticked as confirmed, but the reason recorded is not a confirmation.",
    False: "the reason recorded says confirmed, but the box is not ticked.",
}


def note_verdict(notes) -> str | None:
    """The outcome a reason *states*, or ``None`` if it states none.

    A prefix test on the lab's own verdict words. Free text that opens with
    anything else — a scientist describing a blot — is not a verdict and is not
    read as one.
    """
    first = (notes or "").strip().lower()
    if not first:
        return None
    # `Failed-Truncated protein` and `Failed - truncated` alike: the word ends
    # at the first character that is not part of it.
    word = ""
    for ch in first:
        if not ch.isalpha():
            break
        word += ch
    return _VERDICT_WORDS.get(word)


def outcome(line) -> str:
    """What this line's knockout confirmation says, tick and reason together."""
    ticked = bool(getattr(line, "ko_validated", False))
    stated = note_verdict(getattr(line, "ko_validation_notes", ""))
    if stated is None:
        return CONFIRMED if ticked else UNCHECKED
    if stated == CONFIRMED:
        return CONFIRMED if ticked else DISPUTED
    # The reason states a non-confirmation. Ticked, that is the disagreement
    # that put a green pill over "Failed-Decreased protein level"; unticked, the
    # two agree and the reason is simply more specific than the box.
    return DISPUTED if ticked else stated


def confirmed(line) -> bool:
    """Whether this knockout counts as confirmed — for anything that *counts*."""
    return outcome(line) == CONFIRMED


def disagrees(line) -> bool:
    return outcome(line) == DISPUTED


def badge(line) -> dict:
    """``{outcome, label, tone, note}`` — one reader for every screen that draws
    a knockout's confirmation, so the board's pill and the gene page's tick
    cannot say different things about one row."""
    verdict = outcome(line)
    label, tone = _BADGE[verdict]
    return {
        "outcome": verdict,
        "label": label,
        "tone": tone,
        "note": (_DISPUTE[bool(getattr(line, "ko_validated", False))]
                 if verdict == DISPUTED else ""),
    }


# ── The same rule, as a queryset filter ──────────────────────────────────────
#
# The board's KO filter and the board's KO badge must agree, or filtering to
# "Confirmed" puts a row reading *check this row* at the top of the list — which
# is exactly how the twelfth field test found this.

def _stated(field: str, words) -> Q:
    q = Q()
    for word in words:
        q |= Q(**{f"{field}__istartswith": word})
    return q


def confirmed_q(field: str = "ko_validation_notes") -> Q:
    """Lines whose knockout is confirmed and whose reason does not say otherwise."""
    return Q(ko_validated=True) & ~_stated(field, _NOT_CONFIRMED_WORDS)


def disputed_q(field: str = "ko_validation_notes") -> Q:
    """Lines where the tick and the reason disagree, in either direction."""
    return ((Q(ko_validated=True) & _stated(field, _NOT_CONFIRMED_WORDS))
            | (Q(ko_validated=False) & _stated(field, ("confirmed",))))
