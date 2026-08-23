"""
OGA Data Feed API — core/api_views.py
======================================

Drop this file into core/api_views.py

Endpoints:
    GET  /api/v1/antibodies/      — returns antibodies (filtered by consumer type)
    GET  /api/v1/genes/           — returns genes with recommendation status
    GET  /api/v1/gene-detail/     — returns ALL antibodies for a gene (competitor view)
    GET  /api/v1/status/          — health check / consumer info (no rate limit)
    GET/PUT /api/v1/portal-config/ — retrieve/save portal preferences
    POST /api/v1/mark-reviewed/   — update last_queried_at (mark data as reviewed)
    POST /api/v1/report-issue/    — send structured issue report email
    GET/POST /api/v1/reviewed/    — per-antibody review tracking
    DELETE   /api/v1/reviewed/clear/ — clear all per-antibody reviews

Authentication:
    Header: X-API-Key: <uuid>

Query parameters:
    ?since=2026-03-01           — override: get everything after this date
    ?recommended_only=true      — manufacturers only: filter to recommended antibodies
    ?application=WB             — filter by application type (WB, IP, ICC-IF, FC)
    ?gene=CTSB                  — filter by gene name
    ?preview=true               — portal mode: skip rate limit, don't update last_queried_at

Gene filter (demo accounts):
    APIConsumer.gene_filter is a comma-separated list of gene names.
    When set, the consumer can ONLY access those genes via any endpoint.
    Cleared when converting demo → paid.

Recommendation visibility:
    Manufacturer consumers see recommendations for their own antibodies
    and for all antibodies via gene-detail (intel+ tier).
    RRID consumers receive metadata and images but NO recommendations.
    This protects the interpretation layer — OGA's core commercial value.
"""

import json
from datetime import datetime

from django.core.mail import send_mail
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from django.db.models import Q, Exists, OuterRef

from .models import Gene, Antibody, Experiment, Description, APIConsumer, ReviewedAntibody


# Rate limit for heavy endpoints: minimum seconds between calls per consumer
RATE_LIMIT_SECONDS = 3600  # 1 hour


# ─────────────────────────────────────────────────────────
# Authentication (no rate limit — used by all endpoints)
# ─────────────────────────────────────────────────────────

def _authenticate(request):
    """
    Authenticate via X-API-Key header.
    Returns (consumer, None) on success or (None, JsonResponse) on failure.
    """
    api_key = request.META.get('HTTP_X_API_KEY', '').strip()
    if not api_key:
        return None, JsonResponse(
            {'error': 'Missing X-API-Key header'},
            status=401
        )
    try:
        consumer = APIConsumer.objects.get(api_key=api_key, is_active=True)
    except APIConsumer.DoesNotExist:
        return None, JsonResponse(
            {'error': 'Invalid or inactive API key'},
            status=403
        )
    return consumer, None


# ─────────────────────────────────────────────────────────
# Rate limiting (applied only to heavy endpoints)
# ─────────────────────────────────────────────────────────

def _check_rate_limit(consumer):
    """
    Returns None if OK, or a JsonResponse if rate limited.
    Only call this from endpoints that do heavy DB reads/writes.
    """
    if consumer.last_queried_at:
        seconds_since = (timezone.now() - consumer.last_queried_at).total_seconds()
        if seconds_since < RATE_LIMIT_SECONDS:
            minutes_left = int((RATE_LIMIT_SECONDS - seconds_since) / 60)
            return JsonResponse(
                {'error': f'Rate limited. Try again in {minutes_left} minutes.'},
                status=429
            )
    return None


# ─────────────────────────────────────────────────────────
# Helper: build the "since" cutoff
# ─────────────────────────────────────────────────────────

def _get_since(request, consumer):
    """
    Determine the cutoff datetime.
    Priority: ?since= param > consumer.last_queried_at > None (return all)
    """
    since_param = request.GET.get('since', '').strip()
    if since_param:
        parsed = parse_datetime(since_param)
        if parsed is None:
            # Try date-only format
            try:
                parsed = timezone.make_aware(
                    datetime.strptime(since_param, '%Y-%m-%d')
                )
            except ValueError:
                return None, JsonResponse(
                    {'error': f'Invalid date format: {since_param}. Use YYYY-MM-DD or ISO 8601.'},
                    status=400
                )
        return parsed, None
    return consumer.last_queried_at, None  # None = return everything


