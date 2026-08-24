"""
OGA Data Feed API — core/api_views.py
======================================

Phase 3 rewrite: all antibody/gene data reads come from pipeline PostgreSQL.
APIConsumer and ReviewedAntibody stay in core/academy_db (unchanged).

API response JSON shape from _serialise_antibody() is preserved exactly.

Endpoints:
    GET  /api/v1/                 — machine-readable catalogue (core/api_manifest.py)
    GET  /api/v1/manifest/        — download manifest of image URLs (core/api_manifest.py)
    GET  /api/v1/antibodies/      — returns antibodies (filtered by consumer type)
    GET  /api/v1/genes/           — returns genes with recommendation status
    GET  /api/v1/gene-detail/     — returns ALL antibodies for a gene (competitor view)
    GET  /api/v1/status/          — health check / consumer info
    GET/PUT /api/v1/portal-config/ — retrieve/save portal preferences
    POST /api/v1/mark-reviewed/   — update last_queried_at (mark data as reviewed)
    POST /api/v1/report-issue/    — send structured issue report email
    GET/POST /api/v1/reviewed/    — per-antibody review tracking
    DELETE   /api/v1/reviewed/clear/ — clear all per-antibody reviews

Authentication:
    Header: X-API-Key: <uuid>

Two things about ``last_queried_at``, because one field was doing three jobs:

  * It is the **review cursor** — the ``since`` for the delta feed and the
    portal's "new since review" marker. Only ``/v1/antibodies/`` and
    ``/v1/mark-reviewed/`` move it, and ``/v1/antibodies/`` now takes
    ``?advance_cursor=false`` so a machine client can read the delta without
    consuming it. A client that fails mid-parse used to lose that delta for
    good.
  * It is **no longer the abuse guard**. Throttling is per consumer in
    ``core/api_throttle.py`` and applies to every endpoint here, including
    ``?preview=true``, which used to skip the only check there was — while
    being the exact request the portal's own front end makes on connect.
"""

import json
from datetime import datetime

from django.core.mail import send_mail
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_safe
from django.db.models import Q

# Pipeline models — primary data source
from pipeline.models import (
    Target, Antibody as PipelineAntibody, Company as PipelineCompany,
    PublicationImage, Report,
)

# Core models that stay in academy_db
from .models import APIConsumer, ReviewedAntibody

from . import api_throttle, api_usage
from pipeline.public import (public_targets, published_antibodies,
                             published_figures)
# Clonality from the enum and `is_recombinant` together — see
# pipeline/services/clonality.py.
from pipeline.services import clonality as clonality_svc

from .recommendations import (SCOPE_NOTE, curated_gene_ids,
                              recommendations_for)


RATE_LIMIT_SECONDS = 3600  # 1 hour
BASE_URL = 'https://onlygoodantibodies.co.uk'

# There is no tier logic left anywhere in this API. `APIConsumer.tier` is still
# a column and `/v1/status/` still reports it, because dropping a field is a
# migration against live academy_db that buys nothing — but nothing reads it to
# decide anything. If you are about to add a `tier ==` test, that is a product
# decision the owner has already made the other way.


# ─────────────────────────────────────────────────────────
# Authentication (reads from academy_db — unchanged)
# ─────────────────────────────────────────────────────────

def _authenticate(request):
    api_key = request.META.get('HTTP_X_API_KEY', '').strip()
    if not api_key:
        return None, JsonResponse(
            {'error': 'Missing X-API-Key header'},
            status=401,
        )
    try:
        consumer = APIConsumer.objects.get(api_key=api_key, is_active=True)
    except APIConsumer.DoesNotExist:
        return None, JsonResponse(
            {'error': 'Invalid or inactive API key'},
            status=403,
        )
    return consumer, None


# ─────────────────────────────────────────────────────────
# Guard: authenticate + throttle, in that order
# ─────────────────────────────────────────────────────────

def _guard(request):
    """``(consumer, error)`` — the opening lines of every endpoint.

    Throttling is applied to **everything**, ``?preview=true`` included. The old
    check was skipped for preview, and preview is what the portal requests on
    connect, so the guarded path was the one nobody used.

    The budget headers are stashed on the request and applied by the
    ``@api_throttle.budget_headers`` decorator, so no endpoint has to remember
    them on each of its exits.

    **It also counts the request** (``core/api_usage.py``), on both paths, which
    is why the counting lives here rather than in each endpoint: this is the one
    line every endpoint already goes through, and a counter wired into eleven
    views is a counter missing from the twelfth. A keyless request is counted
    without its caller being identified — the baseline has to include the
    unauthenticated traffic, or the effect of ever opening this up cannot be
    measured against anything.
    """
    consumer, err = _authenticate(request)
    if err:
        # A request with a bad key used to be refused and counted nowhere: the
        # per-consumer counters are keyed on a consumer it does not have. So the
        # one unmetered path into this API was the unauthenticated one, and each
        # attempt cost a lookup against the database that holds the logins.
        anon_refusal = api_throttle.check_anonymous(request)
        if anon_refusal is None:
            # Allowed through the anonymous ceiling, then refused for want of a
            # key. Counted: today every one of these is a 401 or 403, which is
            # exactly the "how many are knocking" baseline, and the same rows
            # keep counting them if they ever start succeeding.
            api_usage.record(request, None)
        return None, anon_refusal or err

    headers, refused = api_throttle.check(consumer)
    api_throttle.stash(request, headers)
    if refused:
        return None, refused

    api_usage.record(request, consumer)
    return consumer, None


