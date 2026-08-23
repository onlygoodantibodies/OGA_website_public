"""The search box in the nav bar, and the page it lands on.

Logic lives in ``services/find.py``; this is thin.
"""
from __future__ import annotations

from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from pipeline.decorators import pipeline_member_required
from pipeline.services import find as finder


@pipeline_member_required
@require_GET
def find(request):
    """Resolve what someone typed.

    An exact gene goes straight to that gene's page — that is the common case and
    the fastest useful answer. Anything else lands here, showing where the string
    appears across the four boards, because picking one of three matching
    antibodies for somebody is how you send them to the wrong record.
    """
    term = (request.GET.get("q") or "").strip()

    target = finder.exact_gene(term)
    if target is not None and not request.GET.get("all"):
        return redirect("pipeline:target_detail", pk=target.pk)

    return render(request, "pipeline/find.html", {
        "term": term,
        "results": finder.find(term),
        "exact_gene": target,
    })


@pipeline_member_required
@require_GET
def session_detail(request, pk):
    """The old session page, now a redirect to the board with its results open.

    Everything that page did is on the sessions board: the results grid, the
    session conditions, the protocol phases, and the bench-sheet round trip. What
    it still has is *links* — the first field test recorded every result here, so
    the bookmarks and the pasted URLs exist, and a 404 would strand them.

    It keeps its URL name for the same reason ``pipeline:dashboard`` does.
    """
    return redirect(f"{reverse('pipeline:session_board')}?open={pk}")
