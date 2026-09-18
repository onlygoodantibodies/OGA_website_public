"""Where a vial actually is — the freezer, the box, the slot in the box.

``InventoryLocation`` has held this since the first migration and 3,058 of the
antibodies on live carry one, filled by the Access import: 107 distinct −20 °C
boxes at McGill plus its 4 °C and −80 shelves (read 1 Sep 2026). **None of it
was drawn on any screen or carried in any sheet.** uOttawa asked for "a box
number field", which is the same field, and it was already there and invisible —
the shape CLAUDE.md calls *a field a write path fills must be drawn on some
screen*, one step worse, because here the write path was a one-off import and
nothing has been able to read or edit it since.

This is the one reader for all of it, and it exists because the fact was already
being computed in two places for cell lines and about to be in two more for
antibodies.

Three things it settles:

* **A record can be in more than one place, and that is not a mistake.** 134
  antibodies on live have two locations — a −20 °C working aliquot and a −80 °C
  backup is the ordinary reason. So every reader takes the list, and a surface
  that can only show one says so rather than picking. ``one_of`` is what a cell
  edit uses, and it refuses a record with two rather than silently editing the
  first.
* **A storage temperature is read, not guessed.** ``read_type`` takes what
  people actually write — ``-20``, ``(-20°C)``, ``−20`` with a real minus sign,
  ``4C``, ``fridge``, ``LN2`` — and the caller says what an unrecognised value
  means. A **paste** passes ``default=OTHER``, which is what
  ``bulk_cell_lines`` has always done and what keeps a row with an odd storage
  note worth writing; a **typed cell** passes no default and gets a refusal
  naming the temperatures on offer, because somebody typing into one cell can
  fix it now and filing their entry under "Other" tells them nothing.
* **A location that records nothing is deleted, not kept blank.** Same rule as
  ``target_board``'s report row: a row a cell edit created must go when the cell
  is emptied, or the freezer fills with rows saying only that something is
  somewhere.
"""
from __future__ import annotations

from pipeline.models import InventoryLocation

DB = "pipeline_db"

OTHER = InventoryLocation.StorageType.OTHER

#: The hierarchy, coarsest first. One tuple, so a reader, a writer and a
#: "does this row still say anything" test cannot list different fields.
HIER = ("building", "room", "freezer", "shelf", "rack", "box", "position")

#: How each stored code is written on a screen. Not
#: ``get_storage_type_display()``: that answers ``-20°C Freezer``, which is the
#: right words for an admin dropdown and too long for a grid cell beside a box
#: number.
TYPE_LABELS = {
    "4c": "4°C",
    "-20": "−20°C",
    "-80": "−80°C",
    "ln2": "LN₂",
    "rt": "RT",
    OTHER: "other",
}

ACCEPTED_TYPES = "4C, -20, -80, LN2, RT or other"

# Ordered: the first match wins, so `-80` is tested before `-20` can be found
# inside a longer string and `4` never matches the `4` in `-40`. Every key is
# compared against the text with spaces removed and both Unicode minus signs
# folded to ASCII, which is how `(−20 °C)` off a supplier's page arrives.
_TYPE_PATTERNS = (
    (("ln2", "liquidn", "nitrogen"), "ln2"),
    (("-80", "80c", "minus80"), "-80"),
    (("-20", "20c", "minus20"), "-20"),
    (("4c", "+4", "fridge", "refrigerat"), "4c"),
    (("rt", "roomtemp", "ambient"), "rt"),
)


def _fold(raw: str) -> str:
    """Lower-case, minus signs folded to ASCII, whitespace and degree signs out.

    ``(−20 °C)`` and ``-20C`` are the same shelf. The Unicode minus (U+2212) and
    en dash arrive from anything copied off a supplier's web page, and a fold
    that misses them files the vial under "other" while the screen shows a value
    that looks perfectly readable.
    """
    text = (raw or "").lower()
    for ch in ("−", "–", "—"):
        text = text.replace(ch, "-")
    for ch in (" ", "\t", "°", "(", ")", "[", "]", "°"):
        text = text.replace(ch, "")
    return text