def _guard_optional(request):
    """``(consumer or None, error)`` — the guard for the endpoints needing no key.

    ``/antibodies/`` and ``/genes/`` publish what the public gene pages already
    publish, so a key was never protecting the data; anyone who wanted it could
    read it off the pages (owner, 12 Aug 2026). It was metering and attribution,
    and both survive: a key still works, still identifies the caller, still
    applies their supplier filter and still moves their cursor.

    **A key that is sent is still checked.** Falling back to anonymous access on
    a bad key would be the worst of both — a manufacturer whose key expired
    would silently start receiving *every* supplier's rows instead of their own,
    with a 200 and nothing said. Wrong keys are refused exactly as before; only
    the absence of one is now allowed through.

    ``/gene-detail/`` deliberately keeps ``_guard`` (owner's decision, 12 Aug):
    it groups a gene's antibodies by vendor with a per-supplier recommended
    tally, which is the one thing here that is not already on a page.
    """
    if request.META.get('HTTP_X_API_KEY', '').strip():
        return _guard(request)

    refusal = api_throttle.check_anonymous(request)
    if refusal:
        return None, refusal
    # Counted with no caller — `_guard` has recorded keyless requests since it
    # was written, precisely so the effect of opening this up is measurable
    # against a baseline rather than guessed at.
    api_usage.record(request, None)
    return None, None


def _cache_headers(response, consumer):
    """How long a shared cache may hold this reply, and what it varies on.

    ``Vary`` is the load-bearing half and the easy one to leave out. The body
    depends on the key — a manufacturer's supplier filter, a demo account's
    gene list — so without it an edge cache can serve one consumer's rows to
    another, or a keyed consumer the anonymous full set. It is set on both
    branches for that reason, not only on the cacheable one.

    Keyless replies get the ``public, max-age=300`` the other two keyless
    endpoints already use, so repeat traffic is answered by the CDN instead of
    by a dyno — which is what makes opening these up cheaper than leaving them
    keyed, rather than dearer.
    """
    response['Vary'] = 'X-API-Key'
    response['Cache-Control'] = (
        'private, no-cache' if consumer else 'public, max-age=300')
    return response


# ─────────────────────────────────────────────────────────
# The delta gate on /v1/antibodies/
#
# Not an abuse guard — that is api_throttle. This is the contract of the
# incremental feed: one delta per hour, so a consumer polling in a loop cannot
# consume its own cursor. It answers with Retry-After, because a machine client
# that is told when to come back does, and one that is told "try again in 42
# minutes" in prose has to parse English to find out.
# ─────────────────────────────────────────────────────────

def _check_rate_limit(consumer):
    if consumer.last_queried_at:
        seconds_since = (timezone.now() - consumer.last_queried_at).total_seconds()
        if seconds_since < RATE_LIMIT_SECONDS:
            seconds_left = int(RATE_LIMIT_SECONDS - seconds_since)
            response = JsonResponse(
                {
                    'error': f'Rate limited. Try again in '
                             f'{max(1, seconds_left // 60)} minutes.',
                    'retry_after_seconds': seconds_left,
                    'hint': 'Add preview=true for the full set without the '
                            'delta gate, or advance_cursor=false to read the '
                            'delta without consuming it.',
                },
                status=429,
            )
            response['Retry-After'] = str(seconds_left)
            return response
    return None


# ─────────────────────────────────────────────────────────
# Helper: build the "since" cutoff (unchanged)
# ─────────────────────────────────────────────────────────

def parse_since(raw):
    """``(datetime, error)`` from a ``since=`` value. Always timezone-aware.

    The aware part is not decoration. Since Django 4.1 ``parse_datetime`` falls
    back to ``datetime.fromisoformat``, and on Python 3.11 that accepts a bare
    ``2020-01-01`` and returns it **naive** — so the ``make_aware`` branch below
    stopped being reached, and a plain date went into ``created_at__gt`` naive.
    Django warns and then interprets it in the current zone, which is a silent
    offset on a cursor comparison and a ``RuntimeWarning`` on every call. Nobody
    saw it because the endpoint answered, plausibly, with nearly the right rows.
    """
    parsed = parse_datetime(raw)
    if parsed is None:
        try:
            parsed = datetime.strptime(raw, '%Y-%m-%d')
        except (ValueError, TypeError):
            return None, JsonResponse(
                {'error': f'Invalid date format: {raw}. '
                          'Use YYYY-MM-DD or ISO 8601.'},
                status=400,
            )
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed, None


def _get_since(request, consumer):
    since_param = request.GET.get('since', '').strip()
    if since_param:
        return parse_since(since_param)
    return consumer.last_queried_at, None


# ─────────────────────────────────────────────────────────
# Helper: gene filter for demo accounts (reads consumer, queries pipeline)
# ─────────────────────────────────────────────────────────

def _get_allowed_genes(consumer):
    # No consumer is the keyless caller, who is scoped to nothing: the demo
    # gene filter is a property of a key, and having no key is not having a
    # narrow one. What they get is the published set, which is what the public
    # gene pages already draw.
    if consumer is None:
        return None
    gene_list = consumer.get_gene_filter_list()
    return gene_list if gene_list else None