# ─────────────────────────────────────────────────────────
# Helper: get allowed gene names for a consumer
# ─────────────────────────────────────────────────────────

def _get_allowed_genes(consumer):
    """
    Returns a list of allowed gene names if the consumer has a gene_filter,
    or None if unrestricted. Uses the model's get_gene_filter_list() method.
    """
    gene_list = consumer.get_gene_filter_list()
    return gene_list if gene_list else None


def _apply_gene_filter_to_antibody_qs(qs, allowed_genes):
    """
    Filter an Antibody queryset to only include antibodies whose gene
    name is in the allowed list. Case-insensitive matching.
    Returns the queryset unchanged if allowed_genes is None.
    """
    if allowed_genes is None:
        return qs
    # Build case-insensitive Q filter across all allowed gene names
    q = Q()
    for gene_name in allowed_genes:
        q |= Q(gene__name__iexact=gene_name)
    return qs.filter(q)


def _apply_gene_filter_to_gene_qs(qs, allowed_genes):
    """
    Filter a Gene queryset to only include genes whose name is in
    the allowed list. Case-insensitive matching.
    Returns the queryset unchanged if allowed_genes is None.
    """
    if allowed_genes is None:
        return qs
    q = Q()
    for gene_name in allowed_genes:
        q |= Q(name__iexact=gene_name)
    return qs.filter(q)


# ─────────────────────────────────────────────────────────
# Helper: compute gene-level has_recommendations
# ─────────────────────────────────────────────────────────

def _gene_has_recommendations(gene):
    """
    Returns True if ANY antibody for this gene has ANY application
    set to True. This is the signal that tells consumers whether
    absence of a recommendation is informative or not.
    """
    return Description.objects.filter(
        antibody__gene=gene
    ).filter(
        Q(wb_app=True) | Q(icc_if_app=True) | Q(ip_app=True) | Q(fc_app=True)
    ).exists()


# ─────────────────────────────────────────────────────────
# Helper: serialise an antibody with full metadata
# ─────────────────────────────────────────────────────────

BASE_URL = 'https://onlygoodantibodies.co.uk'


def _serialise_antibody(antibody, gene_rec_status, include_recs=True):
    """
    Build the JSON representation of an antibody with all metadata
    a manufacturer or RRID would need.

    include_recs: if False, recommendations are omitted entirely.
    RRID consumers should never receive recommendation data.
    """
    desc = getattr(antibody, 'description', None)

    # Gather experiment images
    experiments = []
    for exp in antibody.experiments.all():
        exp_data = {
            'experiment_type': exp.experiment_type,
            'experiment_type_display': exp.get_experiment_type_display(),
        }
        if exp.file_path:
            exp_data['image_url'] = f'{BASE_URL}/media/{exp.file_path}'
        else:
            exp_data['image_url'] = None
        experiments.append(exp_data)

    result = {
        'antibody_name': antibody.name,
        'gene': antibody.gene.name,
        'gene_has_recommendations': gene_rec_status if include_recs else None,
        'gene_page_url': f'{BASE_URL}/antibodies/{antibody.gene.name}/',
        'created_at': antibody.created_at.isoformat() if hasattr(antibody, 'created_at') and antibody.created_at else None,
        'metadata': {},
        'recommendations': {},
        'experiments': experiments,
        'embed_urls': None,
    }

    if desc:
        result['metadata'] = {
            'rrid': desc.rrid,
            'supplier': desc.supplier,
            'host': desc.host,
            'clonality': desc.clonality,
            'clone_id': desc.clone_ID,
            'recombinant': desc.recombinant,
            'product_link': desc.product_link,
            'discontinued': desc.discontinued,
        }

        # Only include recommendations for authorised consumers
        if include_recs:
            result['recommendations'] = {
                'WB': desc.wb_app,
                'ICC-IF': desc.icc_if_app,
                'IP': desc.ip_app,
                'FC': desc.fc_app,
            }
        # else: recommendations stays as empty dict {}

        # Embed card URLs — prefer RRID lookup, fall back to catalogue number
        if desc.rrid:
            embed_base = f'{BASE_URL}/embed/?rrid={desc.rrid}'
        else:
            embed_base = f'{BASE_URL}/embed/?catalogue={antibody.name}'

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

