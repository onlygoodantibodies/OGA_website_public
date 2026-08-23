"""Freeze-down C-numbers: one reader, and it never invents a number.

``CellLine.c_number`` and ``CellLineVial.c_number`` are ``IntegerField``s — the
lab writes ``C-631`` on the tube and the digits are what is stored. So a write
path has to turn the label into that integer, and the one thing it must never do
is *find* an integer in a string that is not a C-number at all.

That is what it did. ``bulk_cell_lines`` pulled the first digit run out of the
cell with ``re.search(r"\\d+")``, so the eleventh field test typed
``C-RUN11-01`` and ``C-RUN11-02`` into the **c number** column, got a preview
that called both rows ``new`` and said nothing about the value, and saved two
different knockouts as **C-11** — indistinguishable from McGill's real C-11, and
reachable only by noticing ``[C-11]`` on a later screen and going back to look.
Same shape as the concentration bug and the opposite outcome from the grid,
which has refused a non-numeric cell by name since it was written: the rule was
enforced in the edit path and not in the write path.

So: convert what we understand, and refuse what we do not, by name. Both paths
call this, so the grid and the paste box cannot disagree about what a C-number
is.

**The parsing itself now lives in ``services/lab_numbers.py``**, because an
antibody's A-number is the same convention one letter over and the two were
about to be written twice. This module keeps its name and its callers: six of
them and a rule in CLAUDE.md say ``services/c_number.py`` is the one reader for
a C-number, and that is still true — it is where a cell line's half of that
module is spelled.
"""
from __future__ import annotations

from pipeline.services import lab_numbers

KIND = lab_numbers.CELL_LINE

ACCEPTED = lab_numbers.spec(KIND).accepted


def parse(raw, *, field="c number") -> tuple[int | None, str]:
    """``(number, error)``.

    ``(None, "")`` for a blank cell — blank means "not written down", which is
    never a reason to refuse. ``(None, message)`` when the cell says something
    this cannot turn into a C-number; the message quotes what was typed and says
    what is accepted, because "not a number" is wrong about the case that
    matters — ``C-RUN11-01`` *contains* a number. Otherwise ``(int, "")``.
    """
    return lab_numbers.parse(raw, kind=KIND, field=field)


def label(value) -> str:
    """``C-631`` for 631 — the way it is written on the tube, and blank for None."""
    return lab_numbers.label(value, kind=KIND)