def _apply_gene_filter_to_antibody_qs(qs, allowed_genes):
    """Filter pipeline Antibody queryset by allowed gene names."""
    if allowed_genes is None:
        return qs
    q = Q()
    for gene_name in allowed_genes:
        q |= Q(target__gene_name__iexact=gene_name)
    return qs.filter(q)


def _apply_gene_filter_to_target_qs(qs, allowed_genes):
    """Filter pipeline Target queryset by allowed gene names."""
    if allowed_genes is None:
        return qs
    q = Q()
    for gene_name in allowed_genes:
        q |= Q(gene_name__iexact=gene_name)
    return qs.filter(q)


# ─────────────────────────────────────────────────────────
# Helper: target-level has_recommendations
# ─────────────────────────────────────────────────────────

def _target_has_recommendations(target):
    """Has this gene been curated at all? One query, for one target.

    Kept because the gene page and the MCP connector ask it about a single
    target. **Do not call it in a loop** — that was one query per gene on every
    feed request. ``core.recommendations.curated_gene_ids`` answers the same question
    for a whole batch in one query, and the feeds use that.
    """
    return PipelineAntibody.objects.filter(target=target).filter(
        Q(wb_recommended=True) | Q(ip_recommended=True)
        | Q(if_recommended=True) | Q(fc_recommended=True)
    ).exists()


# ─────────────────────────────────────────────────────────
# Helper: resolve supplier filter to company PKs
# ─────────────────────────────────────────────────────────

def _get_supplier_company_ids(supplier_filter_str):
    """
    Convert the consumer's supplier_filter string (comma-separated canonical
    names) into pipeline Company PKs. Matches against display_name first,
    then name, so it works whether or not display_name has been populated.
    """
    suppliers = [s.strip() for s in supplier_filter_str.split(',') if s.strip()]
    if not suppliers:
        return None
    return list(
        PipelineCompany.objects.filter(
            Q(display_name__in=suppliers) | Q(name__in=suppliers)
        ).values_list('pk', flat=True)
    )


# ─────────────────────────────────────────────────────────
# Helper: get report link for a target
# ─────────────────────────────────────────────────────────

def _report_link_of(target):
    """The best report DOI on a target, read from prefetched rows.

    ``target.reports.filter(...)`` issues a fresh query even when ``reports``
    was prefetched, so in a loop it is one query per gene. Sorting the already
    loaded list in Python is the same answer for free — the ordering is the same
    ``-f1000_date, -zenodo_date``, with ``None`` sorting last the way the
    database puts NULLs last on a descending sort.
    """
    def key(report):
        return (report.f1000_date is not None, report.f1000_date,
                report.zenodo_date is not None, report.zenodo_date)

    candidates = [r for r in target.reports.all()
                  if (r.f1000_doi or '') or (r.zenodo_doi or '')]
    if not candidates:
        return None
    best = sorted(candidates, key=key, reverse=True)[0]
    return best.f1000_doi or best.zenodo_doi


def _get_report_link(target):
    """The report DOI for one target, for callers outside a loop.

    There was a fallback here onto ``core.Gene.f1000_report_link`` in the retired
    ``default`` SQLite. Measured before removing it on 23 Aug 2026: of the 155
    public targets, 154 carry a pipeline ``Report`` with a DOI and never reached
    it, and the one that does not (RAB5B) had no row in the legacy table either.
    It returned nothing for every public gene, so the feed is unchanged.
    """
    report = target.reports.filter(
        Q(f1000_doi__gt='') | Q(zenodo_doi__gt='')
    ).order_by('-f1000_date', '-zenodo_date').first()
    if report:
        return report.f1000_doi or report.zenodo_doi
    return None


# ─────────────────────────────────────────────────────────
# Helper: serialise a pipeline antibody
# JSON shape matches the pre-Phase-3 output exactly.
# ─────────────────────────────────────────────────────────