@require_GET
def antibodies_feed(request):
    """
    Main data feed endpoint.

    Manufacturer consumers: returns antibodies from their supplier,
    added since their last query (or ?since= override).
    Includes recommendations.

    RRID consumers: returns all antibodies added since last query,
    regardless of vendor. Does NOT include recommendations.

    Optional filters:
        ?recommended_only=true  — only return antibodies with at least
                                  one True recommendation
        ?application=WB         — filter to antibodies with a specific
                                  application recommended
        ?gene=CTSB              — filter to a specific gene
        ?since=2026-03-01       — override the "since last query" cutoff
        ?preview=true           — portal mode: skip rate limit and
                                  last_queried_at update

    Rate limited: 1 call per hour per consumer (unless preview=true).
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    # Preview mode: portal browsing — no rate limit, no last_queried_at update
    preview = request.GET.get('preview', '').lower() == 'true'

    if not preview:
        # Rate limit heavy endpoints
        rate_err = _check_rate_limit(consumer)
        if rate_err:
            return rate_err

    # Determine cutoff
    # In preview mode, ignore since/last_queried_at — return everything
    if preview:
        since = None
    else:
        since, err = _get_since(request, consumer)
        if err:
            return err

    # Record the query time BEFORE fetching (so nothing falls between cracks)
    query_time = timezone.now()

    # Start with all antibodies, prefetch related data
    qs = Antibody.objects.select_related(
        'gene', 'description'
    ).prefetch_related('experiments').all()

    # Apply "since" filter if we have a cutoff
    if since:
        qs = qs.filter(created_at__gt=since)

    # Apply consumer gene_filter (demo accounts)
    allowed_genes = _get_allowed_genes(consumer)
    qs = _apply_gene_filter_to_antibody_qs(qs, allowed_genes)

    # Manufacturer filter: restrict to their supplier(s)
    # supplier_filter supports comma-separated values for parent companies
    # e.g. "Bio-Techne,Bio-Techne (Novus Biologicals),Bio-Techne (R&D Systems)"
    if consumer.consumer_type == 'manufacturer' and consumer.supplier_filter:
        suppliers = [s.strip() for s in consumer.supplier_filter.split(',') if s.strip()]
        if len(suppliers) == 1:
            qs = qs.filter(description__supplier=suppliers[0])
        else:
            qs = qs.filter(description__supplier__in=suppliers)

    # Optional: filter by gene (URL param — further narrows within allowed genes)
    gene_param = request.GET.get('gene', '').strip()
    if gene_param:
        qs = qs.filter(gene__name__iexact=gene_param)

    # Optional: filter by application recommendation
    # RRID consumers cannot use these filters — diffing filtered vs
    # unfiltered results would reconstruct the recommendation matrix
    app_filter = request.GET.get('application', '').strip().upper()
    recommended_only = request.GET.get('recommended_only', '').lower() == 'true'

    if consumer.consumer_type != 'manufacturer':
        app_filter = ''
        recommended_only = False

    app_field_map = {
        'WB': 'description__wb_app',
        'ICC-IF': 'description__icc_if_app',
        'IP': 'description__ip_app',
        'FC': 'description__fc_app',
    }
    if app_filter and app_filter in app_field_map:
        qs = qs.filter(**{app_field_map[app_filter]: True})

    if recommended_only:
        qs = qs.filter(
            Q(description__wb_app=True) |
            Q(description__icc_if_app=True) |
            Q(description__ip_app=True) |
            Q(description__fc_app=True)
        )

    # Determine whether this consumer should see recommendations
    include_recs = (consumer.consumer_type == 'manufacturer')

    # Build response — cache gene recommendation status to avoid repeated queries
    gene_rec_cache = {}
    results = []

    for ab in qs:
        gene_id = ab.gene_id
        if gene_id not in gene_rec_cache:
            gene_rec_cache[gene_id] = _gene_has_recommendations(ab.gene)

        results.append(_serialise_antibody(ab, gene_rec_cache[gene_id], include_recs=include_recs))

    # Only update last_queried_at for non-preview calls
    if not preview:
        consumer.last_queried_at = query_time
        consumer.save(update_fields=['last_queried_at'])

    return JsonResponse({
        'consumer': consumer.name,
        'consumer_type': consumer.consumer_type,
        'query_time': query_time.isoformat(),
        'since': since.isoformat() if since else None,
        'preview': preview,
        'count': len(results),
        'antibodies': results,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/genes/
# ─────────────────────────────────────────────────────────

@require_GET
def genes_feed(request):
    """
    Returns all genes with their recommendation status.
    Useful for RRID to understand the full landscape.

    Manufacturer consumers see full recommendation counts per application.
    RRID consumers see has_recommendations boolean only — no per-app breakdown.

    Optional filters:
        ?has_recommendations=true  — only genes with at least one recommendation
        ?since=2026-03-01          — genes that have had antibodies added since

    Rate limited: 1 call per hour per consumer.
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    # Rate limit heavy endpoints
    rate_err = _check_rate_limit(consumer)
    if rate_err:
        return rate_err

    since, err = _get_since(request, consumer)
    if err:
        return err

    genes = Gene.objects.prefetch_related(
        'antibodies__description',
        'antibodies__experiments'
    ).all()

    # Apply consumer gene_filter (demo accounts)
    allowed_genes = _get_allowed_genes(consumer)
    genes = _apply_gene_filter_to_gene_qs(genes, allowed_genes)

    # Optional: only genes with new antibodies since cutoff
    if since:
        genes = genes.filter(
            antibodies__created_at__gt=since
        ).distinct()

    # Optional: filter to genes that have recommendations
    has_rec_filter = request.GET.get('has_recommendations', '').lower()

    # Determine whether this consumer should see recommendation detail
    include_recs = (consumer.consumer_type == 'manufacturer')

    results = []
    for gene in genes:
        has_rec = _gene_has_recommendations(gene)

        if has_rec_filter == 'true' and not has_rec:
            continue
        if has_rec_filter == 'false' and has_rec:
            continue

        antibody_count = gene.antibodies.count()
        experiment_count = sum(
            ab.experiments.count() for ab in gene.antibodies.all()
        )

        gene_data = {
            'gene': gene.name,
            'gene_page_url': f'{BASE_URL}/antibodies/{gene.name}/',
            'f1000_report': gene.f1000_report_link,
            'has_recommendations': has_rec,
            'antibody_count': antibody_count,
            'experiment_count': experiment_count,
        }

        # Only include per-application breakdown for manufacturers
        if include_recs:
            rec_summary = {'WB': 0, 'ICC-IF': 0, 'IP': 0, 'FC': 0}
            for ab in gene.antibodies.all():
                desc = getattr(ab, 'description', None)
                if desc:
                    if desc.wb_app:
                        rec_summary['WB'] += 1
                    if desc.icc_if_app:
                        rec_summary['ICC-IF'] += 1
                    if desc.ip_app:
                        rec_summary['IP'] += 1
                    if desc.fc_app:
                        rec_summary['FC'] += 1
            gene_data['recommendations_by_application'] = rec_summary

        results.append(gene_data)

    return JsonResponse({
        'consumer': consumer.name,
        'count': len(results),
        'genes': results,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/gene-detail/?gene=ACSL5
# ─────────────────────────────────────────────────────────

@require_GET
def gene_detail(request):
    """
    Returns ALL antibodies for a given gene, regardless of supplier.
    Used by the portal competitor view (intel tier and above).
    Always includes recommendations — this is the paid intel product.

    Required parameter:
        ?gene=ACSL5         — gene name (case-insensitive)

    No rate limit — lightweight per-gene query.
    No last_queried_at update — read-only browsing.
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    # Require intel tier or above
    tier_level = {'free': 0, 'data': 1, 'intel': 2, 'full': 3}
    if tier_level.get(consumer.tier, 0) < 2:
        return JsonResponse(
            {'error': 'Gene detail requires intel tier or above.'},
            status=403
        )

    gene_name = request.GET.get('gene', '').strip()
    if not gene_name:
        return JsonResponse(
            {'error': 'Missing required parameter: ?gene='},
            status=400
        )

    try:
        gene = Gene.objects.get(name__iexact=gene_name)
    except Gene.DoesNotExist:
        return JsonResponse(
            {'error': f'Gene not found: {gene_name}'},
            status=404
        )

    # Check consumer gene_filter — demo accounts can only view allowed genes
    allowed_genes = _get_allowed_genes(consumer)
    if allowed_genes is not None:
        if not any(gene.name.lower() == ag.lower() for ag in allowed_genes):
            return JsonResponse(
                {'error': f'Gene not available on your current plan: {gene_name}'},
                status=403
            )

    # Get ALL antibodies for this gene (no supplier filter)
    qs = Antibody.objects.select_related(
        'gene', 'description'
    ).prefetch_related('experiments').filter(gene=gene)

    gene_rec_status = _gene_has_recommendations(gene)

    # Build response, grouped by supplier
    # Always include recs — intel+ tier is the paid competitor view
    results = []
    supplier_summary = {}

    for ab in qs:
        serialised = _serialise_antibody(ab, gene_rec_status, include_recs=True)
        results.append(serialised)

        # Track supplier counts
        supplier = (ab.description.supplier if hasattr(ab, 'description') and ab.description else 'Unknown')
        if supplier not in supplier_summary:
            supplier_summary[supplier] = {'count': 0, 'recommended': 0}
        supplier_summary[supplier]['count'] += 1
        recs = serialised.get('recommendations', {})
        if any(recs.get(app) for app in ['WB', 'ICC-IF', 'IP', 'FC']):
            supplier_summary[supplier]['recommended'] += 1

    # Identify which suppliers belong to this consumer
    consumer_suppliers = []
    if consumer.supplier_filter:
        consumer_suppliers = [s.strip() for s in consumer.supplier_filter.split(',') if s.strip()]

    return JsonResponse({
        'consumer': consumer.name,
        'gene': gene.name,
        'gene_page_url': f'{BASE_URL}/antibodies/{gene.name}/',
        'has_recommendations': gene_rec_status,
        'total_antibodies': len(results),
        'consumer_suppliers': consumer_suppliers,
        'supplier_summary': supplier_summary,
        'antibodies': results,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/status/
# ─────────────────────────────────────────────────────────

@require_GET
def api_status(request):
    """
    Health check and consumer info.
    Shows what the consumer will get on their next query.
    Also returns portal_config for portal use.
    No rate limit — lightweight read-only check.
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    # Count what they'd get since last query
    since = consumer.last_queried_at
    qs = Antibody.objects.all()
    if since:
        qs = qs.filter(created_at__gt=since)
    if consumer.consumer_type == 'manufacturer' and consumer.supplier_filter:
        suppliers = [s.strip() for s in consumer.supplier_filter.split(',') if s.strip()]
        if len(suppliers) == 1:
            qs = qs.filter(description__supplier=suppliers[0])
        else:
            qs = qs.filter(description__supplier__in=suppliers)

    # Apply consumer gene_filter (demo accounts)
    allowed_genes = _get_allowed_genes(consumer)
    qs = _apply_gene_filter_to_antibody_qs(qs, allowed_genes)

    # Gene/antibody/experiment totals — also scoped by gene_filter
    if allowed_genes is not None:
        gene_total = Gene.objects.all()
        gene_total = _apply_gene_filter_to_gene_qs(gene_total, allowed_genes).count()
        ab_total = _apply_gene_filter_to_antibody_qs(Antibody.objects.all(), allowed_genes).count()
        exp_total = Experiment.objects.filter(
            antibody__in=_apply_gene_filter_to_antibody_qs(Antibody.objects.all(), allowed_genes)
        ).count()
    else:
        gene_total = Gene.objects.count()
        ab_total = Antibody.objects.count()
        exp_total = Experiment.objects.count()

    return JsonResponse({
        'consumer': consumer.name,
        'consumer_type': consumer.consumer_type,
        'tier': consumer.tier,
        'supplier_filter': consumer.supplier_filter,
        'gene_filter': consumer.gene_filter,
        'last_queried_at': consumer.last_queried_at.isoformat() if consumer.last_queried_at else None,
        'pending_antibodies': qs.count(),
        'total_genes': gene_total,
        'total_antibodies': ab_total,
        'total_experiments': exp_total,
        'portal_config': consumer.portal_config or {},
    })


# ─────────────────────────────────────────────────────────
# Endpoint: GET/PUT /api/v1/portal-config/
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'PUT'])
def portal_config_update(request):
    """
    GET: retrieve current portal configuration.
    PUT: save portal configuration (URL pattern, filename convention, etc.).

    Accepts JSON body for PUT:
    {
        "url_pattern": "https://images.example.com/val/{catalogue}_{application}.png",
        "filename_pattern": "{catalogue}_{application}",
        "image_format": "png",
        "contact_name": "...",
        "contact_email": "..."
    }
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    if request.method == 'GET':
        return JsonResponse({
            'consumer': consumer.name,
            'portal_config': consumer.portal_config or {},
        })

    # PUT — save configuration
    try:
        config = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    # Validate: must be a dict
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
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
def mark_reviewed(request):
    """
    Marks the consumer's data as reviewed by updating last_queried_at.
    Called from the portal when the manufacturer has finished reviewing
    their current batch. Next portal visit will show new items since
    this timestamp.
    """
    consumer, err = _authenticate(request)
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
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
def report_issue(request):
    """
    Sends a structured issue report email.

    Called from the portal (full tier only). Sends email from
    onlygoodantibodies@gmail.com to hsv6@leicester.ac.uk,
    CC's the contact_email from the consumer's portal_config.

    Expects JSON body:
    {
        "catalogue": "MAB4364*",
        "gene": "SYT1",
        "rrid": "AB_2199304",
        "issue_type": "recommendation|discontinued|technical",
        "details": "Optional description of the issue"
    }
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    # Require full tier
    tier_level = {'free': 0, 'data': 1, 'intel': 2, 'full': 3}
    if tier_level.get(consumer.tier, 0) < 3:
        return JsonResponse(
            {'error': 'Issue reporting requires the full tier.'},
            status=403
        )

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
            status=400
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

    # CC the consumer's contact email if set in portal_config
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
        # Send CC separately (send_mail doesn't support CC directly)
        if cc_list:
            send_mail(
                subject=subject,
                message=message,
                from_email='onlygoodantibodies@gmail.com',
                recipient_list=cc_list,
                fail_silently=True,  # Don't fail if CC bounces
            )
    except Exception as e:
        return JsonResponse(
            {'error': f'Email sending failed: {str(e)}'},
            status=500
        )

    return JsonResponse({
        'status': 'sent',
        'subject': subject,
        'cc': cc_list,
    })


# ─────────────────────────────────────────────────────────
# Endpoint: GET/POST /api/v1/reviewed/
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def reviewed_antibodies(request):
    """
    Per-antibody review tracking for portal consumers.

    GET:  Returns list of antibody catalogue numbers this consumer
          has individually marked as reviewed.
    POST: Bulk upsert reviewed antibodies from portal session.
          Body: {"catalogues": ["cat1", "cat2", ...]}
          Idempotent — re-posting the same catalogue just updates reviewed_at.
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    if request.method == 'GET':
        reviewed = list(
            ReviewedAntibody.objects
            .filter(consumer=consumer)
            .values_list('antibody_catalogue', flat=True)
        )
        return JsonResponse({'reviewed': reviewed})

    # POST — bulk upsert
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
            defaults={'reviewed_at': now}
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
# ─────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['DELETE'])
def clear_reviewed(request):
    """
    Clears all individually reviewed antibodies for this consumer.
    The last_queried_at timestamp is not affected.
    """
    consumer, err = _authenticate(request)
    if err:
        return err

    deleted_count, _ = ReviewedAntibody.objects.filter(consumer=consumer).delete()
    return JsonResponse({'deleted': deleted_count})