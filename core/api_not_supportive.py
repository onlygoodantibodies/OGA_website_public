"""The not-supportive review list, as JSON and CSV.

Two endpoints over one reader (``core/not_supportive.py``), so the number on
the portal's button and the rows in the spreadsheet are the same set read two
ways. A count that came from a different query from the list it totals is this
codebase's most-recorded defect, and a download is the worst place for it: the
disagreement leaves the building.

  ``GET /api/v1/not-supportive/``      the rows, and a manifest of what they hold
  ``GET /api/v1/not-supportive/csv/``  one row per application finding

Everything here is **already published**. These are figures on public gene
pages and recommendations the public API already serves — this endpoint assembles
a slice that a supplier would otherwise have to walk every gene by hand to
build, and assembling it is the whole of what it adds. That is why, unlike
``core/api_pipeline.py``, a key with no supplier scope is **not** refused: there
is nothing here to leak. A scoped key is narrowed to its own reagents, an
unscoped one sees the dataset, and both are exactly what ``/v1/antibodies/``
already answers.

**A key is still required.** Not to protect the data but to attribute the
request and to apply the supplier filter.

**The download is deliberately not cached by anything shared.** It is per-key
by construction, and a supplier's own list sitting in an edge cache under a URL
with no key in it is the ``Vary`` mistake ``_cache_headers`` was written for,
with a file at the end of it.

**There was a PDF here and it was withdrawn** (owner, 14 Sep 2026). It rendered
one page per antibody with the figures in it, and the figures are the part it
could not be relied on to deliver: every one is an object-storage read on a
four-thread site, so the document was budgeted, and a budgeted document is one
that sometimes arrives short of the pictures it exists for. The page shows
every application's figure instead, which is where the comparison is actually
made — see ``core/templates/core/portal.html::nsCard``. Do not reinstate it
without solving the fetching, which is the whole of why it went.
"""
from __future__ import annotations

import logging
from datetime import date

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_safe

from . import api_throttle
from . import not_supportive as NS
from .api_views import (BASE_URL, _get_allowed_genes, _get_supplier_company_ids,
                        _guard)

logger = logging.getLogger(__name__)

#: What a scoped key that matches no supplier on file is told. Same refusal as
#: ``api_pipeline._scope``'s and for the same reason: an empty list reads as
#: "you have no failing reagents", which is a statement about the dataset made
#: from a fact about a configuration string — and it is the one wrong answer
#: here that a manufacturer would be pleased to receive.
NO_SUPPLIER_MATCH = (
    "This key is scoped to {names!r}, which matches no supplier name in the "
    "dataset, so it cannot be used to decide which reagents are yours. That is "
    "a configuration problem and not a statement about your products — email "
    "onlygoodantibodies@gmail.com and we will fix the scope.")


def _scope(request):
    """``(consumer, company_ids, allowed_genes, error)``.

    ``company_ids`` is ``None`` for a key with no supplier scope, which means
    the whole dataset — see the module docstring for why that is safe here and
    refused next door.
    """
    consumer, err = _guard(request)
    if err:
        return None, None, None, err

    names = (consumer.supplier_filter or '').strip()
    if not names:
        return consumer, None, _get_allowed_genes(consumer), None

    company_ids = _get_supplier_company_ids(names)
    if not company_ids:
        return None, None, None, JsonResponse(
            {'error': 'Supplier scope matches no supplier on file',
             'detail': NO_SUPPLIER_MATCH.format(names=names)}, status=409)
    return consumer, company_ids, _get_allowed_genes(consumer), None


def _scope_line(consumer, company_ids):
    """One sentence naming whose list this is — on the page and in the JSON.

    A document with no scope on it is one nobody can file. It names the key's
    own supplier string rather than the resolved company rows, because that is
    the thing the reader recognises.
    """
    if company_ids is None:
        return 'Every supplier in the OGA dataset.'
    return f'Reagents supplied by {consumer.supplier_filter}.'


def _genes(request):
    """``?gene=`` as a list, or ``None``. Comma-separated, the way every board takes it.

    **It narrows the download as well as the listing**, and that is the whole
    reason it exists here rather than in the browser: a filter that moved the
    drawn rows while the file stayed whole would be a download that did not
    match the count on its own button.
    """
    raw = (request.GET.get('gene') or '').strip()
    genes = [g.strip() for g in raw.split(',') if g.strip()]
    return genes or None