def _serialise_antibody(antibody, gene_rec_status, include_recs=True):
    """
    Build the JSON representation of a pipeline antibody.

    ``include_recs=False`` strips OGA's recommendations. **No caller in this app sets
    it any more**, and the reason it went is worth keeping: it was gated on
    ``consumer_type == 'manufacturer'``, so a registry account received
    ``recommendations: {}`` for all 1,645 rows — and the portal, which reads
    neither ``consumer_type`` nor ``gene_has_recommendations``, rendered that as
    "**0 recommended**" with every badge grey. A statement about the dataset,
    made from a fact about the account, with nothing on the page to tell them
    apart. The same recommendations are on the public gene pages with no gate at all
    (``core/views.py::gene_recommendations``), so withholding them here
    protected nothing and broke the one screen that showed them.

    The parameter stays because the MCP connector passes it explicitly and
    stripping recommendations is a coherent thing for a future caller to want.
    It is just not an entitlement tier.

    Two keys carry the recommendation and they answer different questions:

    ``recommendations``
        The raw booleans, unchanged since this API was written, because the
        portal and every existing integration read that shape.
    ``oga_recommendations``
        Three-valued, from ``core/recommendations.py``. ``False`` alone cannot
        say whether an antibody was tested and not recommended or never tested,
        and printing the first when you mean the second says a named commercial
        product failed testing nobody ran on it. Named for *whose* recommendation
        it is, because this codebase already distinguishes OGA's from the
        supplier's claims and the two are drawn side by side.

    A third key, ``verdicts``, was a deprecated alias of the second and was
    dropped on 7 Aug 2026. It was the wrong word — these are recommendations
    from testing under the consensus protocols, not settled judgements about a
    product — and it was safe to remove because no key had been issued to
    anybody outside OGA (owner). The portal read it and moved in the same
    commit; it was the only consumer.
    """
    # Supplier name: display_name (OGA canonical) > name (Access)
    supplier_name = None
    if antibody.company:
        supplier_name = antibody.company.display_name or antibody.company.name

    # Publication images → experiments array
    experiments = []
    tested_applications = set()
    for img in antibody.publication_images.all():
        tested_applications.add(img.application_type)
        experiments.append({
            'experiment_type': img.application_type,
            'experiment_type_display': img.get_application_type_display(),
            # Derive from storage so it stays correct after the R2 flip:
            # local storage → '/media/…' (prefix BASE_URL); R2 → absolute URL already.
            'image_url': (
                img.image.url if img.image.url.startswith('http')
                else f'{BASE_URL}{img.image.url}'
            ) if img.image else None,
        })

    result = {
        'antibody_name': antibody.catalogue_number,
        'gene': antibody.target.gene_name,
        'gene_has_recommendations': gene_rec_status if include_recs else None,
        'gene_page_url': f'{BASE_URL}/antibodies/{antibody.target.gene_name}/',
        'created_at': antibody.created_at.isoformat() if antibody.created_at else None,
        'metadata': {
            'rrid': antibody.rrid or None,
            'supplier': supplier_name,
            'host': antibody.host_species or None,
            # Both from services/clonality.py, which asks the enum and
            # `is_recombinant` together. Reading either alone was wrong in a
            # different direction per site, and recombinant-versus-not is a
            # field this API is asked to advise on.
            'clonality': clonality_svc.label(antibody),
            'clone_id': antibody.clone_id or None,
            # Boolean → string for backward compat: core stored "Yes"/null
            'recombinant': 'Yes' if clonality_svc.is_recombinant(antibody) else None,
            'product_link': antibody.supplier_url or None,
            'discontinued': antibody.out_of_market,
        },
        'recommendations': {},
        'oga_recommendations': {},
        'experiments': experiments,
        'embed_urls': None,
    }

    if include_recs:
        result['recommendations'] = {
            'WB': antibody.wb_recommended,
            'ICC-IF': antibody.if_recommended,
            'IP': antibody.ip_recommended,
            'FC': antibody.fc_recommended,
        }
        result['oga_recommendations'] = recommendations_for(
            antibody, tested_applications, bool(gene_rec_status))

    # Embed card URLs
    if antibody.rrid:
        embed_base = f'{BASE_URL}/embed/?rrid={antibody.rrid}'
    else:
        embed_base = f'{BASE_URL}/embed/?catalogue={antibody.catalogue_number}'

    result['embed_urls'] = {
        'all': embed_base,
        'WB': f'{embed_base}&application=WB',
        'IP': f'{embed_base}&application=IP',
        'ICC-IF': f'{embed_base}&application=ICC-IF',
        'FC': f'{embed_base}&application=FC',
    }

    return result


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/antibodies/
# ─────────────────────────────────────────────────────────

