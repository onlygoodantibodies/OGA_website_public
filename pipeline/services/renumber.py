"""Give a bench's antibodies their A-numbers, or deal them out again.

uOttawa logged eighteen antibodies into the portal, stopped, and kept a
spreadsheet instead. The reason was not a defect: *"since it automatically
assigns numbers, I held off … I wanted to take advantage of being able to
rearrange Abs against each protein in a single box as I receive the Abs."*
Reagents arrive over weeks, and the number decides which box a vial goes in, so
numbering at the moment of logging forces the order of arrival onto the freezer.
Opening two boxes for a protein instead of five is the whole of the ask.

**Why this is not a cell you can retype.** Swapping A-1 and A-2 is impossible one
edit at a time: every intermediate state collides, `lab_numbers.clash` refuses
it, and the way round is a scratch number — which is exactly how two vials come
to share one. So the unit here is the **whole mapping**, computed and checked
together and written in one transaction. A swap is then one operation rather than
two edits, and there is no moment in between when the data is wrong.

Four rules hold it up.

**The mapping is checked as a whole, against itself and against the rest of the
bench.** Two rows in the set landing on one number, or a row landing on a number
some antibody outside the set already carries, refuses the batch by name — not
the row. Half a renumbering is worse than none: the reader would have no way to
know which half.

**A number that has left the app is not ours to change.** `bench_results.
_resolve_ab` reads the `Ab#` off an uploaded bench sheet and matches it against
`ab_number` **across the gene**, so renumbering a vial whose number is already on
a printed sheet can file a later reading against a different antibody — silently,
which is this codebase's characteristic defect. Any antibody carrying a reading,
a published figure, a queued crop or a recorded outcome is therefore **excluded
and named**, never renumbered. The app cannot know what is written on a tube, so
the panel says that part out loud instead.

**The number on the button is what was consented to** — the same rule
`review.release` and `services/deletion.py` follow. `apply` re-plans and refuses a
set that moved, because a preview is not a permission slip. A *count* is not
enough for that: one row leaving the filter as another joins it keeps the count
and changes the set entirely, so `plan` stamps the mapping and `apply` compares
it, the way `bulk_targets.plan` stamps its rows.

**Your own bench's numbers, or you are a superuser** — the rule
`deletion.site_refusal` states for deleting and `attachments` for attaching.
These numbers are written on somebody else's freezer boxes, and nothing in the
app can tell you whether they are.

What this deliberately does **not** do: fill gaps, renumber across sites (a
number belongs to one bench — McGill's A-1 and Leicester's A-1 are different
antibodies), or touch a record it was not handed.
"""
from __future__ import annotations

import hashlib

from django.db import transaction
from django.db.models import Exists, OuterRef

from pipeline.models import Antibody
from pipeline.services import lab_numbers
from pipeline.services import session_board

DB = "pipeline_db"

#: The four procedures, and the reverse accessor each result table hangs off.
_PROCEDURES = {"wb_results": "WB", "ip_results": "IP",
               "if_results": "IF", "fc_results": "FC"}

#: The other three ways an antibody's number reaches somewhere this app cannot
#: reach back into. Read off the relations rather than a flag, so a new way of
#: using an antibody is one line here rather than something somebody must
#: remember to set at the other end.
_OTHER = ("publication_images", "pending_images", "outcomes")

#: Why each kind of history pins a number. Ordered as `_held_back` tests them:
#: the most consequential and most likely first, and **`planned` last, because
#: it is the precautionary one rather than the certain one**.
_HELD = {
    "reading": ("has a reading recorded against it — that number is on a bench "
                "sheet by now, and moving it would file a later reading against "
                "a different antibody"),
    "publication_images": ("has a published figure — the number under it is what "
                           "readers and the extension have been given"),
    "pending_images": ("has a crop waiting for review, which will carry this "
                       "number onto a public gene page"),
    "outcomes": "has a recorded outcome against it",
    # **A result row is not a reading.** `sessions.apply` writes one blank row
    # per antibody when a session is *planned*, so the four result relations are
    # true of an antibody nobody has read yet — and the first version of this
    # file told those rows they had "a western blot reading against it", which
    # is the precise sentence `session_board.is_reading` exists to prevent.
    #
    # They are still held back, and this is the honest reason: planning is when
    # the bench sheet gets printed, and `lab_numbers.sheet_number` prints the
    # A-number on it. So the risk is real; it is just not a reading.
    "planned": ("is on a planned experiment — a bench sheet may already have "
                "been printed carrying this number"),
}


