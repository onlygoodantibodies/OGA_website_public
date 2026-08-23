"""The programmatic route: a manifest of image URLs, not the images.

A consumer wanting every figure OGA has published does not want us to build them
a zip. The media bucket is public by design — ``AWS_QUERYSTRING_AUTH = False``
plus a Cloudflare custom domain, because the public antibody pages serve figures
straight off it — so every image already has a permanent URL that any HTTP
client can fetch, in parallel, resumably, from a CDN, without touching this
dyno. The portal's own "Download Selected" has always worked that way: JSZip
fetches each image *in the browser* and zips it there.

What a machine needs from us is therefore not bytes. It is an accurate,
complete, verifiable list of what exists and where it is — and a cheap way to
ask again later whether anything has changed.

Why the default is the **whole** manifest, not a delta
------------------------------------------------------
Because deletions are not recorded anywhere. A ``PublicationImage`` that is
removed, or an antibody whose figure is replaced, leaves no tombstone — so a
delta feed can only ever say what arrived, never what went. A client syncing off
deltas alone accumulates files that are no longer part of the dataset and has no
way to find out.

The whole manifest sidesteps it: the client diffs against what it already holds
and both directions fall out, additions and removals alike. The cost of doing
that on every reconnect is what ``ETag`` removes — an unchanged dataset answers
``304 Not Modified`` with no body, which is cheaper than any delta request would
have been. The fingerprint covers image identity, file name and every
recommendation flag in scope, so a **replaced** figure and a changed
recommendation both break the ETag, which a timestamp cursor would miss.

``?since=`` exists for callers who specifically want the additions, and the
response says in as many words that it cannot report removals or replacements.
It is the optimisation, not the contract.

Why the response is complete or says it is not
----------------------------------------------
A truncated manifest is worse than no manifest, because a diffing client reads
absence as deletion and removes files that are still in the dataset. So the full
set is returned by default, and any request that limits it comes back with
``complete: false`` and the count it left out. Never let a cap be silent — that
rule is written down in CLAUDE.md about board pagination, and it is load-bearing
here for a different and sharper reason.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_safe

from pipeline.models import PublicationImage

from . import api_throttle, recommendations as R
from .api_views import (
    BASE_URL, _get_allowed_genes, _get_supplier_company_ids, _guard, _paging,
    parse_since,
)

# 2 since 7 Aug 2026: the body gained `scope.requested_genes` and the
# `sync.narrowed_by_request` / `narrowed_note` pair when `?gene=` was added.
# Additive, but this constant exists to say the body shape moved.
MANIFEST_VERSION = 2

# Default filename pattern, matching the portal's Settings panel default so a
# consumer who set one there gets the same names from both routes.
DEFAULT_FILENAME_PATTERN = '{gene}_{catalogue}_{application}'

# Illegal on Windows; the browser zip strips exactly these.
_ILLEGAL_IN_FILENAME = re.compile(r'[*?"<>|:/\\]')

_APP_DISPLAY = dict(PublicationImage.ApplicationType.choices)


# ─────────────────────────────────────────────────────────
# Scope — the same entitlement rules the rest of the API applies
# ─────────────────────────────────────────────────────────

def _all_images():
    """Every published figure in the public dataset, in a stable order.

    The order matters beyond tidiness: it is what the prepared archive is built
    in, so two builds of one dataset version produce the same zip.

    The set itself is ``pipeline.public.published_figures`` — the same rows the
    home page calls *tests* and ``/status/`` reports as ``total_experiments`` —
    so ``files_in_scope`` is a count of the same thing under a different name,
    by construction rather than by coincidence. Only the ordering is this
    module's own.
    """
    from pipeline.public import published_figures

    return (
        published_figures()
        .select_related('antibody', 'antibody__target', 'antibody__company')
        .order_by('antibody__target__gene_name', 'antibody__catalogue_number',
                  'application_type', 'pk')
    )


def _scope_of(consumer):
    """``(queryset, narrowed)`` — what this consumer may see, and whether it is
    less than the whole public dataset.

    The two answers come from one pass on purpose. ``is_full_scope`` decides
    whether a consumer may be redirected to the shared archive, which holds
    *every* supplier's figures — so a second function working the same thing out
    from the same fields is a scope leak waiting for the two to drift, and it
    would drift silently: both are plausible, only one is applied, and the
    difference is visible only in a zip somebody has already downloaded.

    Mirrors ``antibodies_feed``: the public boundary is a published figure, the
    demo gene filter narrows to named genes, and a manufacturer sees their own
    supplier's rows.
    """
    qs = _all_images()
    narrowed = False

    allowed_genes = _get_allowed_genes(consumer)
    if allowed_genes is not None:
        from django.db.models import Q
        gene_q = Q()
        for gene_name in allowed_genes:
            gene_q |= Q(antibody__target__gene_name__iexact=gene_name)
        qs = qs.filter(gene_q)
        narrowed = True

    if consumer.consumer_type == 'manufacturer' and consumer.supplier_filter:
        company_ids = _get_supplier_company_ids(consumer.supplier_filter)
        if company_ids is not None:
            qs = qs.filter(antibody__company_id__in=company_ids)
            narrowed = True

    return qs, narrowed


def _scoped_images(consumer):
    """Every published figure this consumer is entitled to, as a queryset."""
    return _scope_of(consumer)[0]


def if_none_match_matches(header, etag):
    """Does an ``If-None-Match`` header match our tag? RFC 9110 §13.1.2.

    This was ``header == etag``, exact string equality, and it meant **no
    partner ever got a 304 from production** (found 7 Aug 2026 by running
    ``bin/check_api.py`` against the live API with a real key; every local test
    passed).

    A cache or proxy that changes the entity body — Cloudflare compressing the
    response, in our case — is *required* to weaken the tag it forwards, so the
    client stores ``W/"abc-json"``, sends that back, and an exact comparison
    against ``"abc-json"`` fails. The reply is a full 200 and the whole 1.5 MB
    manifest again, every reconnect, which is the exact cost the ETag exists to
    remove and which `API.md` promises it removes.

    The spec says ``If-None-Match`` uses the **weak** comparison function, so
    ``W/"x"`` and ``"x"`` are a match; it is a comma-separated list; and ``*``
    matches any current representation. None of the three worked before.
    """
    if not header:
        return False
    header = header.strip()
    if header == '*':
        return True

    def opaque(tag):
        tag = tag.strip()
        if tag[:2].upper() == 'W/':
            tag = tag[2:]
        return tag

    return opaque(etag) in {opaque(tag) for tag in header.split(',') if tag.strip()}


# ─────────────────────────────────────────────────────────
# Fingerprint — what makes the ETag change
# ─────────────────────────────────────────────────────────

def _fingerprint(qs, curated_ids):
    """A strong ETag over everything a client would need to re-download.

    Deliberately not a timestamp. ``PublicationImage.created_at`` is
    ``auto_now_add``, so replacing a figure in place leaves it untouched — a
    cursor would call that dataset unchanged while the file behind the URL is a
    different image. Hashing the stored file name catches it, because
    ``AWS_S3_FILE_OVERWRITE = False`` means a replacement lands on a new key.

    The recommendation flags are in the hash as well: one flipping changes what
    the manifest says about a product without changing any file, and the
    curated-gene set is included whole because one gene's first recommendation
    changes the answer for every *other* antibody against it (see
    ``core/recommendations.py``).
    """
    digest = hashlib.sha256()
    digest.update(f'v{MANIFEST_VERSION}\n'.encode())
    digest.update(('curated:' + ','.join(str(i) for i in sorted(curated_ids))
                   + '\n').encode())
    rows = qs.values_list(
        'pk', 'image', 'application_type',
        'antibody__wb_recommended', 'antibody__ip_recommended',
        'antibody__if_recommended', 'antibody__fc_recommended',
    ).order_by('pk')
    count = 0
    for row in rows.iterator():
        digest.update(('\x1f'.join(str(part) for part in row) + '\n').encode())
        count += 1
    return digest.hexdigest(), count


# ─────────────────────────────────────────────────────────
# Rows
# ─────────────────────────────────────────────────────────

def _absolute(image):
    """The permanent public URL for a stored figure.

    Derived from the storage backend rather than assembled by hand, so it stays
    correct on both sides of the R2 flip: local storage yields ``/media/…`` and
    needs the origin in front, R2 yields an absolute URL already.
    """
    if not image:
        return None
    url = image.url
    return url if url.startswith('http') else f'{BASE_URL}{url}'


def _extension_of(url):
    tail = url.rsplit('/', 1)[-1]
    return tail.rsplit('.', 1)[-1].lower() if '.' in tail else 'bin'


def _filename(pattern, row, extension):
    name = (pattern
            .replace('{gene}', row['gene'] or '')
            .replace('{catalogue}', row['catalogue_number'] or '')
            .replace('{rrid}', row['rrid'] or '')
            .replace('{application}', row['application'] or ''))
    name = _ILLEGAL_IN_FILENAME.sub('', name).strip() or f"image_{row['image_id']}"
    return f'{name}.{extension}'


def _rows(qs, consumer, curated_ids, include_recommendations, attach_images=False):
    """Serialise the scoped figures, one row per downloadable file.

    Filename collisions are resolved rather than ignored: a pattern that omits
    ``{application}`` gives one antibody's four figures one name, and the
    browser zip silently keeps whichever landed last. A suffix keeps all four,
    and the count is reported so the consumer can fix their pattern.

    ``attach_images`` hangs the ``FieldFile`` on each row as ``_image``, for the
    archive builder, which needs the bytes as well as the name. It is off by
    default and must stay off for anything that serialises: a model object in a
    row is a ``JsonResponse`` 500 that takes the whole response down and reads
    as data loss — the rule CLAUDE.md records about board rows, and the same
    dict shape reaches ``JsonResponse`` here.
    """
    config = consumer.portal_config or {}
    pattern = (config.get('filename_pattern') or '').strip() or DEFAULT_FILENAME_PATTERN

    rows = []
    seen_names = {}
    collisions = 0

    for image in qs:
        antibody = image.antibody
        target = antibody.target
        url = _absolute(image.image)
        if not url:
            continue

        company = antibody.company
        row = {
            'image_id': image.pk,
            'url': url,
            'application': image.application_type,
            'application_display': _APP_DISPLAY.get(
                image.application_type, image.application_type),
            'gene': target.gene_name,
            'catalogue_number': antibody.catalogue_number,
            'rrid': antibody.rrid or None,
            'supplier': (company.display_name or company.name) if company else None,
            'product_link': antibody.supplier_url or None,
            'discontinued': antibody.out_of_market,
            'gene_page_url': f'{BASE_URL}/antibodies/{target.gene_name}/',
            'added_at': image.created_at.isoformat() if image.created_at else None,
        }

        if include_recommendations:
            value = R.recommendation(
                antibody, image.application_type,
                {image.application_type}, target.pk in curated_ids)
            row['oga_recommendation'] = value

        filename = _filename(pattern, row, _extension_of(url))
        if filename in seen_names:
            collisions += 1
            stem, _, ext = filename.rpartition('.')
            # Step until the name is genuinely free, rather than trusting the
            # first suffix: a dataset can legitimately contain a figure already
            # called `X_2.png`, and handing two rows one name is the exact
            # silent overwrite this whole block exists to prevent.
            nth = 2
            while f'{stem}_{nth}.{ext}' in seen_names:
                nth += 1
            filename = f'{stem}_{nth}.{ext}'
        seen_names[filename] = True
        row['filename'] = filename

        if attach_images:
            row['_image'] = image.image

        rows.append(row)

    return rows, collisions


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/manifest/
# ─────────────────────────────────────────────────────────

CSV_COLUMNS = [
    'url', 'filename', 'gene', 'catalogue_number', 'rrid', 'supplier',
    'application', 'application_display', 'oga_recommendation',
    'product_link', 'discontinued', 'gene_page_url', 'image_id', 'added_at',
]


def rows_to_csv(rows):
    """The manifest as CSV text.

    One writer for the ``?format=csv`` response and for the ``manifest.csv``
    inside the prepared archive, so a reader who has both cannot find them
    disagreeing about a column.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