@require_safe
@api_throttle.budget_headers
def antibodies_feed(request):
    """
    Main data feed endpoint. Reads from pipeline PostgreSQL.

    ``?preview=true``  — the whole set, no delta gate, cursor untouched.
    ``?advance_cursor=false`` — take the delta and leave the cursor where it is,
    so a client that fails while parsing can ask for the same rows again. The
    default still advances, because existing integrations depend on it.
    """
    consumer, err = _guard_optional(request)
    if err:
        return err

    # A keyless caller has no cursor, so the whole delta apparatus is not
    # merely skipped for them — it does not exist. No cursor to gate on, none
    # to advance, and `since` only if they ask for one by hand. That is exactly
    # what `?preview=true` already meant, so it is the same branch rather than a
    # second one: the alternative is a keyless request quietly writing
    # `last_queried_at` on a consumer row it does not have.
    preview = request.GET.get('preview', '').lower() == 'true' or consumer is None
    advance_cursor = (
        consumer is not None
        and not preview
        and request.GET.get('advance_cursor', '').lower() != 'false'
    )

    if not preview:
        rate_err = _check_rate_limit(consumer)
        if rate_err:
            return rate_err

    if preview:
        # An explicit ?since= is still honoured with no key — it is a filter on
        # the data, not a read of anybody's cursor.
        since = None
        if consumer is None and request.GET.get('since', '').strip():
            since, err = parse_since(request.GET['since'].strip())
            if err:
                return err
    else:
        since, err = _get_since(request, consumer)
        if err:
            return err

    query_time = timezone.now()

    qs = (
        PipelineAntibody.objects
        .select_related('target', 'company')
        .prefetch_related('publication_images')
        .filter(publication_images__isnull=False)
        .distinct()
    )

    if since:
        qs = qs.filter(created_at__gt=since)

    # Demo account gene filter
    allowed_genes = _get_allowed_genes(consumer)
    qs = _apply_gene_filter_to_antibody_qs(qs, allowed_genes)

    # Manufacturer filter: restrict to their supplier(s)
    if (consumer is not None
            and consumer.consumer_type == 'manufacturer'
            and consumer.supplier_filter):
        company_ids = _get_supplier_company_ids(consumer.supplier_filter)
        if company_ids is not None:
            qs = qs.filter(company_id__in=company_ids)

    # Optional: filter by gene
    gene_param = request.GET.get('gene', '').strip()
    if gene_param:
        qs = qs.filter(target__gene_name__iexact=gene_param)

    # Optional: filter by application recommendation
    app_filter = request.GET.get('application', '').strip().upper()
    recommended_only = request.GET.get('recommended_only', '').lower() == 'true'

    # These two used to be forced off for anybody who was not a manufacturer,
    # which is why "Recommended only" and the application filter came back empty
    # on the portal rather than refusing: the control was drawn, the request was
    # accepted, and the server quietly ignored it.

    app_field_map = {
        'WB': 'wb_recommended',
        'ICC-IF': 'if_recommended',
        'IP': 'ip_recommended',
        'FC': 'fc_recommended',
    }
    if app_filter and app_filter in app_field_map:
        qs = qs.filter(**{app_field_map[app_filter]: True})

    if recommended_only:
        qs = qs.filter(
            Q(wb_recommended=True) | Q(if_recommended=True)
            | Q(ip_recommended=True) | Q(fc_recommended=True)
        )

    matched = qs.count()

    # Optional paging. Off by default: this feed's callers diff against what
    # they hold, and a page that does not say it is a page reads as the dataset
    # having shrunk.
    limit, offset, err = _paging(request)
    if err:
        return err
    if limit is not None or offset:
        qs = qs[offset:offset + limit] if limit is not None else qs[offset:]

    rows = list(qs)

    # One query for the whole page, not one per gene. This was
    # `_target_has_recommendations` inside the loop — cached per target, so it
    # was ~159 extra queries on the live set rather than 1,645, and still 159
    # more than it needs.
    curated = curated_gene_ids({ab.target_id for ab in rows})

    results = [
        _serialise_antibody(ab, ab.target_id in curated)
        for ab in rows
    ]

    if advance_cursor:
        consumer.last_queried_at = query_time
        consumer.save(update_fields=['last_queried_at'])

    complete = (limit is None and offset == 0)
    body = {
        # Null for a keyless caller rather than a made-up name like
        # "anonymous", which would be indistinguishable from a consumer somebody
        # had actually called that.
        'consumer': consumer.name if consumer else None,
        'consumer_type': consumer.consumer_type if consumer else None,
        'query_time': query_time.isoformat(),
        'since': since.isoformat() if since else None,
        'preview': preview,
        'count': len(results),
        'matched': matched,
        'complete': complete,
        'cursor_advanced': advance_cursor,
        # What the recommendations below do and do not cover. On the envelope
        # rather than on each row, for the reason `SCOPE_NOTE`'s own docstring
        # gives — a caveat on every row is a caveat nobody reads.
        'recommendation_scope': SCOPE_NOTE,
        'antibodies': results,
    }
    if not complete:
        body['truncated'] = {
            'offset': offset, 'limit': limit,
            'warning': 'This is a page of the feed, not all of it. A URL absent '
                       'from this response has not necessarily been removed.',
        }

    return _cache_headers(JsonResponse(body), consumer)


def _paging(request):
    """``(limit, offset, error)`` from the query string; both optional."""
    raw = (request.GET.get('limit') or '').strip()
    limit = None
    if raw:
        try:
            limit = max(1, int(raw))
        except ValueError:
            return None, 0, JsonResponse(
                {'error': f'limit must be a whole number; got {raw!r}.'},
                status=400)
    try:
        offset = max(0, int(request.GET.get('offset') or 0))
    except ValueError:
        return None, 0, JsonResponse(
            {'error': 'offset must be a whole number.'}, status=400)
    return limit, offset, None


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/genes/
# ─────────────────────────────────────────────────────────

