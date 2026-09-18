"""Pre-release data for the supplier whose reagent it is.

The figure cropper no longer publishes: crops wait in the review queue
(``pipeline/services/review.py``) until somebody releases them. That gap is
useful to exactly one group of people outside the lab — **the manufacturer of
the reagent in the figure** — who are the only ones who can catch "that is the
discontinued version of the catalogue number", "that lot was withdrawn", "the
RRID is wrong" *before* it is on a public page with their product's name on it.

So these three endpoints exist, and the rule that shapes all of them is the one
in the title: **your own reagents, and nothing else.**

  ``GET /api/v1/pipeline-data/``       your figures awaiting release
  ``GET /api/v1/pipeline-image/?id=`` the bytes of one of them
  ``GET /api/v1/gene-progress/``       where the genes you have reagents in
                                       have got to, in reporting terms

Four decisions worth stating, because each one is a way this could leak.

**A key is required, and a *scoped* key.** ``core/api_views.py``'s feeds have a
keyless path — right, because everything they return is already on the public
website. Nothing here is. A consumer with no ``supplier_filter`` (a registry
account, say) is refused **by name and with the reason**, rather than being
handed the queue: "unscoped" is not a narrow scope, it is no scope, and the one
thing this must never do is show one manufacturer another's unpublished
figures.

**The image bytes come through here**, never as a bucket URL. Pending figures
are stored by ``pipeline/storages.py::attachment_storage`` — no public custom
domain, signed and short-lived — and an unreleased figure on a permanent public
URL is released to anyone who has the URL whatever the database says. The
gating is re-checked on the image request itself: a URL is not a permission.

**Provisional means provisional.** Every row says so, and the recommendation on
it is labelled as not yet applied to the antibody. A manufacturer who reads
"recommended" here and prints it on a product page would be quoting a verdict
OGA has not published; the envelope says that in one sentence rather than
burying it per row.

**Reporting status is scoped to their genes but not to their reagents.** Which
applications a gene has published figures for, and whether the report is out,
is public information about a public page — what is scoped is *which genes are
listed*, which is the question they asked ("where are the genes I have reagents
in?"). Their own awaiting-release count is theirs alone.
"""
from __future__ import annotations

import logging

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_safe

from pipeline.models import Antibody, PendingPublicationImage, Target
from pipeline.services import gene_reporting
from pipeline.services.review import PENDING

from . import api_throttle
from .api_views import (BASE_URL, _apply_gene_filter_to_antibody_qs,
                        _apply_gene_filter_to_target_qs, _get_allowed_genes,
                        _get_supplier_company_ids, _guard)

logger = logging.getLogger(__name__)

DB = "pipeline_db"

#: Said once on the envelope rather than on every row — a caveat on every row is
#: a caveat nobody reads (``core/recommendations.py::SCOPE_NOTE``'s own rule).
PRE_RELEASE_NOTE = (
    "These figures are NOT published. They have been cropped from a characterisation "
    "experiment and are waiting for OGA's review meeting, and they may change or "
    "be withdrawn before release. You are seeing them because they are figures of "
    "your own reagents, ahead of publication, so that you can tell us about a "
    "wrong catalogue number, a withdrawn lot or a mistaken RRID before the page "
    "goes live. Do not publish, quote or link them, and do not treat "
    "'recommended' here as an OGA recommendation — that verdict is not applied "
    "to the antibody until the figure is released. Once released, the same "
    "figure appears in /api/v1/manifest/ and on the public gene page."
)

NO_SCOPE = (
    "This endpoint returns unpublished data, so it is limited to a key that "
    "names the supplier whose reagents it may see. Your key has no supplier "
    "scope, which is not a narrow scope — it is none — so there is no set of "
    "reagents that would be yours to see before release. Published data is "
    "unaffected: /api/v1/antibodies/, /v1/manifest/ and /v1/download/ answer "
    "for your key as they always have. Email onlygoodantibodies@gmail.com to "
    "have a supplier scope set."
)


def _scope(request):
    """``(consumer, company_ids, error)`` — who is asking and what is theirs.

    The supplier scope is resolved once and shared by all three endpoints,
    because two copies of "which companies are yours" is how two surfaces come
    to answer one question differently.
    """
    consumer, err = _guard(request)
    if err:
        return None, None, err
    if not (consumer.supplier_filter or "").strip():
        return None, None, JsonResponse(
            {'error': 'No supplier scope on this key', 'detail': NO_SCOPE},
            status=403)
    company_ids = _get_supplier_company_ids(consumer.supplier_filter)
    if not company_ids:
        # The key names suppliers, and none of them matches a Company on file.
        # A named refusal, not an empty list: an empty list reads as "you have
        # no reagents here", which is a statement about the dataset made from a
        # fact about a configuration string.
        return None, None, JsonResponse(
            {'error': 'Supplier scope matches no supplier on file',
             'detail': (f"This key is scoped to {consumer.supplier_filter!r}, "
                        "which does not match any supplier name in the "
                        "dataset, so it cannot be used to decide what is "
                        "yours. Email onlygoodantibodies@gmail.com."),
             }, status=409)
    return consumer, company_ids, None


def _their_pending(company_ids, allowed_genes):
    qs = (PendingPublicationImage.objects.using(DB)
          .filter(status=PENDING, antibody__company_id__in=company_ids)
          .select_related('antibody', 'antibody__target', 'antibody__company'))
    if allowed_genes is not None:
        q = None
        from django.db.models import Q
        for gene in allowed_genes:
            part = Q(antibody__target__gene_name__iexact=gene)
            q = part if q is None else (q | part)
        qs = qs.filter(q) if q is not None else qs.none()
    return qs