def _attached_annotations() -> dict:
    """One ``EXISTS`` per question, so the cost does not grow with the set.

    `.exists()` per row per relation is a query an antibody per relation, and
    this runs over the **whole filtered set** — an unfiltered press would be
    tens of thousands of queries. The board's own rule ("query count does not
    grow with row count") applies here more than anywhere, because nothing on
    the page would say why it had stopped responding.

    Two questions per procedure, not one: whether a result row **exists** (the
    session is planned) and whether one **carries a reading**
    (`session_board.reading_q`, the single reader for that). They hold the row
    back either way; they are different sentences.
    """
    out = {}
    for accessor, procedure in _PROCEDURES.items():
        rel = Antibody._meta.get_field(accessor)
        rows = rel.related_model.objects.filter(
            **{rel.field.name: OuterRef("pk")})
        out[f"_att_row_{procedure}"] = Exists(rows)
        out[f"_att_read_{procedure}"] = Exists(
            rows.filter(session_board.reading_q(procedure)))
    for accessor in _OTHER:
        rel = Antibody._meta.get_field(accessor)
        out[f"_att_{accessor}"] = Exists(
            rel.related_model.objects.filter(**{rel.field.name: OuterRef("pk")}))
    return out


def _held_back(antibody) -> str:
    """Why this antibody's number may not move, or ``""``."""
    if any(getattr(antibody, f"_att_read_{p}") for p in _PROCEDURES.values()):
        return _HELD["reading"]
    for accessor in _OTHER:
        if getattr(antibody, f"_att_{accessor}"):
            return _HELD[accessor]
    if any(getattr(antibody, f"_att_row_{p}") for p in _PROCEDURES.values()):
        return _HELD["planned"]
    return ""


def _label(antibody) -> str:
    bits = [antibody.company.name if antibody.company_id else "",
            antibody.catalogue_number or f"#{antibody.pk}"]
    if antibody.target_id and antibody.target.gene_name:
        bits.append(f"({antibody.target.gene_name})")
    return " ".join(b for b in bits if b)


def plan(ids, *, start=None, member=None, is_superuser=False,
         db: str = DB) -> dict:
    """What renumbering these would do, in the order given. Read-only.

    ``ids`` arrive in the order the board is **showing** them, which is the
    order the numbers are dealt in — that is the whole interface. The board
    sorts by gene, then supplier, then catalogue, so "number them as shown" is
    already "group this protein's antibodies together", which is what was asked
    for.

    ``start`` is the first number to give out; ``None`` means continue the
    site's own run from its highest. Passing the lowest number the set already
    holds is what turns this from *assign* into *rearrange* — the same numbers,
    dealt out in a new order.
    """
    rows = list(Antibody.objects.using(db)
                .filter(pk__in=list(ids))
                .select_related("company", "target", "site")
                .annotate(**_attached_annotations()))
    by_id = {a.pk: a for a in rows}
    ordered = [by_id[i] for i in ids if i in by_id]

    if not ordered:
        return {"refusal": "No antibodies were chosen.", "items": [],
                "moving": 0, "site": ""}

    sites = {a.site_id for a in ordered}
    if len(sites) > 1:
        names = sorted({a.site.name for a in ordered if a.site_id})
        return {"refusal": (f"These antibodies are at {len(sites)} different "
                            f"benches ({', '.join(names)}). A number belongs to "
                            f"one bench — McGill's A-1 and Leicester's A-1 are "
                            f"different antibodies — so renumber one site at a "
                            f"time."),
                "items": [], "moving": 0, "site": ""}

    site_id = ordered[0].site_id
    if not site_id:
        return {"refusal": ("These antibodies have no site, and a number belongs "
                            "to a bench. Set the site first."),
                "items": [], "moving": 0, "site": ""}
    site_name = ordered[0].site.name

    # **Your own bench's numbers, or you are a superuser** — the rule
    # `deletion.site_refusal` states for deleting and `attachments` for
    # attaching. These numbers are written on somebody else's freezer boxes,
    # and nothing in the app can tell you whether they are.
    if not is_superuser:
        mine = getattr(member, "site_id", None)
        if not mine:
            return {"refusal": ("Your account has no site, so there are no "
                                "numbers here that are yours to change. A "
                                "superuser can set one on the people board."),
                    "items": [], "moving": 0, "site": site_name}
        if mine != site_id:
            return {"refusal": (f"These antibodies belong to {site_name}, not to "
                                f"your site. You can renumber your own bench's "
                                f"antibodies; a superuser can renumber any."),
                    "items": [], "moving": 0, "site": site_name}

    # Rows whose number has already reached paper keep it, and are named. They
    # stay out of the mapping entirely — including as *targets*, so a moving row
    # cannot land on one.
    movable, held = [], []
    for a in ordered:
        why = _held_back(a)
        (held if why else movable).append((a, why))

    first = start if start is not None else lab_numbers.next_number(
        lab_numbers.ANTIBODY, site_id, db=db)
    try:
        first = int(first)
    except (TypeError, ValueError):
        return {"refusal": f"'{start}' is not a number to start from.",
                "items": [], "moving": 0, "site": site_name}
    if first < 1:
        return {"refusal": "The first number must be 1 or more.",
                "items": [], "moving": 0, "site": site_name}

    mapping = {a.pk: first + i for i, (a, _why) in enumerate(movable)}
    taken = set(mapping.values())

    # Anything at this bench that is **not** being renumbered and whose number
    # one of these rows would land on. Held-back rows are in here too, which is
    # the point: their numbers are exactly the ones that must not be taken.
    moving_ids = set(mapping)
    clashes = list(Antibody.objects.using(db)
                   .filter(site_id=site_id, ab_number__in=sorted(taken))
                   .exclude(pk__in=moving_ids)
                   .select_related("company", "target"))

    items = []
    for a, why in held:
        items.append({"id": a.pk, "label": _label(a),
                      "old": lab_numbers.label(a.ab_number, kind=lab_numbers.ANTIBODY),
                      "new": "", "moves": False, "held": why})
    for a, _why in movable:
        new = mapping[a.pk]
        old = a.ab_number
        items.append({"id": a.pk, "label": _label(a),
                      "old": lab_numbers.label(old, kind=lab_numbers.ANTIBODY),
                      "new": lab_numbers.label(new, kind=lab_numbers.ANTIBODY),
                      "moves": old != new, "held": ""})

    refusal = ""
    if clashes:
        names = ", ".join(
            f"{lab_numbers.label(c.ab_number, kind=lab_numbers.ANTIBODY)} "
            f"({_label(c)})" for c in clashes[:5])
        more = "" if len(clashes) <= 5 else f" and {len(clashes) - 5} more"
        refusal = (f"This would give {len(clashes)} number"
                   f"{'' if len(clashes) == 1 else 's'} to a vial that already "
                   f"has {'it' if len(clashes) == 1 else 'them'} at {site_name}: "
                   f"{names}{more}. Two vials cannot share a number on a freezer "
                   f"box. Start from a higher number, or include those rows in "
                   f"the set so they move too.")

    moving = sum(1 for it in items if it["moves"])
    return {"refusal": refusal, "items": items, "moving": moving,
            "held": len(held), "site": site_name, "site_id": site_id,
            "first": first,
            # The whole mapping, so `apply` can be handed exactly what was shown
            # rather than recomputing from a set that may have moved.
            "mapping": {str(k): v for k, v in mapping.items()},
            "stamp": _stamp(mapping)}