@require_safe
@api_throttle.budget_headers
def genes_feed(request):
    """
    Returns all genes (targets) with their recommendation status.

    A catalogue, not an event feed — and it now behaves like the one its own
    first line has always claimed it is.

    Needs no key. It reads no cursor and consumes none, so there was nothing
    here for a key to protect that the gene pages do not already publish.
    """
    consumer, err = _guard_optional(request)
    if err:
        return err

    # No delta gate here, deliberately. This endpoint *checked* last_queried_at
    # and never set it, while antibodies_feed set it — so asking for antibodies
    # and then genes inside the hour returned 429 for the genes and the reverse
    # order worked. It reads no cursor and consumes none; the throttle above is
    # what protects it.
    #
    # And it no longer *defaults* to that cursor either. `_get_since` falls back
    # to last_queried_at when no ?since= is given, so every consumer that had
    # ever called the antibodies feed got `{"count": 0, "genes": []}` from an
    # endpoint whose own docstring says it returns all genes — and an empty list
    # cannot be told from a broken endpoint by the client receiving it. A delta
    # is still available by asking for one.
    since = None
    since_param = request.GET.get('since', '').strip()
    if since_param:
        since, err = parse_since(since_param)
        if err:
            return err

    # `public_targets()` and not "every named Target". The difference is not
    # cosmetic: on live it is 159 against 582. A Target row means "we intend to
    # work on this gene", and importing a site's target list adds hundreds for
    # work that has not started — so the 423 extra rows were gene names with
    # `antibody_count: 0` handed to manufacturers and registries, telling them
    # what OGA plans to characterise next.
    #
    # `pipeline/public.py` is the one definition of a public gene and its own
    # docstring says these must not reach the site, the extension or the MCP
    # dataset. This API was simply never added to that list, so it disagreed
    # with the homepage counter, with the MCP connector and with the browser
    # extension about how many genes OGA has data on.
    targets = (
        public_targets()
        .prefetch_related('antibodies__company', 'antibodies__publication_images', 'reports')
    )

    allowed_genes = _get_allowed_genes(consumer)
    targets = _apply_gene_filter_to_target_qs(targets, allowed_genes)

    if since:
        targets = targets.filter(antibodies__created_at__gt=since).distinct()

    has_rec_filter = request.GET.get('has_recommendations', '').lower()

    targets = list(targets)
    curated = curated_gene_ids({t.pk for t in targets})

    results = []
    for target in targets:
        has_rec = target.pk in curated

        if has_rec_filter == 'true' and not has_rec:
            continue
        if has_rec_filter == 'false' and has_rec:
            continue

        # `len()` on the prefetched list rather than `.count()`. NOT for the
        # reason it looks like: a related manager's `.count()` *does* use the
        # prefetch cache on this Django, so the version this replaced issued no
        # extra queries and swapping it saved nothing. Measured, because the
        # folklore says otherwise and a comment claiming a saving that is not
        # there is worse than no comment. It stays as `len()` only because a
        # reader should not have to know that rule to see that the list is
        # already in memory. The two N+1s on this endpoint were real and are
        # elsewhere: `_target_has_recommendations` per gene, and a
        # `Gene.objects.get` per gene in the report-link fallback — the latter
        # gone entirely now that the legacy fallback is retired.
        #
        # The PUBLISHED antibodies, not every antibody on the gene — filtered in
        # Python for the same reason the count is taken that way, so the whole
        # thing still costs no extra query.
        #
        # This is the gene-set defect above, one level down and left in place
        # when that one was fixed: the feed stopped listing 423 genes with
        # nothing behind them, and went on reporting 275 unpublished antibodies
        # spread across the 159 that remained. SOD1 was `antibody_count: 29`
        # against the 11 rows `/gene-detail/` returns for it and the 11 on the
        # gene page — one API contradicting itself about one gene, in the
        # direction that flatters us, to an audience of manufacturers and
        # registries. Summed, the feed claimed 1,920 antibodies while its own
        # `/status/` manifest said 1,645.
        antibodies = [ab for ab in target.antibodies.all()
                      if ab.publication_images.all()]
        antibody_count = len(antibodies)
        experiment_count = sum(
            len(ab.publication_images.all()) for ab in antibodies
        )

        f1000_link = _report_link_of(target)

        gene_data = {
            'gene': target.gene_name,
            'gene_page_url': f'{BASE_URL}/antibodies/{target.gene_name}/',
            'f1000_report': f1000_link,
            'has_recommendations': has_rec,
            'antibody_count': antibody_count,
            'experiment_count': experiment_count,
        }

        # Unconditional: these counts were withheld from anyone who was not a
        # manufacturer, and they are on the public gene pages already.
        rec_summary = {'WB': 0, 'ICC-IF': 0, 'IP': 0, 'FC': 0}
        for ab in antibodies:
            if ab.wb_recommended:
                rec_summary['WB'] += 1
            if ab.if_recommended:
                rec_summary['ICC-IF'] += 1
            if ab.ip_recommended:
                rec_summary['IP'] += 1
            if ab.fc_recommended:
                rec_summary['FC'] += 1
        gene_data['recommendations_by_application'] = rec_summary

        results.append(gene_data)

    return _cache_headers(JsonResponse({
        'consumer': consumer.name if consumer else None,
        'count': len(results),
        # Say which question was answered. An empty `genes` means something
        # different for a delta than for the catalogue, and the caller is the
        # one who has to tell them apart.
        'since': since.isoformat() if since else None,
        'mode': 'incremental' if since else 'full',
        'recommendation_scope': SCOPE_NOTE,
        'genes': results,
    }), consumer)


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/gene-detail/?gene=ACSL5
# ─────────────────────────────────────────────────────────

