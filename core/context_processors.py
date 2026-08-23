"""Template context every surface that draws a recommendation needs.

``core/templates/core/_recommendation_caveat.html`` is the one wording, and it
reads ``scope_note`` and ``consensus_protocol_url`` from here.

Supplying them from a context processor rather than from each view *is* the
point. The caveat is owed by six surfaces across two apps — the gene pages, the
portal, the embeddable card, ``/data-access/``, ``/extension/`` and the
selection tool — and four of them had grown their own hand-typed copy of the
sentence, which is how one of them (the gene page) came to be saying something
different from the other three. A view that forgot to pass the value would
render the include **blank**: a caveat silently absent on a page that looks like
it has one, which is worse than never having added it.

Two constants and no query, so it costs a dict on every request.
"""
from __future__ import annotations

from . import recommendations as R


def recommendation_scope(request):
    """What an OGA recommendation does and does not cover, plus the protocols."""
    return {
        'scope_note': R.SCOPE_NOTE,
        'consensus_protocol_url': R.CONSENSUS_PROTOCOL_URL,
    }
