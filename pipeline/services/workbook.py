"""Which sheet an uploaded workbook means — and saying so.

Six readers open an uploaded spreadsheet in this app and they find the data three
different ways: by tab name (``dataset``, ``session_import``, ``session_io``), by
searching for the header row (``target_list_io``), and by ``wb.active`` — which
was ``views/imports.py::_file_to_text``, the Upload-a-sheet door on three boards,
and ``services/bench_results.py``, a session's printable bench sheet.

``wb.active`` is **whichever tab was selected when the file was saved.** Add a
``Notes`` tab to a downloaded template, type in it, save with it selected — an
ordinary thing to do — and those two readers return the notes and never see the
data. openpyxl always writes ``active`` as the first sheet, so neither the test
suite nor any field test could reach it: all three write their fixtures with the
same library the app reads them with, and a difference that exists only between
*writers* is invisible to all of them at once. Run 11 had real Excel and
confirmed it on the bytes — ``activeTab="1"`` — and the upload then did nothing
at all, with no row count and no error, because a zero-row result had nothing to
say for itself either.

So, two rules, and this module holds both:

* **Pick the sheet by what is in it**, never by which tab was selected. A caller
  says how to recognise its own header row; the best-scoring sheet wins, ties go
  to the leftmost, and a workbook nothing recognises falls back to the first
  sheet with anything in it — never the active one.
* **Say which sheet was read**, and name the others. A reader that silently picks
  is a reader whose mistake is unfindable; naming it turns the worst version of
  this bug into a line the person can act on.

Bounded, too: Excel records a sheet's dimension in the file and it is frequently
wrong — Carl's workbook declares a million rows and holds 373 — so reading stops
after a long run of blank rows, as ``target_list_io`` already did.
"""
from __future__ import annotations

import csv
import io
import re

# Same bounds, and the same reason, as ``target_list_io._read_grid``.
_MAX_TRAILING_BLANKS = 50
_MAX_ROWS = 100_000
# How far down a sheet to look for its header row.
_HEADER_SCAN = 15


def norm(cell) -> str:
    """A header cell, normalised the way every alias map in this app expects."""
    return re.sub(r"\s+", " ", ("" if cell is None else str(cell)).strip().lower()
                  ).strip(" .:#")


def header_scorer(known):
    """A ``recognise`` for a caller whose header row is a set of column names."""
    names = {norm(k) for k in known}
    return lambda cells: sum(1 for c in cells if norm(c) in names)


class Sheet:
    """The grid that was read, and where it came from.

    ``note`` is empty for the ordinary one-sheet case and a sentence when there
    was a choice to make — which is exactly when a reader needs to be told.
    """

    def __init__(self, rows, name="", others=()):
        self.rows = rows
        self.name = name
        self.others = list(others)

    @property
    def note(self) -> str:
        if not self.others:
            return ""
        return (f"Read from the sheet “{self.name}”. This file also has "
                f"{', '.join(chr(8220) + o + chr(8221) for o in self.others)} — "
                f"nothing was read from "
                f"{'it' if len(self.others) == 1 else 'those'}.")

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)


def _grid(ws):
    rows, blanks = [], 0
    for n, raw in enumerate(ws.iter_rows(values_only=True)):
        cells = list(raw)
        if any(c is not None and str(c).strip() for c in cells):
            blanks = 0
            rows.append(cells)
        else:
            blanks += 1
            if blanks > _MAX_TRAILING_BLANKS:
                break
            rows.append(cells)
        if n > _MAX_ROWS:
            break
    while rows and not any(c is not None and str(c).strip() for c in rows[-1]):
        rows.pop()
    return rows


def read(f, *, recognise=None, prefer=()) -> Sheet:
    """An uploaded .xlsx/.csv/.tsv as a :class:`Sheet`. Never ``wb.active``."""
    name = (getattr(f, "name", "") or "").lower()
    if name.endswith((".csv", ".tsv", ".txt")):
        raw = f.read().decode("utf-8-sig", errors="replace")
        delim = "\t" if name.endswith((".tsv", ".txt")) else ","
        return Sheet([list(r) for r in csv.reader(io.StringIO(raw), delimiter=delim)])

    import openpyxl
    wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
    try:
        grids = [(ws.title, _grid(ws)) for ws in wb.worksheets]
    finally:
        wb.close()

    wanted = {norm(p) for p in prefer}
    best, best_score = None, -1
    for i, (title, rows) in enumerate(grids):
        if not rows:
            continue
        score = 0
        if norm(title) in wanted:
            score += 1000
        if recognise:
            score += max((recognise(r) for r in rows[:_HEADER_SCAN]), default=0)
        # A sheet with data always beats a sheet with none, so a workbook whose
        # headers nothing recognises still reads its first real sheet rather
        # than an empty Notes tab.
        score += 1
        if score > best_score:
            best, best_score = i, score

    if best is None:
        return Sheet([], grids[0][0] if grids else "",
                     [t for t, _ in grids[1:]] if len(grids) > 1 else [])
    title, rows = grids[best]
    return Sheet(rows, title, [t for t, _ in grids if t != title])