@require_safe
@api_throttle.budget_headers
def gene_detail(request):
    """
    Returns ALL antibodies for a given gene. Intel+ tier only.
    """
    consumer, err = _guard(request)
    if err:
        return err

    # The competitor view shows one gene's antibodies across every supplier,
    # grouped by vendor. That is commercially sensitive in a way the rest of
    # this API is not, so the gate is WHO IS ASKING, not what they pay: only a
    # manufacturer sees it, and they see their own products named among the
    # others. It was gated on tier and not on type at all, which is the wrong
    # axis twice over — a registry on a high tier got the full competitor
    # breakdown, and it was the one endpoint that never applied the
    # entitlement rule the other three did.
    if consumer.consumer_type != 'manufacturer':
        return JsonResponse(
            {'error': 'The competitor view is available to antibody '
                      'manufacturers only.',
             'your_consumer_type': consumer.consumer_type},
            status=403,
        )

    gene_name = request.GET.get('gene', '').strip()
    if not gene_name:
        return JsonResponse(
            {'error': 'Missing required parameter: ?gene='},
            status=400,
        )

    try:
        target = Target.objects.get(gene_name__iexact=gene_name)
    except Target.DoesNotExist:
        return JsonResponse(
            {'error': f'Gene not found: {gene_name}'},
            status=404,
        )

    allowed_genes = _get_allowed_genes(consumer)
    if allowed_genes is not None:
        if not any(target.gene_name.lower() == ag.lower() for ag in allowed_genes):
            return JsonResponse(
                {'error': f'Gene not available on your current plan: {gene_name}'},
                status=403,
            )

    qs = (
        PipelineAntibody.objects
        .select_related('target', 'company')
        .prefetch_related('publication_images')
        .filter(target=target, publication_images__isnull=False)
        .distinct()
    )

    target_rec_status = _target_has_recommendations(target)

    results = []
    supplier_summary = {}

    for ab in qs:
        serialised = _serialise_antibody(ab, target_rec_status)
        results.append(serialised)

        supplier = serialised['metadata'].get('supplier') or 'Unknown'
        if supplier not in supplier_summary:
            supplier_summary[supplier] = {'count': 0, 'recommended': 0}
        supplier_summary[supplier]['count'] += 1
        recs = serialised.get('recommendations', {})
        if any(recs.get(app) for app in ['WB', 'ICC-IF', 'IP', 'FC']):
            supplier_summary[supplier]['recommended'] += 1

    consumer_suppliers = []
    if consumer.supplier_filter:
        consumer_suppliers = [s.strip() for s in consumer.supplier_filter.split(',') if s.strip()]

    return JsonResponse({
        'consumer': consumer.name,
        'gene': target.gene_name,
        'gene_page_url': f'{BASE_URL}/antibodies/{target.gene_name}/',
        'has_recommendations': target_rec_status,
        'total_antibodies': len(results),
        'consumer_suppliers': consumer_suppliers,
        'supplier_summary': supplier_summary,
        'recommendation_scope': SCOPE_NOTE,
        'antibodies': results,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/status/
# ─────────────────────────────────────────────────────────

def pending_antibodies_for(consumer):
    """How many published antibodies this consumer has not seen yet.

    Their scope (supplier and gene) narrowed to what has appeared since their
    delta cursor — the number ``/v1/status/`` reports to them as
    ``pending_antibodies``, and the one the pipeline's own consumer page shows
    the owner.

    **It is a function because two screens ask it.** A partner told 37 waiting
    by their own portal and 41 by the internal page would see the disagreement
    rather than the better answer, and this is exactly the shape that has bitten
    this repo before — a figure written out by hand in a second place, agreeing
    on the day it was written and drifting afterwards.
    """
    qs = published_antibodies()
    if consumer.last_queried_at:
        qs = qs.filter(created_at__gt=consumer.last_queried_at)

    if consumer.consumer_type == 'manufacturer' and consumer.supplier_filter:
        company_ids = _get_supplier_company_ids(consumer.supplier_filter)
        if company_ids is not None:
            qs = qs.filter(company_id__in=company_ids)

    return _apply_gene_filter_to_antibody_qs(
        qs, _get_allowed_genes(consumer)).count()


@require_safe
@api_throttle.budget_headers
def api_status(request):
    """Health check and consumer info. No rate limit."""
    consumer, err = _guard(request)
    if err:
        return err

    allowed_genes = _get_allowed_genes(consumer)

    # Totals — scoped by gene_filter.
    #
    # Same three readers as the homepage counter and the `/genes/` feed
    # (`pipeline/public.py`), or `total_genes` says 582 while the list it is a
    # total OF returns 159. The gene half was fixed by pointing it at
    # `public_targets`; the antibody half was written out here by hand and
    # happened to agree, while the home page's copy of it deduped on catalogue
    # number and the `/genes/` feed's copy counted unpublished rows. Three
    # spellings of one number is two too many — none of them is imported now.
    base_target_qs = public_targets()
    base_ab_qs = published_antibodies()

    if allowed_genes is not None:
        base_target_qs = _apply_gene_filter_to_target_qs(base_target_qs, allowed_genes)
        base_ab_qs = _apply_gene_filter_to_antibody_qs(base_ab_qs, allowed_genes)

    gene_total = base_target_qs.count()
    ab_total = base_ab_qs.count()
    # Narrowed by `base_ab_qs` rather than counted whole, because this one is
    # scoped by the consumer's gene filter. Unscoped it is the home page's
    # `experiment_count` exactly.
    exp_total = published_figures().filter(antibody__in=base_ab_qs).count()

    return JsonResponse({
        'consumer': consumer.name,
        'consumer_type': consumer.consumer_type,
        'tier': consumer.tier,
        'supplier_filter': consumer.supplier_filter,
        'gene_filter': consumer.gene_filter,
        'last_queried_at': consumer.last_queried_at.isoformat() if consumer.last_queried_at else None,
        'pending_antibodies': pending_antibodies_for(consumer),
        'total_genes': gene_total,
        'total_antibodies': ab_total,
        'total_experiments': exp_total,
        'portal_config': consumer.portal_config or {},
        # The portal calls this on connect and nothing else, so it is where the
        # one-file download has to be announced — a browser cannot follow
        # /v1/download/ itself (the key is a header, not a query string, and
        # putting it in the URL would leave it in history and in every log), so
        # the page needs the object-storage URL to navigate to. Imported here
        # rather than at module level: api_manifest imports from this module.
        'bulk_download': _bulk_download_status(consumer),
    })


def _bulk_download_status(consumer):
    """The archive block, as the manifest reports it.

    Same reader, so the portal button and a programmatic client cannot disagree
    about whether there is a file to fetch or where it is.
    """
    from .api_manifest import bulk_download_status

    return bulk_download_status(consumer)


# ─────────────────────────────────────────────────────────
# Endpoint: GET/PUT /api/v1/portal-config/
# (reads/writes APIConsumer in academy_db — unchanged)
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'PUT'])
@api_throttle.budget_headers
def portal_config_update(request):
    consumer, err = _guard(request)
    if err:
        return err

    if request.method == 'GET':
        return JsonResponse({
            'consumer': consumer.name,
            'portal_config': consumer.portal_config or {},
        })

    try:
        config = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    if not isinstance(config, dict):
        return JsonResponse({'error': 'Config must be a JSON object'}, status=400)

    consumer.portal_config = config
    consumer.save(update_fields=['portal_config'])

    return JsonResponse({
        'status': 'saved',
        'portal_config': config,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: POST /api/v1/mark-reviewed/
# (writes APIConsumer in academy_db — unchanged)
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
@api_throttle.budget_headers
def mark_reviewed(request):
    consumer, err = _guard(request)
    if err:
        return err

    consumer.last_queried_at = timezone.now()
    consumer.save(update_fields=['last_queried_at'])

    return JsonResponse({
        'status': 'ok',
        'last_queried_at': consumer.last_queried_at.isoformat(),
    })


# ─────────────────────────────────────────────────────────
# Endpoint: POST /api/v1/report-issue/
# (reads APIConsumer from academy_db, sends email — unchanged)
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
@api_throttle.budget_headers
def report_issue(request):
    consumer, err = _guard(request)
    if err:
        return err

    # Ungated, by decision. Somebody telling us a recommendation looks wrong,
    # or that a product has been discontinued, is doing OGA a favour — charging
    # for the privilege made the dataset worse the more it was withheld.

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    catalogue = body.get('catalogue', '').strip()
    gene = body.get('gene', '').strip()
    rrid = body.get('rrid', '').strip()
    issue_type = body.get('issue_type', '').strip()
    details = body.get('details', '').strip()

    if not catalogue or not gene or not issue_type:
        return JsonResponse(
            {'error': 'Required fields: catalogue, gene, issue_type'},
            status=400,
        )

    type_labels = {
        'recommendation': 'Recommendation concern',
        'discontinued': 'Product discontinued',
        'technical': 'Technical image problem',
    }
    type_label = type_labels.get(issue_type, issue_type)

    subject = f'[OGA FLAG] {consumer.name} | {catalogue} | {gene} | {type_label}'

    message = (
        f'Company: {consumer.name}\n'
        f'Antibody: {catalogue}\n'
        f'Gene: {gene}\n'
        f'RRID: {rrid or "N/A"}\n'
        f'Issue type: {type_label}\n'
        f'\nDetails:\n{details or "(No details provided)"}\n'
        f'\nGene page: {BASE_URL}/antibodies/{gene}/\n'
        f'\nSent via OGA Data Portal'
    )

    config = consumer.portal_config or {}
    cc_email = config.get('contact_email', '').strip()
    cc_list = [cc_email] if cc_email else []

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email='onlygoodantibodies@gmail.com',
            recipient_list=['hsv6@leicester.ac.uk'],
            fail_silently=False,
        )
        if cc_list:
            send_mail(
                subject=subject,
                message=message,
                from_email='onlygoodantibodies@gmail.com',
                recipient_list=cc_list,
                fail_silently=True,
            )
    except Exception as e:
        return JsonResponse(
            {'error': f'Email sending failed: {str(e)}'},
            status=500,
        )

    return JsonResponse({
        'status': 'sent',
        'subject': subject,
        'cc': cc_list,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: GET/POST /api/v1/reviewed/
# (reads/writes ReviewedAntibody in academy_db — unchanged)
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'POST'])
@api_throttle.budget_headers
def reviewed_antibodies(request):
    consumer, err = _guard(request)
    if err:
        return err

    if request.method == 'GET':
        reviewed = list(
            ReviewedAntibody.objects
            .filter(consumer=consumer)
            .values_list('antibody_catalogue', flat=True)
        )
        return JsonResponse({'reviewed': reviewed})

    try:
        data = json.loads(request.body)
        catalogues = data.get('catalogues', [])
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    if not isinstance(catalogues, list):
        return JsonResponse({'error': 'catalogues must be a list'}, status=400)

    now = timezone.now()
    created_count = 0
    for cat in catalogues:
        cat = str(cat).strip()
        if not cat:
            continue
        _, created = ReviewedAntibody.objects.update_or_create(
            consumer=consumer,
            antibody_catalogue=cat,
            defaults={'reviewed_at': now},
        )
        if created:
            created_count += 1

    total = ReviewedAntibody.objects.filter(consumer=consumer).count()
    return JsonResponse({
        'saved': len(catalogues),
        'new': created_count,
        'total_reviewed': total,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: DELETE /api/v1/reviewed/clear/
# (writes ReviewedAntibody in academy_db — unchanged)
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['DELETE'])
@api_throttle.budget_headers
def clear_reviewed(request):
    consumer, err = _guard(request)
    if err:
        return err

    deleted_count, _ = ReviewedAntibody.objects.filter(consumer=consumer).delete()
    return JsonResponse({'deleted': deleted_count})