@require_safe
@api_throttle.budget_headers
def manifest(request):
    """A complete list of every figure this consumer may download.

    Never writes. Calling it does not advance any cursor, does not mark anything
    reviewed, and returns the same bytes for the same dataset however many times
    it is called — which is the whole difference between this and
    ``/v1/antibodies/``.
    """
    consumer, err = _guard(request)
    if err:
        return err

    fmt = (request.GET.get('format') or 'json').strip().lower()
    if fmt not in ('json', 'csv', 'urls'):
        return JsonResponse(
            {'error': f'Unknown format: {fmt}. Use json, csv or urls.'},
            status=400)

    qs = _scoped_images(consumer)

    # ?gene= narrows the request the way ?gene= narrows /download/, so a partner
    # mirroring one gene has an incremental route and not only a one-off zip.
    #
    # It narrows BEFORE the fingerprint on purpose. The narrowed set is what the
    # reply describes, so it is what the ETag must identify — the same argument
    # the format makes two comments down. A client alternating between the full
    # manifest and one gene would otherwise be told 304 and go on holding the
    # wrong set.
    genes = [g.strip() for g in (request.GET.get('gene') or '').split(',')
             if g.strip()]
    if genes:
        from django.db.models import Q

        gene_q = Q()
        for gene in genes:
            gene_q |= Q(antibody__target__gene_name__iexact=gene)
        qs = qs.filter(gene_q)

    since_raw = (request.GET.get('since') or '').strip()
    since = None
    if since_raw:
        since, err = parse_since(since_raw)
        if err:
            return err

    target_ids = set(qs.values_list('antibody__target_id', flat=True))
    curated_ids = R.curated_gene_ids(target_ids)

    # The fingerprint is always over the WHOLE scope, never the ?since= subset:
    # it identifies the dataset, so two clients on different cursors that hold
    # the same data must agree about its version.
    version, total_images = _fingerprint(qs, curated_ids)

    # An ETag identifies a *representation*, not a dataset, so the format is in
    # it: a client that cached the CSV and then asked for JSON with the same tag
    # would otherwise be told 304 and go on using CSV bytes as JSON.
    # `dataset_version` in the body stays format-free — that is the thing two
    # clients compare to decide whether they hold the same data.
    #
    # The gene selection is in it for the same reason. Two different narrowings
    # can produce the same fingerprint only if they hold the same files, but
    # relying on that is relying on a coincidence; naming the selection makes it
    # true by construction.
    scope_tag = ('-' + ','.join(sorted(g.lower() for g in genes))) if genes else ''
    etag = f'"{version}-{fmt}{scope_tag}"'

    if if_none_match_matches(request.headers.get('If-None-Match'), etag) and not since:
        response = HttpResponse(status=304)
        response['ETag'] = etag
        return response

    if since:
        qs = qs.filter(created_at__gt=since)

    limit, offset, err = _paging(request)
    if err:
        return err

    matched = qs.count()
    if limit is not None or offset:
        qs = qs[offset:offset + limit] if limit is not None else qs[offset:]

    # `complete` answers one question and it is not "was this response capped".
    # It is "does this hold every file in your scope" — so a ?since= delta is
    # NOT complete however few rows were dropped, because a client that diffs a
    # delta believing it complete deletes every file it did not mention. That is
    # the single most damaging thing this endpoint could get wrong.
    returned_all = (limit is None and offset == 0 and since is None)

    # Recommendations go to everyone. They are on the public gene pages with no
    # gate at all, so withholding them from an API consumer protected nothing —
    # and on the portal it produced "0 recommended" across the whole dataset,
    # which is a false statement about the data made from a fact about the
    # account.
    rows, collisions = _rows(qs, consumer, curated_ids, include_recommendations=True)

    if fmt == 'urls':
        body = '\n'.join(row['url'] for row in rows)
        response = HttpResponse(body + ('\n' if body else ''),
                                content_type='text/plain; charset=utf-8')
    elif fmt == 'csv':
        response = HttpResponse(rows_to_csv(rows),
                                content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="oga_manifest.csv"'
    else:
        response = JsonResponse(_envelope(
            consumer, rows, version, total_images, matched, returned_all,
            collisions, since, offset, limit, etag, genes))

    response['ETag'] = etag
    # The manifest is per consumer, so a shared cache must never serve one
    # consumer's scope to another.
    response['Cache-Control'] = 'private, no-cache'
    response['X-OGA-Dataset-Version'] = version
    response['X-OGA-Manifest-Complete'] = 'true' if returned_all else 'false'
    return response


def _envelope(consumer, rows, version, total_images, matched, returned_all,
              collisions, since, offset, limit, etag, genes=()):
    """The JSON body — the rows plus everything needed to sync against them."""
    body = {
        'manifest_version': MANIFEST_VERSION,
        'generated_at': timezone.now().isoformat(),

        # The value to send back as If-None-Match on the next reconnect. Changes
        # whenever any file, name or recommendation in scope changes.
        'dataset_version': version,

        'consumer': consumer.name,
        'consumer_type': consumer.consumer_type,
        'tier': consumer.tier,

        'scope': {
            'genes': _get_allowed_genes(consumer),
            'supplier_filter': consumer.supplier_filter or None,
            'includes_recommendations': True,
            'recommendation_scope': R.SCOPE_NOTE,
            # What ?gene= asked for, echoed. `complete` below is true for this
            # narrowing, which is exactly the shape that could have a client
            # delete its whole mirror — see `narrowed_by_request` in the sync
            # note for the guard.
            'requested_genes': list(genes) or None,
        },

        'counts': {
            'files_in_scope': total_images,
            'files_matched': matched,
            'files_returned': len(rows),
        },

        # A diffing client MUST read this before treating a missing URL as a
        # deletion. False means the response was capped and absence means
        # nothing.
        'complete': returned_all,

        'files': rows,
        'sync': _sync_note(since, returned_all, offset, limit, etag, genes),
        'bulk_download': _bulk_download_note(consumer, version),
    }

    if collisions:
        body['filename_collisions'] = collisions
        body['filename_collisions_note'] = (
            f'{collisions} file(s) would have been given a name already used by '
            'another figure, and were suffixed to keep them distinct. Include '
            '{application} in your filename pattern to avoid this.')

    return body


def _bulk_download_note(consumer, version=None):
    """Where to get everything in one file, told to a client already listing it.

    A prepared archive nothing mentions is the export-with-no-importer failure
    one direction over: routed, built nightly, and found by nobody. The manifest
    is the one response every programmatic consumer reads, and ``/v1/status/``
    is the only call the portal makes on connect, so both carry it.

    ``version`` is passed in when the caller already computed it (the manifest
    does, for its ETag) and worked out here otherwise. It is never cached: a
    stale fingerprint names the *previous* archive, which still exists until it
    is pruned, so caching would hand somebody a complete and readable zip of
    yesterday's dataset — the one failure the fingerprint key exists to make
    impossible.
    """
    from . import bulk_archive

    note = {'endpoint': f'{BASE_URL}/api/v1/download/'}
    if not is_full_scope(consumer):
        note['scope'] = 'built_per_request'
        note['note'] = (
            'Your key is scoped to part of the dataset, so the endpoint builds '
            'a zip of your own figures rather than redirecting to the shared '
            'archive.')
        return note

    if version is None:
        qs = _all_images()
        target_ids = set(qs.values_list('antibody__target_id', flat=True))
        version, _total = _fingerprint(qs, R.curated_gene_ids(target_ids))

    note['scope'] = 'whole_public_dataset'
    note.update(bulk_archive.status_for(version))
    return note


def bulk_download_status(consumer):
    """The archive block for a caller with no fingerprint to hand."""
    return _bulk_download_note(consumer)


def _sync_note(since, returned_all, offset, limit, etag, genes=()):
    """What this response can and cannot tell a client about change.

    ``reconnect_with`` carries the **literal header to send**, not a recipe for
    building one. It used to read ``If-None-Match: <dataset_version>``, and
    ``dataset_version`` is deliberately not the ETag — the ETag also names the
    representation (`-json`, `-csv`), because a client that cached the CSV and
    asked for JSON with the same tag would otherwise be told 304 and read CSV
    bytes as JSON. So a client following that instruction to the letter got 200
    for ever and re-downloaded the whole manifest every time, with nothing on
    either side reporting a fault: the endpoint answered, the data was right,
    and the one feature the instruction existed to enable silently never
    engaged. Give the value, not the formula.
    """
    note = {
        'mode': 'incremental' if since else 'full',
        'etag': etag,
        # The per-file identity, said out loud because getting it wrong is
        # silent. A re-cropped figure keeps its `filename` — that is built from
        # gene, catalogue number and application — and lands on a new object
        # key, so `url` is the only field that moves. A client that skips
        # anything it already has a file for will see the dataset change, pull
        # this manifest, and then keep the stale image indefinitely.
        'compare_on': 'url',
        'compare_note': (
            'To fetch only what you do not already hold, compare each file\'s '
            '`url` against the one you stored last time — not whether a file of '
            'that name exists. A replaced figure keeps its filename and changes '
            'its url.'),
        'reconnect_with': f'If-None-Match: {etag}',
        'reconnect_note': (
            'Send that header verbatim. It is not `dataset_version` — the ETag '
            'identifies this format as well as this data, so the two differ on '
            'purpose.'),
        # True means "a URL absent from this reply has been withdrawn". A
        # ?gene= reply is complete for the genes asked for, so deletions ARE
        # trackable — but only against a mirror of those genes. Point it at a
        # mirror of the whole dataset and every other gene looks withdrawn,
        # which is the mass deletion this endpoint's `complete` comment calls
        # the worst thing it could get wrong. So the narrowing is stated
        # beside the flag rather than left for a reader to infer.
        'deletions_tracked': not bool(since),
        'narrowed_by_request': bool(genes),
        'image_format': 'original',
        'narrowed_note': (
            'This reply covers only the gene(s) you asked for, so diff it '
            'against a mirror of those genes and nothing wider. Files for '
            'other genes are absent because you did not ask for them, not '
            'because they were withdrawn.'
        ) if genes else None,
        'image_format_note': (
            'Files are served exactly as stored. The portal\'s PNG/JPG '
            'conversion happens in the browser and is not applied here.'),
    }
    if since:
        note['since'] = since.isoformat()
        note['warning'] = (
            'An incremental manifest lists figures added after `since`. It '
            'cannot report figures that were removed, and it cannot report a '
            'figure whose image was replaced in place — the stored timestamp '
            'does not move when that happens. Pull the full manifest with '
            'If-None-Match to detect either.')
    else:
        note['detail'] = (
            'This is the complete set for your scope. Diff it against what you '
            'already hold: URLs that have gone are figures withdrawn from the '
            'dataset.')
    # Capped, as distinct from filtered. `returned_all` is False for a ?since=
    # delta too, and calling that "a page of the manifest" would be the wrong
    # explanation of the right flag.
    if limit is not None or offset:
        note['truncated'] = {
            'offset': offset, 'limit': limit,
            'warning': 'This response is a page of the manifest, not the whole '
                       'of it. Absence of a URL here does not mean it was '
                       'removed from the dataset.',
        }
    return note


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/download/  — the easy option
# ─────────────────────────────────────────────────────────

#: A zip built inside the request is held in memory and pushed through the web
#: dyno, so it is only ever offered for a set small enough that both are free.
#: Anything larger is what the prepared archive exists for.
STREAM_MAX_FILES = 400


def is_full_scope(consumer):
    """Whether this consumer sees the whole public dataset.

    Only a full-scope consumer can be handed the shared prepared archive: it
    holds every public figure, so giving it to a manufacturer scoped to their
    own catalogue would hand them every competitor's figures in one file. A
    narrowed consumer gets their own zip built on the spot, which is affordable
    precisely because their scope is narrow.

    Answered by the function that does the narrowing, never worked out again
    from the same fields — see ``_scope_of``.
    """
    return not _scope_of(consumer)[1]


def _readme_for(version, count):
    import textwrap

    from django.urls import reverse

    from . import bulk_archive

    return bulk_archive.README_TEMPLATE.format(
        version=version,
        built=timezone.now().strftime('%Y-%m-%d %H:%M UTC'),
        count=count,
        # Wrapped here rather than in the constant: SCOPE_NOTE is one sentence
        # for every surface, and only this one is a plain-text file opened in
        # whatever the reader's zip tool uses.
        scope=textwrap.fill(R.SCOPE_NOTE, 78),
        recommended=R.MEANINGS[R.RECOMMENDED],
        not_recommended=R.MEANINGS[R.NOT_RECOMMENDED],
        not_tested=R.MEANINGS[R.NOT_TESTED],
        manifest_url=f'{BASE_URL}{reverse("api:manifest")}',
        base_url=BASE_URL,
    )


def archive_inputs():
    """Everything ``manage.py build_bulk_archive`` needs, from one place.

    The command assembles none of this itself. If it did, the archive's
    ``manifest.csv``, its filenames and its version could all drift from what
    ``/v1/manifest/`` reports about the very same figures — and a reader joining
    the CSV to the files on ``filename`` would find rows that match nothing,
    with no error anywhere to explain it.
    """
    qs = _all_images()
    target_ids = set(qs.values_list('antibody__target_id', flat=True))
    curated_ids = R.curated_gene_ids(target_ids)
    version, total = _fingerprint(qs, curated_ids)

    rows, _collisions = _rows(qs, _ArchiveConsumer(), curated_ids,
                              include_recommendations=True, attach_images=True)
    return {
        'version': version,
        'rows': rows,
        'total': total,
        'manifest_csv': rows_to_csv(rows),
        'readme': _readme_for(version, len(rows)),
    }


class _ArchiveConsumer:
    """The unrestricted consumer the shared archive is built for.

    ``_rows`` reads a consumer only for its filename pattern, and the archive
    must use the documented default rather than whichever partner happened to
    trigger the build — the names inside a shared object cannot depend on that.
    """
    portal_config = {}


@require_safe
@api_throttle.budget_headers
def download(request):
    """The whole dataset as one file — a redirect for most callers.

    The bytes are not served from here. A full-scope caller is sent to the
    prepared archive on object storage with ``302``, which every HTTP client,
    browser and download manager follows, and which is resumable and edge-served
    in a way nothing streamed through this dyno could be.

    ``?gene=`` narrows it, and a narrowed request is built here and now, because
    one gene is a handful of files. That is also what a scoped consumer always
    gets.
    """
    consumer, err = _guard(request)
    if err:
        return err

    genes = [g.strip() for g in (request.GET.get('gene') or '').split(',') if g.strip()]

    qs = _scoped_images(consumer)
    if genes:
        from django.db.models import Q
        gene_q = Q()
        for gene in genes:
            gene_q |= Q(antibody__target__gene_name__iexact=gene)
        qs = qs.filter(gene_q)

    # The shared archive covers exactly one thing: the whole public set. Any
    # narrowing at all — a scoped key or a ?gene= — means building it here.
    if is_full_scope(consumer) and not genes:
        from . import bulk_archive

        target_ids = set(qs.values_list('antibody__target_id', flat=True))
        version, _total = _fingerprint(qs, R.curated_gene_ids(target_ids))
        url = bulk_archive.url_for(version)
        if url:
            return HttpResponseRedirect(url)
        return JsonResponse({
            'error': 'The prepared archive for this version of the dataset is '
                     'not built yet.',
            'detail': ('A figure has been published since the last build, and '
                       'the archive is rebuilt daily. Nothing is missing — every '
                       'file is listed in the manifest below and can be fetched '
                       'from its own URL, or narrow this request with ?gene= to '
                       'have one built now.'),
            'dataset_version': version,
            'manifest': f'{BASE_URL}/api/v1/manifest/',
        }, status=503)

    matched = qs.count()
    if matched == 0:
        return JsonResponse(
            {'error': 'No figures match that request.',
             'detail': 'Check the gene symbol against /api/v1/genes/.'
                       if genes else 'Your key has no figures in scope.'},
            status=404)
    if matched > STREAM_MAX_FILES:
        return JsonResponse({
            'error': f'{matched} figures is too many to build on the spot '
                     f'(the limit is {STREAM_MAX_FILES}).',
            'detail': 'Ask for fewer genes, or use the manifest to fetch the '
                      'files directly from object storage.',
            'manifest': f'{BASE_URL}/api/v1/manifest/',
        }, status=413)

    return _zip_response(qs, consumer, genes)


def _zip_response(qs, consumer, genes):
    """Build a small zip in memory and hand it over, manifest included."""
    import zipfile

    target_ids = set(qs.values_list('antibody__target_id', flat=True))
    curated_ids = R.curated_gene_ids(target_ids)
    rows, _collisions = _rows(qs, consumer, curated_ids,
                              include_recommendations=True, attach_images=True)

    buffer = io.BytesIO()
    # Stored, not deflated: PNG and JPEG are already compressed, so deflate buys
    # about 2% for CPU on every byte at both ends.
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
        archive.writestr('manifest.csv', rows_to_csv(rows))
        for row in rows:
            try:
                with row['_image'].open('rb') as fh:
                    archive.writestr(f'figures/{row["filename"]}', fh.read())
            except (OSError, ValueError):
                # One unreadable object must not cost the caller the other
                # 39 files. It is absent from figures/ and present in
                # manifest.csv, which is the discrepancy that says so.
                continue

    stem = '-'.join(genes) if genes else 'figures'
    name = _ILLEGAL_IN_FILENAME.sub('', f'oga-{stem}') or 'oga-figures'
    response = HttpResponse(buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{name}.zip"'
    response['Cache-Control'] = 'private, no-cache'
    return response


# ─────────────────────────────────────────────────────────
# Endpoint: GET /api/v1/  — the catalogue
# ─────────────────────────────────────────────────────────

@require_safe
def openapi_document(request):
    """The OpenAPI 3.1 description of this API. No key required.

    Point Swagger UI, Redoc, Postman, Insomnia or openapi-generator at this URL.
    It is built from the code (see core/api_schema.py), so it moves with the
    endpoints rather than being a promise somebody has to remember to update.
    """
    from .api_schema import document

    response = JsonResponse(document(), json_dumps_params={'indent': 2})
    # Public, read-only, and useful to anything that wants to generate a client.
    response['Access-Control-Allow-Origin'] = '*'
    response['Cache-Control'] = 'public, max-age=300'
    return response


@require_safe
@api_throttle.budget_headers
def api_index(request):
    """What this API offers, machine-readable and without a key.

    An API for computers whose endpoint list lives only in a Python file is one
    every consumer has to be told about by email. Carries no data, so it needs
    no key.
    """
    def url(path):
        return f'{BASE_URL}/api/v1/{path}'

    response = JsonResponse({
        'name': 'OGA Data API',
        'version': 'v1',
        'authentication': {
            'scheme': 'api-key',
            'header': 'X-API-Key',
            'note': 'Send the key as a header. The portal also accepts ?key= in '
                    'the URL for convenience; do not use that from a program — '
                    'query strings end up in server logs and browser history.',
        },
        'rate_limits': {
            'per_minute': api_throttle.BURST_LIMIT,
            'per_hour': api_throttle.SUSTAINED_LIMIT,
            'headers': ['X-RateLimit-Limit-Burst', 'X-RateLimit-Remaining-Burst',
                        'X-RateLimit-Limit-Sustained',
                        'X-RateLimit-Remaining-Sustained', 'Retry-After'],
            'note': 'Read the remaining count and pace yourself; a 429 carries '
                    'Retry-After in seconds.',
        },
        'endpoints': [
            {
                'path': url('manifest/'),
                'method': 'GET',
                                'summary': 'Every published figure you may download, as URLs. '
                           'Does not contain image bytes — fetch the URLs '
                           'directly; they are public and permanent.',
                'parameters': {
                    'format': 'json (default) | csv | urls',
                    'gene': 'comma-separated gene symbols — every file for '
                            'those genes, so complete stays true. Diff it '
                            'against a mirror of those genes only; see '
                            'sync.narrowed_by_request in the reply.',
                    'since': 'ISO 8601 or YYYY-MM-DD — additions only. Cannot '
                             'report removals; see the sync block in the reply.',
                    'limit': 'optional cap; a capped reply sets complete=false',
                    'offset': 'optional, use with limit',
                },
                'caching': 'Strong ETag. Send If-None-Match on reconnect and an '
                           'unchanged dataset answers 304.',
                'side_effects': 'none',
            },
            {
                'path': url('download/'),
                'method': 'GET',
                'summary': 'Everything in one zip. Redirects (302) to a prepared '
                           'archive on object storage — follow it. Contains the '
                           'figures and manifest.csv naming each one.',
                'parameters': {
                    'gene': 'optional, comma-separated — builds a zip of just '
                            'those genes instead of redirecting.',
                },
                'caching': 'The archive URL carries the dataset version, so it '
                           'is safe to cache for ever; re-request this endpoint '
                           'to learn the current one.',
                'side_effects': 'none',
            },
            {
                'path': url('antibodies/'),
                'method': 'GET',
                                'summary': 'Antibody records with metadata and image URLs.',
                'parameters': {
                    'preview': 'true returns the full set and advances nothing',
                    'since': 'ISO 8601 or YYYY-MM-DD',
                    'advance_cursor': 'false leaves your review cursor alone',
                    'gene': 'exact gene symbol',
                    'limit': 'optional cap; a capped reply sets complete=false',
                    'offset': 'optional, use with limit',
                },
                'side_effects': 'Advances your review cursor unless preview=true '
                                'or advance_cursor=false.',
            },
            {'path': url('genes/'), 'method': 'GET',              'summary': 'Genes with antibody and figure counts.',
             'side_effects': 'none'},
            {'path': url('gene-detail/'), 'method': 'GET',
             'available_to': 'manufacturer consumers only',
             'summary': 'Every antibody for one gene, grouped by supplier — the '
                        'competitor view. Commercially sensitive, so it is the '
                        'one endpoint restricted by who is asking.',
             'side_effects': 'none'},
            {'path': url('openapi.json'), 'method': 'GET',
             'summary': 'This API described as OpenAPI 3.1 - feed it to '
                        'Swagger UI, Postman or openapi-generator.',
             'side_effects': 'none'},
            {'path': url('status/'), 'method': 'GET',              'summary': 'Your scope, tier, cursor and dataset totals.',
             'side_effects': 'none'},
            {'path': url('mark-reviewed/'), 'method': 'POST',              'summary': 'Move your review cursor to now.',
             'side_effects': 'Writes last_queried_at.'},
            {'path': url('pipeline-data/'), 'method': 'GET',
             'available_to': 'keys with a supplier scope only',
             'summary': 'Figures of YOUR OWN reagents that have been cropped '
                        'and are not yet published — the pre-release window, so '
                        'a wrong catalogue number or a withdrawn lot can be '
                        'caught before the public page goes live. Unpublished '
                        'and provisional: do not quote or link them.',
             'parameters': {'gene': 'exact gene symbol'},
             'side_effects': 'none'},
            {'path': url('pipeline-image/'), 'method': 'GET',
             'available_to': 'keys with a supplier scope only',
             'summary': 'The image bytes of one pre-release figure. Served here '
                        'rather than from object storage, and re-checked against '
                        'your scope on each request.',
             'parameters': {'id': 'the id from figures[].image_url'},
             'side_effects': 'none'},
            {'path': url('gene-progress/'), 'method': 'GET',
             'available_to': 'keys with a supplier scope only',
             'summary': 'Where the genes you have reagents in have got to: '
                        'which applications have published figures, which have '
                        'figures of yours awaiting release, and whether the '
                        'report is out. Derived from records, never a typed '
                        'status.',
             'parameters': {'gene': 'exact gene symbol'},
             'side_effects': 'none'},
        ],
        'oga_recommendations': {
            'values': [R.RECOMMENDED, R.NOT_RECOMMENDED, R.NOT_TESTED],
            'meanings': R.MEANINGS,
            'scope': R.SCOPE_NOTE,
            'note': 'A recommendation is per application.',
        },
        # This response is the reference. `/using-the-data/` is about citing and
        # reusing OGA's findings and says nothing about the API, so naming it
        # here would send a reader who is stuck to a page that cannot answer
        # them — and a pointer that does not resolve costs more than none.
        'openapi': f'{BASE_URL}/api/v1/openapi.json',
        'documentation': f'{BASE_URL}/api/v1/',
        'human_portal': f'{BASE_URL}/portal/',
    })
    # Needs no key and is the same bytes for everybody, so let the CDN answer it
    # rather than this dyno. Same reason openapi.json carries one.
    response['Cache-Control'] = 'public, max-age=300'
    response['Access-Control-Allow-Origin'] = '*'
    return response
