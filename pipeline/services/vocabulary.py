"""What a column already says, offered as a reminder rather than a rule.

A grid cell is a bare text box by default, and for a field with a small set of
sensible answers that is the wrong control twice over: it invites a value the
server will refuse, and — worse, because nothing refuses it — it invites a
*second spelling* of a value that is already there. Both failures are quiet.

Two kinds of field, and the difference is whether an unlisted value is
legitimate. ``board.js::editorHtml`` already draws both and this module feeds the
second:

* **closed** — a model's own ``choices``, or a set the writer refuses outside of.
  A ``<select>``, built from the enum, because anything else is a mistake the
  save would reject anyway.
* **open with a convention** — an isotype, a host species, a reactivity string.
  An ``<input>`` with a ``<datalist>``, because a vocabulary nobody may add to is
  one that stops describing the bench. The list is what the database already
  holds, so it is a reminder and never a gate. Same trade ``newEntry``'s
  ``suggestions`` makes, and the same one the sessions board's ``rating`` cell
  makes.

**The case fold is the half that earns this module's existence.** ``host_species``
on live holds ``rabbit`` 2,113 times and ``Rabbit`` 250, ``mouse`` 700 and
``Mouse`` 83; ``isotype`` holds ``IgG1 Kappa`` 30 times and ``IgG1 kappa`` 11
(read 1 Sep 2026). A list built from raw distinct values would offer both
spellings of each, side by side, and a picker that offers a split is a picker
that entrenches it — the reader picks whichever is nearer the cursor and the
column is no more consistent than before. So the offered list is folded, and the
spelling offered is **the one already most used**.

Nothing here writes. The minority spellings stay exactly as they are: which
spelling a column *should* carry is a curation question, the same one
``fix_gene_case`` refuses to answer on its own, and merging them would be a live
data change made as a side effect of drawing a dropdown.
"""
from __future__ import annotations

from collections import Counter

DB = "pipeline_db"

#: A field with more distinct values than this is not a vocabulary — it is free
#: text, and a datalist of hundreds is a scroll rather than a reminder. Callers
#: get an empty list and the cell stays a plain box, which is the honest
#: control for a field nobody has a convention for.
MAX_OPTIONS = 40


def on_file(model, field: str, *, db: str = DB, limit: int = MAX_OPTIONS,
            extra_filter=None) -> list[str]:
    """The values this column already holds, commonest first, one per spelling.

    One ``GROUP BY`` over the column — not a walk over the rows — so the cost
    does not grow with the table. Returns ``[]`` when the column has more
    distinct values than ``limit``, which is the signal that it is free text.
    """
    qs = model.objects.using(db)
    if extra_filter is not None:
        qs = qs.filter(extra_filter)
    counts = Counter()
    spellings: dict[str, Counter] = {}
    for raw, n in qs.values_list(field).annotate(**{"_n": _count(field)}):
        text = (raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        counts[key] += n
        spellings.setdefault(key, Counter())[text] += n
        if len(counts) > limit:
            return []
    # Commonest first, and alphabetical within a tie so the list is stable
    # between page loads — a picker whose order moves under you is its own bug.
    ordered = sorted(counts, key=lambda k: (-counts[k], k))
    return [spellings[k].most_common(1)[0][0] for k in ordered]


def _count(field):
    from django.db.models import Count
    return Count(field)


def choices(model, field: str, **kw) -> dict:
    """A ``cellChoices`` entry for an **open** field: values, never strict.

    ``strict`` is spelled out rather than left to default. ``editorHtml`` treats
    a missing key as false, so the behaviour would be identical — but this dict
    goes over the wire and is read by tests and by anyone debugging a picker,
    and "open" is a decision about the field rather than an absence.
    """
    return {"values": on_file(model, field, **kw), "strict": False}


#: Fields no ``offerable`` sweep should ever suggest, however few distinct
#: values they happen to have today. ``comments`` is prose — a datalist of
#: whole sentences somebody else wrote is not a vocabulary, and offering one
#: invites a person to file another run's note as their own.
NEVER_SUGGEST = {"comments", "notes", "note"}


def offerable(model, *, skip=(), **kw) -> dict:
    """``{field: {"values": [...]}}`` for every text column with a vocabulary.

    **Derived, not typed.** A hand-written list of "fields worth offering" is a
    second place the truth lives, and it goes stale the moment somebody records
    a new kind of value — which is the whole reason ``on_file`` reads the
    column. So the sweep asks every text field and lets the cap answer: a column
    with a handful of distinct values gets a list, one with hundreds gets
    nothing and stays a plain box.

    Measured against live (1 Sep 2026) that line falls in a sensible place on
    its own. Under the cap and well used: ``wb.signal`` 3 distinct over 1,927
    rows, ``ip.enrichment`` 3 over 1,631, ``if.specific_signal`` 3 over 1,256,
    ``ip.lysis_buffer`` 5 over 1,544, ``wb.ecl`` 8 over 1,878, ``wb.gel`` 17,
    ``ip.bead_type`` 17, ``wb.secondary_ab`` 20. Over it and correctly left
    alone: ``wb.dilution`` at 76.
    """
    from django.db import models as dj

    out = {}
    for field in model._meta.get_fields():
        if not isinstance(field, (dj.CharField, dj.TextField)):
            continue
        if field.name in NEVER_SUGGEST or field.name in skip or field.choices:
            continue
        values = on_file(model, field.name, **kw)
        if values:
            out[field.name] = {"values": values, "strict": False}
    return out


def sites(*, blank: str = "", db: str = DB) -> dict:
    """The sites on file, as a **closed** set — one reader for all four boards.

    `sites.strict_id` refuses a name that is not one on file, so a text box here
    was a control that could only ever be wrong twice: it offered a spelling
    nobody would accept, and it did it on four boards that each wrote the list
    out again. ``blank`` names the empty option where clearing the field is a
    real edit — on an antibody or a cell line it means the vial belongs to no
    bench, which `strict_id` reads as exactly that.
    """
    from pipeline.models import Site

    values = [{"value": s.name, "label": s.name}
              for s in Site.objects.using(db).filter(is_active=True).order_by("name")]
    if blank:
        values = [{"value": "", "label": blank}] + values
    return {"strict": True, "values": values}


def enum(field_choices, *, blank: str = "") -> dict:
    """A ``cellChoices`` entry for a **closed** field, from a model's ``choices``.

    ``blank`` adds a leading option whose value is empty, labelled with the text
    given — for a field a person is allowed to clear. Without one, opening a
    dropdown on an empty cell and closing it again writes the first option,
    which is a value nobody chose.
    """
    values = [{"value": v, "label": label} for v, label in field_choices]
    if blank:
        values = [{"value": "", "label": blank}] + values
    return {"strict": True, "values": values}
