"""When a reagent arrived — and how precisely anybody actually knows.

``Antibody.received_date`` is a ``DateField``, and a ``DateField`` has no way to
say *"August 2026"*. That is a real answer, not a sloppy one: a packing slip
carries a day, an order confirmation carries a month, and a vial that turned up
in a box of ten from the spring order carries a year and a shrug. uOttawa's
first batch of records reached us written as ``August 2026`` on every row.

Two ways to hold that were considered and rejected:

* **Store the first of the month.** The column would then say ``2026-08-01``,
  every screen would print *1 Aug 2026*, and a day nobody wrote down would be
  published as a fact. This file's whole neighbourhood exists because of that
  failure mode — see ``report_generator._recorded``, which prints
  ``[lysis buffer]`` rather than a plausible "RIPA".
* **A second, free-text column.** Then two columns answer "when did this
  arrive?", they disagree the moment somebody edits one, and the reader sees the
  disagreement rather than the better answer.

So: **one date column, plus a precision beside it**, and one reader that every
surface asks. The date is stored at the start of whatever period is known —
``2026-08-01`` for *August 2026* — and **nothing ever prints the parts the
precision says are not known.** The day is storage, not a claim.

``day`` is the default and is right for every row already on file: all 2,841
antibodies carrying a ``received_date`` on live (1 Sep 2026) came from the
Access import with real dates — 31 distinct days of the month, only 33 of them
the 1st — so the column that arrives with this change is true of the data that
predates it without a backfill.

**An ambiguous numeric date is refused, not guessed.** ``01/02/2026`` is
1 February to the two benches that wrote this and 2 January to a third, and
there is no way to tell from the cell which was meant. Where the day is above 12
it is unambiguous and read day-first; where it is not, this refuses by name and
gives two spellings that cannot be misread. A wrong date on a reagent is the
quiet kind of wrong: nothing on any screen contradicts it, and it turns up years
later in a methods section.
"""
from __future__ import annotations

import re
from datetime import date

DAY = "day"
MONTH = "month"
YEAR = "year"

#: What ``Antibody.received_precision`` may hold. The model's own choices are
#: built from this, so the column and this reader cannot drift.
CHOICES = ((DAY, "Day"), (MONTH, "Month"), (YEAR, "Year"))

#: The oldest and newest a reagent's arrival can plausibly be. A year outside
#: this is a typo or another column's value, and reading it as a date files a
#: reagent as having arrived in 1998.
_MIN_YEAR, _MAX_YEAR = 1990, 2100

ACCEPTED = ("2026-08-14, 14 Aug 2026, Aug 2026 or 2026 — a day, a month or a "
            "year, whichever you actually know")

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_MONTHS["sept"] = 9

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# `2026-08-14`, and the `2026-08-14 00:00:00` an .xlsx date cell arrives as once
# `views/imports.py::_file_to_text` has called `str()` on it. The time is
# dropped rather than refused: openpyxl hands back a `datetime` for every
# date-formatted cell, so refusing it would refuse the ordinary spreadsheet.
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")
_ISO_MONTH = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_YEAR = re.compile(r"^(\d{4})$")
_NUMERIC = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")
_MONTH_YEAR = re.compile(r"^([a-z]+)[\s,]+(\d{4})$")
_YEAR_MONTH = re.compile(r"^(\d{4})[\s,]+([a-z]+)$")
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s,-]+([a-z]+)[\s,-]+(\d{4})$")
_MONTH_DAY_YEAR = re.compile(r"^([a-z]+)[\s,-]+(\d{1,2})(?:st|nd|rd|th)?[\s,-]+(\d{4})$")


def _month_number(word: str) -> int | None:
    return _MONTHS.get((word or "").strip().lower()[:4]) or \
        _MONTHS.get((word or "").strip().lower()[:3])


def _refusal(text: str, why: str = "") -> tuple[None, str, str]:
    detail = f" {why}" if why else ""
    return None, "", (f"'{text}' is not a date this can read.{detail} "
                      f"Give it as {ACCEPTED}.")


def _build(year, month, day, precision, text):
    """A real ``date`` for the start of the period, or a refusal naming why."""
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        return _refusal(text, f"The year {year} is outside "
                              f"{_MIN_YEAR}–{_MAX_YEAR}.")
    try:
        return date(year, month, day), precision, ""
    except ValueError:
        return _refusal(text, "There is no such day.")