def _include_limited(request):
    """Is the *also include Limited support* tick on? Off unless asked.

    Off by default on the owner's instruction: the default list is the hard
    edge — antibodies where nothing on-target was seen in any application they
    were tested in — and the tick widens it to every antibody with no
    supportive result. A supplier meets the shorter, sharper list first.

    Read the same way every other boolean arrives on this API: present and not
    one of the false spellings. A value nobody sent is off, and `?limited=0`
    is off rather than on-because-it-is-a-non-empty-string.
    """
    raw = (request.GET.get('include_limited') or '').strip().lower()
    return raw not in ('', '0', 'false', 'no', 'off')


def _rows(request):
    """``(rows, references, scope_line, error)`` — the one place they all agree."""
    consumer, company_ids, allowed_genes, err = _scope(request)
    if err:
        return None, None, None, err
    genes = _genes(request)
    if genes and allowed_genes is not None:
        # A demo key's gene list is a ceiling, never a starting point: asking
        # for a gene outside it must narrow to nothing rather than widen.
        permitted = {g.lower() for g in allowed_genes}
        genes = [g for g in genes if g.lower() in permitted] or ['\x00none']
    rows = NS.rows(company_ids=company_ids,
                   allowed_genes=genes or allowed_genes,
                   base_url=BASE_URL,
                   include_limited=_include_limited(request))
    # The reference examples are resolved for the whole reply in one batched
    # pass and hung on the rows, so the page, the spreadsheet and the document
    # all name the same supportive antibody. Deliberately NOT scoped to the
    # caller: the question is what a good result on this gene looks like, and
    # the answer is often somebody else's product — all of it already on the
    # public gene page each row links to.
    references = NS.reference_figures({row['gene'] for row in rows}, BASE_URL)
    NS.attach_references(rows, references)
    return rows, references, _scope_line(consumer, company_ids), None


def _filename(extension):
    return f'oga-not-supportive-{date.today().isoformat()}.{extension}'


@require_safe
@api_throttle.budget_headers
def not_supportive(request):
    """The rows, with a manifest of what a download of them would contain.

    The manifest is here rather than only on the file because **a download owes
    a manifest before the press**, not a receipt after it: the page knows every
    number already, and a supplier pressing *Download CSV* should know how many
    rows they are asking for.

    ``?gene=`` and ``?include_limited=`` narrow this and the download
    identically, so the count on the button is always the count in the file.
    """
    rows, references, scope_line, err = _rows(request)
    if err:
        return err
    response = JsonResponse({
        'scope': scope_line,
        'gene': ','.join(_genes(request) or []),
        'include_limited': _include_limited(request),
        'note': NS.LIST_NOTE,
        'scope_note': NS.SCOPE_NOTE,
        # Read off the rows below, never counted separately.
        'manifest': NS.summary(rows, would_add=_would_add(request, rows)),
        # Keyed by UPPER-CASE gene, one entry per application that has a
        # supportive example. Drawn once per gene section rather than on every
        # card: it is a fact about the gene, and repeating it under each
        # failing product would read as a fact about that product.
        'references': references,
        'antibodies': rows,
    })
    response['Vary'] = 'X-API-Key'
    response['Cache-Control'] = 'private, no-cache'
    return response


def _would_add(request, rows):
    """How many more antibodies the *Limited support* tick would show.

    ``None`` once the tick is already on — there is nothing left to add, and a
    page told "0 more" would be drawing a control that does nothing next to a
    number implying it might.

    It costs a second pass over the same scope, and that is the honest price of
    putting the number on the screen before the press rather than after it. The
    alternative — count it in the browser from a list that does not contain the
    rows being counted — cannot be done at all.
    """
    if _include_limited(request):
        return None
    consumer, company_ids, allowed_genes, err = _scope(request)
    if err:
        return None
    genes = _genes(request)
    wider = NS.rows(company_ids=company_ids,
                    allowed_genes=genes or allowed_genes,
                    include_limited=True)
    return max(len(wider) - len(rows), 0)


@require_safe
@api_throttle.budget_headers
def not_supportive_csv(request):
    """One row per application finding."""
    rows, _references, scope_line, err = _rows(request)
    if err:
        return err
    body = NS.to_csv(rows)
    response = HttpResponse(body, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = (
        f'attachment; filename="{_filename("csv")}"')
    response['Vary'] = 'X-API-Key'
    response['Cache-Control'] = 'private, no-store'
    return response