def _stamp(mapping) -> str:
    """A short fingerprint of the whole mapping, for `apply` to compare against.

    The count alone is not enough. `bulk_targets.plan` stamps its rows for the
    same reason: two different sets can produce the same number of changes — one
    row leaving the filter as another joins it — and a consent check that only
    counts would wave that through. The stamp says *these* antibodies get
    *these* numbers, which is what the reader was actually shown.
    """
    text = ";".join(f"{k}:{v}" for k, v in sorted(mapping.items()))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Refused(Exception):
    """This renumbering will not be attempted, and the reason is for the reader."""


def apply(ids, *, start=None, consented_count, stamp="", member=None,
          is_superuser=False, db: str = DB) -> dict:
    """Write the mapping, in one transaction. Returns what changed.

    Re-plans first and refuses a set that has moved since the preview — a
    preview is not a permission slip, and the number on the button is what was
    consented to. Everything the plan refuses, this refuses.
    """
    with transaction.atomic(using=db):
        fresh = plan(ids, start=start, member=member,
                     is_superuser=is_superuser, db=db)
        if fresh["refusal"]:
            raise Refused(fresh["refusal"])
        if fresh["moving"] != consented_count:
            raise Refused(
                f"This would now change {fresh['moving']} number"
                f"{'' if fresh['moving'] == 1 else 's'}, not the "
                f"{consented_count} the button offered — somebody has edited "
                f"these rows since. Check them again and press it once more.")
        if stamp and stamp != fresh["stamp"]:
            raise Refused(
                "These are not the same antibodies the preview showed — the "
                "rows have changed since you checked them, even though the "
                "count has not. Press Check these again and read the new list.")

        mapping = {int(k): v for k, v in fresh["mapping"].items()}
        rows = {a.pk: a for a in Antibody.objects.using(db)
                .filter(pk__in=list(mapping))}
        changed = []
        for pk, number in mapping.items():
            antibody = rows.get(pk)
            if antibody is None or antibody.ab_number == number:
                continue
            was = lab_numbers.label(antibody.ab_number, kind=lab_numbers.ANTIBODY)
            antibody.ab_number = number
            # `update_fields` so nothing else on the row is rewritten — and
            # `pre_save` will not renumber it, because `lab_numbers.assign`
            # stands back for anything that is not being created.
            antibody.save(using=db, update_fields=["ab_number"])
            changed.append({"id": pk, "was": was,
                            "now": lab_numbers.label(number,
                                                     kind=lab_numbers.ANTIBODY)})
        return {"changed": changed, "site": fresh["site"],
                "held": fresh.get("held", 0)}