def parse(raw) -> tuple[date | None, str, str]:
    """``(date, precision, error)`` for one received-date cell.

    ``(None, "", "")`` for a blank cell — blank means "not written down", which
    is never a reason to refuse. ``(None, "", message)`` when the cell says
    something this cannot turn into a date, quoting what was typed and naming
    what is accepted. Otherwise a ``date`` at the **start** of the period known,
    with the precision that says how much of it to believe.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None, "", ""
    low = text.lower().strip(".")

    m = _ISO.match(low)
    if m:
        return _build(int(m.group(1)), int(m.group(2)), int(m.group(3)), DAY, text)

    m = _ISO_MONTH.match(low)
    if m:
        month = int(m.group(2))
        if not 1 <= month <= 12:
            return _refusal(text, f"There is no month {month}.")
        return _build(int(m.group(1)), month, 1, MONTH, text)

    m = _YEAR.match(low)
    if m:
        return _build(int(m.group(1)), 1, 1, YEAR, text)

    m = _DAY_MONTH_YEAR.match(low)
    if m:
        month = _month_number(m.group(2))
        if month:
            return _build(int(m.group(3)), month, int(m.group(1)), DAY, text)

    m = _MONTH_DAY_YEAR.match(low)
    if m:
        month = _month_number(m.group(1))
        if month:
            return _build(int(m.group(3)), month, int(m.group(2)), DAY, text)

    m = _MONTH_YEAR.match(low)
    if m:
        month = _month_number(m.group(1))
        if month:
            return _build(int(m.group(2)), month, 1, MONTH, text)

    m = _YEAR_MONTH.match(low)
    if m:
        month = _month_number(m.group(2))
        if month:
            return _build(int(m.group(1)), month, 1, MONTH, text)

    # `14/08/2026` — day-first, and only where the first number cannot be a
    # month. `01/02/2026` is two different days depending on which side of the
    # Atlantic wrote it and there is nothing in the cell to settle it, so it is
    # refused rather than guessed: this column ends up in a methods section.
    m = _NUMERIC.match(low)
    if m:
        first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if first > 12 and second <= 12:
            return _build(year, second, first, DAY, text)
        if second > 12 and first <= 12:
            return _build(year, first, second, DAY, text)
        if first <= 12 and second <= 12:
            return _refusal(
                text, f"{first}/{second} could be {first} "
                      f"{_MONTH_NAMES[second - 1]} or {second} "
                      f"{_MONTH_NAMES[first - 1]} and nothing in the cell says "
                      f"which.")
        return _refusal(text, "Neither number can be a month.")

    return _refusal(text)


def label(received, precision: str = DAY) -> str:
    """What a screen prints: ``14 Aug 2026``, ``Aug 2026``, ``2026`` or ``""``.

    Never more precise than the precision allows — that is the whole point of
    the column. A row whose precision is ``month`` prints no day, so the
    ``2026-08-01`` underneath it is never read as the first of the month.
    """
    if not received:
        return ""
    if precision == YEAR:
        return str(received.year)
    if precision == MONTH:
        return f"{_MONTH_NAMES[received.month - 1]} {received.year}"
    return f"{received.day} {_MONTH_NAMES[received.month - 1]} {received.year}"


def stored(received, precision: str = DAY) -> str:
    """What a sheet and an editable cell carry: ``2026-08-14``, ``2026-08``,
    ``2026`` or ``""``.

    ``parse`` accepts every one of these back unchanged, so the download →
    edit → upload round trip does not quietly promote a month to a day. That is
    why this is not ``label``: *14 Aug 2026* reads back fine, but a sheet full
    of them sorts alphabetically and a spreadsheet will not treat them as dates.
    """
    if not received:
        return ""
    if precision == YEAR:
        return f"{received.year:04d}"
    if precision == MONTH:
        return f"{received.year:04d}-{received.month:02d}"
    return received.isoformat()


def of(obj) -> str:
    """The printed date for a record carrying the pair. One call, so no screen
    has to remember to read the precision beside the date."""
    return label(getattr(obj, "received_date", None),
                 getattr(obj, "received_precision", DAY) or DAY)


def cell_value(obj) -> str:
    """The round-trip spelling for a record carrying the pair."""
    return stored(getattr(obj, "received_date", None),
                  getattr(obj, "received_precision", DAY) or DAY)
