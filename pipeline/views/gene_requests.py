"""Genes the public has asked us to characterise.

A read-only list, grouped by gene, most-asked first. It exists because the write
path had to have a screen: a request nobody can see is the same as one that was
never recorded, and the whole point of replacing the contact form was to be able
to count what people ask for.

**Not the Portfolio, and not the target board.** Those two answer *what the
consortium is doing*; this answers *what people outside are waiting for*, and
the two must not be added together — see ``pipeline/gene_request_models.py`` for
why a public request is a different row from a ``TargetNomination``. A request
becomes work by somebody deciding it should, on the target board or the
feasibility page, and each row here links to both.

Nothing on this page writes. That is deliberate rather than unfinished: acting
on a request means adding the gene to a site's list with a funder behind it,
which is exactly what the feasibility page's Add already does — a second, weaker
copy of it here would be a third door to a gene.
"""
from __future__ import annotations

from django.shortcuts import render

from pipeline.decorators import pipeline_member_required
from pipeline.services import gene_requests as GR


@pipeline_member_required
def gene_request_board(request):
    rows = GR.by_gene()
    return render(request, 'pipeline/gene_requests.html', {
        'rows': rows,
        # The heading counts genes; the sub-line counts requests. Two different
        # questions on one screen, so both are named rather than one number
        # being left to stand for whichever the reader assumed.
        'gene_count': len(rows),
        'request_count': sum(row['count'] for row in rows),
        'funded_offers': sum(row['funded_offers'] for row in rows),
        'unchecked': sum(row['unchecked'] for row in rows),
    })