def _row(item, published_pairs):
    ab = item.antibody
    target = ab.target if ab.target_id else None
    return {
        # The handle for /v1/pipeline-image/. `image_url` beside it is the whole
        # absolute URL for a programmatic client; a browser client builds its
        # own from this id against whichever host it is talking to.
        'id': item.pk,
        'catalogue_number': ab.catalogue_number or '',
        'rrid': ab.rrid or '',
        'gene': (target.gene_name if target else '') or '',
        'supplier': (ab.company.display_name or ab.company.name) if ab.company_id else '',
        'application': item.application_type,
        'status': 'awaiting_release',
        # Named for what it is. `recommended: true` alone, on a row a supplier
        # might paste into a product page, would be OGA appearing to recommend
        # something it has not published.
        'provisional_recommendation': bool(item.recommended),
        'replaces_a_published_figure': (
            (item.antibody_id, item.application_type) in published_pairs),
        'staged_at': item.updated_at.isoformat() if item.updated_at else None,
        'image_url': f'{BASE_URL}/api/v1/pipeline-image/?id={item.pk}',
    }


@require_safe
@api_throttle.budget_headers
def pipeline_data(request):
    """Figures of the caller's own reagents that are cropped and not yet public."""
    consumer, company_ids, err = _scope(request)
    if err:
        return err

    allowed_genes = _get_allowed_genes(consumer)
    qs = _their_pending(company_ids, allowed_genes)

    gene_param = (request.GET.get('gene') or '').strip()
    if gene_param:
        qs = qs.filter(antibody__target__gene_name__iexact=gene_param)

    items = list(qs)
    from pipeline.services import review as review_svc
    published_pairs = review_svc.published_pairs(items)
    rows = [_row(i, published_pairs) for i in items]

    genes = sorted({r['gene'] for r in rows if r['gene']})
    return JsonResponse({
        'consumer': consumer.name,
        'count': len(rows),
        'genes': genes,
        'note': PRE_RELEASE_NOTE,
        'figures': rows,
    })


@require_safe
@api_throttle.budget_headers
def pipeline_image(request):
    """One pre-release figure's bytes, re-checked against the caller's scope.

    The check is repeated here rather than trusted from the listing: a URL is
    not a permission, and this one is handed to an outside party.
    """
    consumer, company_ids, err = _scope(request)
    if err:
        return err

    try:
        pk = int((request.GET.get('id') or '').strip())
    except ValueError:
        return JsonResponse(
            {'error': 'id must be a whole number — it is the id from '
                      'figures[].image_url in /api/v1/pipeline-data/.'}, status=400)

    allowed_genes = _get_allowed_genes(consumer)
    item = _their_pending(company_ids, allowed_genes).filter(pk=pk).first()
    if item is None or not item.image:
        # One answer for "no such figure" and "not yours", deliberately: telling
        # a caller that a figure exists but belongs to somebody else is itself a
        # fact about another manufacturer's unpublished work.
        return JsonResponse(
            {'error': 'No pre-release figure of yours with that id.'}, status=404)
    try:
        handle = item.image.open('rb')
    except Exception:
        logger.exception('pending figure %s could not be opened for %s',
                         pk, consumer.name)
        return JsonResponse(
            {'error': 'That figure could not be read from storage. The record '
                      'is fine; the file behind it is not.'}, status=502)
    return FileResponse(handle, filename=item.image.name.rsplit('/', 1)[-1])


@require_safe
@api_throttle.budget_headers
def gene_progress(request):
    """Where the genes the caller has reagents in have got to.

    "Reagents in the system" means every antibody of theirs on file, not only
    the ones with published figures — a gene they supplied an antibody for that
    is still being worked on is exactly the case they are asking about.
    """
    consumer, company_ids, err = _scope(request)
    if err:
        return err

    allowed_genes = _get_allowed_genes(consumer)
    theirs = _apply_gene_filter_to_antibody_qs(
        Antibody.objects.using(DB).filter(company_id__in=company_ids),
        allowed_genes)
    pairs = list(theirs.values_list('target_id', 'pk'))
    target_ids = {tid for tid, _ in pairs if tid}
    their_ab_ids = [ab for _, ab in pairs]
    mine_per_target = {}
    for tid, _ab in pairs:
        if tid:
            mine_per_target[tid] = mine_per_target.get(tid, 0) + 1

    targets = list(_apply_gene_filter_to_target_qs(
        Target.objects.using(DB).filter(pk__in=target_ids), allowed_genes))
    status = gene_reporting.reporting_status(
        targets, pending_antibody_ids=their_ab_ids)

    gene_param = (request.GET.get('gene') or '').strip()
    rows = []
    for target in targets:
        row = dict(status.get(target.pk) or {})
        if not row:
            continue
        if gene_param and (row['gene'] or '').lower() != gene_param.lower():
            continue
        row['your_antibodies'] = mine_per_target.get(target.pk, 0)
        if row.get('public_path'):
            row['public_url'] = f"{BASE_URL}{row.pop('public_path')}"
        else:
            row.pop('public_path', None)
            row['public_url'] = None
        rows.append(row)
    rows.sort(key=lambda r: r['gene'])

    return JsonResponse({
        'consumer': consumer.name,
        'count': len(rows),
        # What each field is derived from, said once. Every one of these is a
        # record that exists — none is a status somebody typed — which is the
        # whole reason the number can be trusted and also the reason it can move
        # without anybody "updating" anything.
        'note': (
            "Derived from records, not from a status field: a gene has a public "
            "page when an antibody for it carries a published figure, an "
            "application is published when a figure for it is on the site, and a "
            "report is 'published' when it carries a Zenodo DOI or a published "
            "F1000 date. 'awaiting_release' counts YOUR reagents' figures that "
            "are cropped and not yet public; see /api/v1/pipeline-data/."),
        'genes': rows,
    })
