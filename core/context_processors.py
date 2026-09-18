"""Template context every surface that draws a recommendation needs.

``core/templates/core/_recommendation_caveat.html`` is the one wording, and it
reads ``scope_note_html`` and ``consensus_protocol_url`` from here.

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

from django.utils.html import escape, format_html

from . import recommendations as R


def _emphasised(note, fragment):
    """``note`` with ``fragment`` in bold, escaped, for a template to print.

    The markup is applied here rather than in ``recommendations``, which is
    Django-free and whose strings travel into JSON on the API, the archive and
    the extension index — a ``<strong>`` in a data field is a tag a consumer has
    to strip.

    A fragment that is not in the note leaves the note **whole and unemphasised**
    rather than raising. A caveat is owed on every one of these pages, and the
    reader of a sentence that lost its bold has still read the sentence; a page
    that 500s has read nothing. `tests_recommendation_caveat` is what makes that
    a visible failure instead of a silent one, since nothing on screen could say
    the emphasis had stopped being drawn.
    """
    before, found, after = note.partition(fragment)
    if not found:
        return escape(note)
    return format_html('{}<strong>{}</strong>{}', before, found, after)


def recommendation_scope(request):
    """What an OGA recommendation does and does not cover, plus the protocols."""
    return {
        'scope_note': R.SCOPE_NOTE,
        # The same sentence with the clause about the reader's own experiment
        # in bold — `SCOPE_EMPHASIS` says why that clause and not another. Drawn
        # by `_recommendation_caveat.html`; `scope_note` stays beside it as the
        # plain text, for anything that is not HTML.
        'scope_note_html': _emphasised(R.SCOPE_NOTE, R.SCOPE_EMPHASIS),
        'consensus_protocol_url': R.CONSENSUS_PROTOCOL_URL,
        # The per-application caveats, as a plain list of sentences.
        #
        # Not the dict: `APPLICATION_SCOPE` is keyed on 'ICC-IF', and a Django
        # template cannot index a dict on a key with a hyphen in it — the page
        # would render blank, which is the same silent-absence failure this
        # whole module exists to prevent. Each sentence names its own
        # application ("Immunofluorescence results are ..."), so nothing is lost
        # by dropping the key, and a second application gaining one needs no
        # template change anywhere.
        'application_scope_notes': list(R.APPLICATION_SCOPE.values()),
        # The half of those sentences that is a fact about the application
        # rather than a pointer at the gene page — for the gene page itself,
        # which is what the other half points at. `APPLICATION_FACT`'s own
        # comment says why the two are separable; an application with nothing
        # specific to say is absent from it, so this list is usually one
        # sentence long and is sometimes empty.
        'application_facts': [R.APPLICATION_FACT[app] for app in R.APPLICATIONS
                              if app in R.APPLICATION_FACT],
    }
