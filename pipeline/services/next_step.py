"""The context a board needs to show one gene's progress strip.

One function, called by all four boards' page views, so the strip cannot end up
saying different things on different boards — and so a board that grows a gene
filter later gets it by adding one line.

Everything is derived through ``services/gene_progress.py``. Nothing here reads
``Target.status``, which is written once when a row is created and advanced by
nothing (CLAUDE.md), and nothing types a percentage: many genes never need all
four applications, so a fraction would call finished work incomplete.

**It answers only when a board is filtered to exactly one gene.** "The next
step" names a single thing or it names nothing, and a strip over an unfiltered
board would be about whichever gene sorted first.
"""
from __future__ import annotations

from pipeline.models import Target
from pipeline.services import gene_progress
from pipeline.services import targets as target_svc

DB = "pipeline_db"


def context(gene: str) -> dict:
    """`{}` unless `gene` names exactly one target on file.

    An unknown gene returns nothing rather than an empty strip: the boards
    already say "nothing matches these filters", and a second empty panel above
    that is noise. ``NA`` is not a gene at all
    (``services/targets.py::NOT_APPLICABLE``) and is refused here too, or the
    96 wild types attached to the placeholder target would produce a progress
    strip for something that is not a gene.
    """
    gene = (gene or "").strip()
    if not gene or target_svc.is_not_applicable(gene):
        return {}
    target = (Target.objects.using(DB)
              .filter(gene_name__iexact=gene)
              .select_related("site").first())
    if target is None:
        return {}

    steps = gene_progress.steps_for(target)
    return {
        "next_step_gene": target.gene_name or gene,
        "next_step_steps": steps,
        "next_step_headline": gene_progress.headline(steps),
        "next_step": gene_progress.next_step(steps),
        "next_step_target_id": target.pk,
    }