def read_type(raw, *, default: str = "") -> tuple[str, str]:
    """``(code, error)`` for a storage temperature.

    ``("", "")`` for a blank cell. When nothing matches, ``default`` decides:
    a paste passes ``OTHER`` and keeps the row, a typed cell passes nothing and
    gets the refusal. One function rather than a lenient and a strict one, so
    the two surfaces cannot come to disagree about what ``-20`` means.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return "", ""
    folded = _fold(text)
    if folded in ("other", "unknown"):
        return OTHER, ""
    for needles, code in _TYPE_PATTERNS:
        if any(n in folded for n in needles):
            return code, ""
    if default:
        return default, ""
    return "", (f"'{text}' is not a storage temperature — give one of "
                f"{ACCEPTED_TYPES}.")


def type_label(code: str) -> str:
    return TYPE_LABELS.get(code or "", code or "")


def label(loc) -> str:
    """One location as a person reads it: ``−20°C box 42``, ``4°C box 3 A1``.

    The temperature leads because it is the thing that tells you which room to
    walk to, and a box number without it is ambiguous — every freezer has a
    box 3.
    """
    if loc is None:
        return ""
    parts = [type_label(loc.storage_type)]
    if (loc.freezer or "").strip():
        parts.append(f"freezer {loc.freezer.strip()}")
    if (loc.box or "").strip():
        parts.append(f"box {loc.box.strip()}")
    if (loc.position or "").strip():
        parts.append(loc.position.strip())
    return " ".join(p for p in parts if p)


def locations(obj) -> list:
    """Every place this antibody is recorded, oldest row first.

    Reads ``obj.locations.all()`` so a queryset that has prefetched them makes
    no query per row — the board draws 50 at a time and the export draws the
    lot.
    """
    if obj is None or not getattr(obj, "pk", None):
        return []
    return sorted(obj.locations.all(), key=lambda l: l.pk)


def summary(obj) -> str:
    """Every location on one line — ``−20°C box 42 · −80°C box 7``.

    All of them, never the first: 134 antibodies on live are genuinely in two
    places, and a cell showing one of them is a cell that sends somebody to the
    wrong freezer while looking exactly like an answer.
    """
    return " · ".join(p for p in (label(l) for l in locations(obj)) if p)


def boxes(obj) -> str:
    """The box numbers alone, for the column that is only about boxes."""
    return " · ".join(
        (l.box or "").strip() for l in locations(obj) if (l.box or "").strip())


def types(obj) -> str:
    """The temperatures alone."""
    return " · ".join(type_label(l.storage_type) for l in locations(obj))


def one_of(obj):
    """``(location_or_None, error)`` — the single location a cell may edit.

    ``(None, "")`` when there is none yet, which is an edit that *creates* one.
    A record in two places is refused by name: a grid cell has one value, and
    silently editing whichever row came first is how the −80 °C backup ends up
    labelled with the −20 °C box number.
    """
    rows = locations(obj)
    if len(rows) > 1:
        return None, (
            f"This vial is recorded in {len(rows)} places ({summary(obj)}), and "
            f"one cell cannot say which to change. Edit them in Django admin, "
            f"or clear the ones that no longer apply.")
    return (rows[0] if rows else None), ""


def records_nothing(loc) -> bool:
    """True when a location says only that something is somewhere.

    The storage type is not content on its own here: it is required by the
    model and every row has one, so a row whose box, position, freezer and note
    are all empty is the row an emptied cell leaves behind.
    """
    if loc is None:
        return True
    return not any((getattr(loc, f, "") or "").strip() for f in HIER) \
        and not (loc.notes or "").strip()


def _site_refusal(obj) -> str:
    """Why this record cannot have a location yet.

    ``InventoryLocation.site`` is a required FK, so there is nowhere to put a
    box number until the vial belongs to a bench — 32 antibodies on live have
    no site. Named rather than a bare failure: the site cell is two columns
    along and this says to fill it in.
    """
    if getattr(obj, "site_id", None):
        return ""
    return ("This antibody has no site, and a freezer box belongs to a bench. "
            "Set the site first — the column is on this row.")


def set_field(obj, field: str, raw, *, db: str = DB) -> tuple[object, str]:
    """Write one hierarchy field (``box``, ``position``, …). ``(location, error)``.

    Creates the location if there is none, deletes it if the edit leaves it
    recording nothing, and refuses a record that is in two places or has no
    site. The caller saves nothing — this writes, because a location is its own
    row rather than a column on the antibody.
    """
    if field not in HIER:
        return None, f"'{field}' is not a storage field."
    value = str(raw if raw is not None else "").strip()
    loc, err = one_of(obj)
    if err:
        return None, err
    if loc is None:
        if not value:
            return None, ""          # clearing what was never recorded
        refusal = _site_refusal(obj)
        if refusal:
            return None, refusal
        loc = InventoryLocation(antibody=obj, site_id=obj.site_id,
                                storage_type=OTHER)
    setattr(loc, field, value)
    # A row a cell edit created must go when the cell is emptied — but only the
    # row it created. A location whose storage type is `other` was made by this
    # function, from a box number and nothing else, so clearing the box leaves
    # it saying nothing and it goes. A location that records a **real
    # temperature** is a different matter: "it is in the −20, box not written
    # down" is a true and useful thing to hold, and deleting it would empty the
    # Storage cell as a side effect of clearing Box — two cells moving on one
    # edit, which reads as a bug whichever way you meant it. Removing that one
    # is `set_type`'s job, and it refuses while a box is still recorded.
    if records_nothing(loc) and loc.storage_type == OTHER:
        if loc.pk:
            loc.delete(using=db)
        return None, ""
    loc.save(using=db)
    return loc, ""


def set_type(obj, raw, *, db: str = DB) -> tuple[object, str]:
    """Write the storage temperature. ``(location, error)``.

    Emptying it is only allowed once the location records nothing else —
    otherwise the row would go on saying *box 42* with no freezer to look in,
    and the refusal says which cell to clear first.
    """
    text = str(raw if raw is not None else "").strip()
    loc, err = one_of(obj)
    if err:
        return None, err
    if not text:
        if loc is None:
            return None, ""
        if not records_nothing(loc):
            return None, (f"This vial is recorded as {label(loc)}. Clear the "
                          f"box before removing the temperature, or the record "
                          f"says which box with no freezer to look in.")
        loc.delete(using=db)
        return None, ""
    code, refusal = read_type(text)
    if refusal:
        return None, refusal
    if loc is None:
        site_refusal = _site_refusal(obj)
        if site_refusal:
            return None, site_refusal
        loc = InventoryLocation(antibody=obj, site_id=obj.site_id)
    loc.storage_type = code
    loc.save(using=db)
    return loc, ""


def write_row(obj, row: dict, *, db: str = DB) -> object | None:
    """Record where a pasted or uploaded row says this antibody lives.

    Fill-only-blank, like every other column on that path: a location already on
    file keeps what it has, and a blank cell never clears one. Dedups on the
    whole place, so re-uploading the same sheet does not stack a second row —
    the same guard ``bulk_cell_lines._ensure_location`` has always had, for the
    same reason.

    Returns the location written or ``None`` when the row said nothing about
    storage, which is the ordinary case.
    """
    fields = {f: str(row.get(f) or "").strip() for f in HIER}
    type_raw = row.get("storage_type") or row.get("storage") or ""
    code, _err = read_type(type_raw, default=OTHER) if str(type_raw).strip() \
        else ("", "")
    if not any(fields.values()) and not code:
        return None
    if not getattr(obj, "site_id", None):
        return None
    existing, err = one_of(obj)
    if err:
        # In two places already. A paste is not the surface on which to choose
        # between them, and it has nothing new to add that the rows do not
        # already say.
        return None
    if existing is None:
        loc = InventoryLocation(antibody=obj, site_id=obj.site_id,
                                storage_type=code or OTHER, **fields)
        loc.save(using=db)
        return loc
    changed = False
    for field, value in fields.items():
        if value and not (getattr(existing, field, "") or "").strip():
            setattr(existing, field, value)
            changed = True
    if code and existing.storage_type == OTHER and code != OTHER:
        existing.storage_type = code
        changed = True
    if changed:
        existing.save(using=db)
    return existing
