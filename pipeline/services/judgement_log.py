"""The history of a judgement — the one writer and the one reader.

``models.JudgementChange`` says why the table exists. This module is where a
change is written (``log``, called from ``outcomes.record``, ``Antibody.save``
and ``review.set_recommended``) and read back (``for_gene``, the history page).

A change that changes nothing is not logged: pressing *Selective* on an
antibody already marked selective is a click, not a decision, and a history
full of them hides the ones that were.
"""
from __future__ import annotations

from pipeline import saved_by

DB = "pipeline_db"


def flag_application(field: str) -> str:
    """A recommendation flag's application, `PublicationImage` spelling —
    read off `outcomes.REC_FIELD` rather than kept as a second map."""
    from pipeline.services.outcomes import REC_FIELD
    return {flag: app for app, flag in REC_FIELD.items()}[field]

#: Which door a path is, most specific first. A path matching none is shown
#: as it is, which is still an answer.
_DOORS = (
    ("/review/release", "Released from the review queue"),
    ("/review/withdraw", "Withdrawn (review queue)"),
    ("/outcomes/withdraw", "Withdrawn (Judge outcomes)"),
    ("/review/", "Review queue"),
    ("/outcomes/", "Judge outcomes"),
    ("/cropper/", "Figure cropper"),
    ("/sessions/", "Sessions"),
    ("/data/", "Downloads & uploads"),
)


def yes_no(value) -> str:
    """A recommendation flag as the history prints it."""
    return "yes" if value else "no"


def _source() -> str:
    request = saved_by._request.get()
    if request is not None:
        return (getattr(request, "path", "") or "")[:200]
    name = saved_by.current()
    return name if name.startswith(saved_by.SCRIPT_PREFIX) else ""


def log(antibody, application, field, old, new, *, queued=False, actor=None):
    """Record one change, or nothing when ``old`` and ``new`` are the same.

    ``antibody`` is an ``Antibody`` (its catalogue and gene are copied onto the
    row) or a bare pk, which is looked up. ``actor`` defaults to whoever
    ``saved_by`` says is saving now.
    """
    from pipeline.models import Antibody, JudgementChange

    old, new = old or "", new or ""
    if old == new:
        return None
    if not isinstance(antibody, Antibody):
        antibody = (Antibody.objects.using(DB).select_related("target")
                    .filter(pk=antibody).first())
        if antibody is None:
            return None
    target = antibody.target if antibody.target_id else None
    return JudgementChange.objects.using(DB).create(
        antibody_id=antibody.pk,
        catalogue_number=(antibody.catalogue_number or "")[:255],
        gene=((target.gene_name if target else "") or "")[:100],
        application_type=application,
        field=field,
        queued=queued,
        old_value=old,
        new_value=new,
        changed_by=(actor if actor is not None else saved_by.current())[:150],
        source=_source(),
    )


def door(source: str) -> str:
    """Where a change came from, in words."""
    if source.startswith(saved_by.SCRIPT_PREFIX):
        return saved_by.who(source)
    for prefix, label in _DOORS:
        if prefix in source:
            return label
    return source


def for_gene(gene):
    """Every change on a gene's antibodies, newest first (a queryset)."""
    from pipeline.models import JudgementChange

    qs = JudgementChange.objects.using(DB).all()
    if gene:
        qs = qs.filter(gene__iexact=gene)
    return qs
