"""Adding a freeze-down batch to a cell line.

The lab's workflow is: freeze down a line → the batch gets the site's next
C-number → the vials go in boxes. ``CellLineVial`` is that batch, and the number
is written on every tube in it, which is why ``lab_numbers`` treats a batch and a
line as one run of numbers.

**One batch at a time, one C-number.** The quantity a person types is how many
physical vials the freeze-down produced (``vial_count``), and those do *not* take
a number each — the whole batch shares one. Getting that backwards would burn a
number per vial, and a burnt number is permanent: ``next_number`` is one past the
highest in use and never fills a gap, because the box it was written on may still
be in the freezer.

Until now there was no way to record a second batch at all. ``bulk_cell_lines.
_ensure_vial`` is the only code that ever made one, reached only by pasting or
uploading a sheet — so adding a batch meant re-pasting the line with a different
number, which is indistinguishable from correcting a typo. 123 lines on live
already carry more than one batch, all of it from the Access import.
"""
from __future__ import annotations

from datetime import date, datetime

from django.db import transaction

from pipeline.models import CellLine, CellLineVial
from pipeline.services import c_number as c_number_svc
from pipeline.services import cell_lines as cell_lines_svc
from pipeline.services import deletion as deletion_svc
from pipeline.services import lab_numbers

DB = "pipeline_db"


def refusal(line, member, is_superuser: bool) -> str:
    """Why this person may not add a batch to this line, or ``""``.

    The same rule as deleting and as attaching a file — your own bench's
    records, or superuser — asked of the one reader, because a second copy of
    the wording is how two surfaces come to answer one question differently.
    """
    if line is None:
        return "That cell line is not on file."
    if not getattr(line, "site_id", None):
        return (f"{line.name} has no site recorded, and a C-number belongs to a "
                f"bench — set the line's site before adding a batch.")
    return deletion_svc.site_refusal(
        "cell line", line, member, is_superuser,
        action="add a batch to", su_verb="add one to")


def preview(line, *, vial_count=None, db: str = DB) -> dict:
    """What pressing Add would do, without doing it.

    Says a number **will** be issued without promising which, the same way the
    paste box does: between the check and the save somebody else's freeze-down
    may take it, and naming it here would print a number on screen that the tube
    does not get.
    """
    existing = cell_lines_svc.batch_numbers(line, db=db)
    return {
        "line": line.name,
        "existing_batches": len(existing),
        "existing_label": cell_lines_svc.format_batches(existing),
        "will_be_numbered": bool(line.site_id),
        "vial_count": vial_count,
        # One press, one batch, one number — said plainly, because "add 6" is
        # otherwise read as six numbers.
        "note": (f"One batch will be added and given this site's next C-number. "
                 f"{_vials_phrase(vial_count)}"),
    }


def _vials_phrase(vial_count) -> str:
    if not vial_count:
        return "The vials in it share that one number."
    return (f"All {vial_count} vial{'s' if vial_count != 1 else ''} in it share "
            f"that one number.")


def parse_freeze_date(raw):
    """``(date, error)``. Blank means **today** — you record a freeze-down on the
    day you did it, so the common case types nothing.

    A blank defaulting to today is the one place in this app where blank does not
    mean "not written down": a batch was frozen on some day, and leaving the
    column empty records nothing anybody can act on. The form shows today's date
    filled in rather than defaulting silently, because a value the app supplies
    is a thing the app did.
    """
    if raw in (None, ""):
        return date.today(), ""
    if isinstance(raw, date):
        return raw, ""
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date(), ""
        except ValueError:
            continue
    return None, (f"'{text}' is not a date — write it as 2026-08-16, or leave it "
                  f"blank for today.")


def add(line, *, member, is_superuser=False, vial_count=None, freeze_date=None,
        passage_number=None, c_number=None, notes="", db: str = DB) -> dict:
    """Add one freeze-down batch. Returns what was written, naming the number.

    The number is issued by ``lab_numbers`` on ``pre_save`` — not here — so this
    path cannot drift from the eight others. A **typed** number is kept as typed
    and refused if another record already holds it, which is the same answer the
    grid gives. A blank freeze date is **today**.
    """
    why = refusal(line, member, is_superuser)
    if why:
        return {"ok": False, "error": why}

    typed = None
    if c_number not in (None, ""):
        typed, err = c_number_svc.parse(c_number)
        if err:
            return {"ok": False, "error": err}
        holder = lab_numbers.holder(lab_numbers.CELL_LINE, line.site_id, typed, db=db)
        if holder is not None:
            return {"ok": False,
                    "error": (f"{c_number_svc.label(typed)} is already "
                              f"{lab_numbers.holder_label(holder)}. Leave the "
                              f"number blank to be given the next free one.")}

    freeze_date, date_err = parse_freeze_date(freeze_date)
    if date_err:
        return {"ok": False, "error": date_err}

    if vial_count is not None:
        try:
            vial_count = int(vial_count)
        except (TypeError, ValueError):
            return {"ok": False,
                    "error": (f"'{vial_count}' is not a number of vials — type a "
                              f"whole number, or leave it blank if you are not "
                              f"recording how many.")}
        if vial_count < 1:
            return {"ok": False, "error": "A batch has at least one vial."}

    with transaction.atomic(using=db):
        batch = CellLineVial(
            cell_line_id=line.pk, site_id=line.site_id, c_number=typed,
            vial_count=vial_count, freeze_date=freeze_date,
            passage_number=passage_number, notes=notes or "", received=True)
        batch.save(using=db)
        # The line's own column is the Access-era bridge: it carries the *first*
        # batch's number so the old single-table search could find the row. It is
        # filled only when blank, and never moved — the tube it names is still in
        # the freezer.
        if line.c_number is None and batch.c_number is not None:
            line.c_number = batch.c_number
            line.save(using=db, update_fields=["c_number"])

    # The board prefetches `vials`, so a line handed in from there carries a
    # cache that predates the batch just written. Drop it or the receipt reports
    # the line as it was a moment ago.
    if hasattr(line, "_prefetched_objects_cache"):
        line._prefetched_objects_cache.pop("vials", None)
    numbers = cell_lines_svc.batch_numbers(line, db=db)
    return {
        "ok": True,
        "batch_id": batch.pk,
        "c_number": c_number_svc.label(batch.c_number),
        "issued": typed is None and batch.c_number is not None,
        "vial_count": batch.vial_count,
        "batches": len(numbers),
        "batch_label": cell_lines_svc.format_batches(numbers),
        "message": _receipt(batch, typed is None),
    }


def _receipt(batch, issued: bool) -> str:
    """What was written, naming the number — a number the app gave a record is a
    thing the app did, so it says so."""
    label = c_number_svc.label(batch.c_number) or "no C-number"
    verb = "issued" if issued else "recorded"
    vials = (f", {batch.vial_count} vial{'s' if batch.vial_count != 1 else ''}"
             if batch.vial_count else "")
    # The freeze date is said too, because a blank one becomes today — a value
    # the app supplied, and those get named rather than assumed.
    when = f", frozen {batch.freeze_date:%d %b %Y}" if batch.freeze_date else ""
    return f"Batch {label} {verb}{vials}{when}."
