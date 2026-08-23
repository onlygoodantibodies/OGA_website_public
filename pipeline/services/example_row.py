"""The example row on a template, and the one reader that discards it.

Every blank spreadsheet this app hands out carries a filled-in example under the
headings, because a column list alone does not say what `parent` or `c number`
wants. The example was written in grey italics and nothing else, which asks the
reader to infer from the *formatting* that this row is not data — and the
personal review said plainly that it is not clear you are meant to type over it.

So the row says what it is: its first cell begins ``e.g.``. That is a fact a
person can read and a fact a parser can act on, which is the point — the screen
and the importer now agree about which row is an example, instead of the screen
implying it and the importer trusting the reader to delete it.

One reader, called by every paste parser, because the alternative is four
implementations of "is this the example?" and one of them being wrong is a
record created from a template's own sample data.
"""
from __future__ import annotations

MARK = "e.g."


def mark(value) -> str:
    """The example's first cell, labelled. ``HAP1`` → ``e.g. HAP1``."""
    text = str(value if value is not None else "").strip()
    return f"{MARK} {text}".strip()


def unmark(value) -> str:
    """``e.g. HAP1`` → ``HAP1`` — for showing the example without its label."""
    text = str(value if value is not None else "").strip()
    if text.lower().startswith(MARK):
        return text[len(MARK):].strip()
    return text


def is_example(cells) -> bool:
    """True when this row is a template's example rather than somebody's data.

    Judged on the first cell that has anything in it, so a template whose first
    column is legitimately blank in the example is still recognised.
    """
    for cell in cells or ():
        text = str(cell if cell is not None else "").strip()
        if not text:
            continue
        return text.lower().startswith(MARK)
    return False


def drop(text: str) -> str:
    """The same thing for pasted text: every line that is an example row, gone.

    Applied before the header sniff rather than after, so a paste of a
    downloaded template — headings, example, then real rows — reads its own
    first data row as row 1.
    """
    lines = (text or "").splitlines()
    kept = [ln for ln in lines if not is_example(_cells(ln))]
    return "\n".join(kept)


def _cells(line: str):
    """Split a pasted line the way the parsers do — tab, then comma, then runs
    of spaces. Only the first non-empty cell is ever looked at, so this is
    deliberately the loosest of the three splits rather than a fifth copy of
    anybody's delimiter logic."""
    if "\t" in line:
        return line.split("\t")
    if "," in line:
        return line.split(",")
    return line.split()
