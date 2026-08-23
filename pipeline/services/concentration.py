"""Antibody concentration: one reader, and it never drops a unit.

``Antibody.concentration`` is a number whose unit is **µg/mL** and is not stored
alongside it. Every write path therefore has to convert into that unit, and the
one thing it must never do is keep the digits and throw the unit away.

That is what it did. ``cropper/commit.py`` pulled the first number out of the
cell with a regex — a comment above it said the value was "kept in the pasted
format (a bare number, no unit conversion)", which is true of the digits and
false of the meaning. The sixth field test pasted a supplier's own
``1.0 mg/mL``; it was stored as ``1``, drawn on the board under ``CONC.`` and
printed in the report's Table 2 under a heading reading µg/µl. One paste, a
thousand-fold error, and nothing on any screen to notice it by. The board's edit
path was quieter still: ``Decimal("1.0 mg/mL")`` raises, the handler passed, and
the cell simply kept its old value.

So: convert what we understand, and refuse what we do not, by name. A refusal
that lists the units it accepts is the difference between "that is wrong" and
"here is what is right" — the same rule as ``services/sites.py``.

Note µg/µL and mg/mL are the same thing (both 1000 µg/mL), and mg/L is the same
as µg/mL. Those coincidences are why a dropped unit is so hard to spot by eye.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# The stored unit, spelled the way a scientist writes it.
STORED_UNIT = "µg/mL"

# Multipliers into µg/mL. Keys are normalised: lower-cased, µ folded to u, and
# every space and dot removed, so "µg / mL", "ug/ml" and "u g/m l" all arrive
# here as "ug/ml".
_FACTORS = {
    "ug/ml": Decimal(1),
    "mcg/ml": Decimal(1),
    "mg/l": Decimal(1),
    "ug/ul": Decimal(1000),
    "mg/ml": Decimal(1000),
    "ug/l": Decimal("0.001"),
    "ng/ml": Decimal("0.001"),
    "ng/ul": Decimal(1),
    "g/l": Decimal(1000),
}

ACCEPTED = "µg/mL, mg/mL, µg/µL, mg/L, ng/mL, ng/µL, µg/L or g/L"

_NUMBER = re.compile(r"^[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def normalise_unit(raw: str) -> str:
    return (raw or "").strip().lower().replace("µ", "u").replace(
        "μ", "u").replace(" ", "").replace(".", "").replace("per", "/")


def parse(raw) -> tuple[Decimal | None, str]:
    """``(value_in_ug_per_ml, error)``.

    ``(None, "")`` for a blank cell — blank means "not written down", never a
    reason to refuse. ``(None, message)`` when the cell says something this
    cannot turn into a concentration; the message names what was typed and what
    is accepted. Otherwise ``(Decimal, "")``.
    """
    text = str(raw or "").strip()
    if not text:
        return None, ""

    m = _NUMBER.match(text)
    if not m:
        return None, (f"'{text}' is not a concentration — give a number, "
                      f"optionally with a unit ({ACCEPTED}).")
    try:
        value = Decimal(m.group(0))
    except InvalidOperation:
        return None, (f"'{text}' is not a concentration — give a number, "
                      f"optionally with a unit ({ACCEPTED}).")

    unit = normalise_unit(text[m.end():])
    if not unit:
        # A bare number is taken as the stored unit, which is what every sheet
        # exported from here contains.
        return value, ""
    factor = _FACTORS.get(unit)
    if factor is None:
        return None, (f"'{text}' has a unit this cannot convert. Concentration "
                      f"is stored in {STORED_UNIT}; give a bare number in "
                      f"{STORED_UNIT}, or use {ACCEPTED}.")
    return value * factor, ""
