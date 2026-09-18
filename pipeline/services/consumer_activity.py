"""What one API consumer has been doing with their key — in sentences.

The impact page answers "who is calling, and how much" for everybody at once.
Standing in front of it, the next question is always about one row: *what is
this partner actually doing with that key, and is it working for them?* A table
of totals cannot answer that. The answer is a paragraph.

**Sentences, because of what the reader does next.** Nobody opens this page to
audit a count; they open it before emailing a partner, and what they need is
"they have never opened the portal" or "their feed is 37 antibodies behind".
Those are built here, not left for a template to narrate from numbers, so a verb
agrees with its count and a zero reads as a fact rather than an empty cell.

**One reader per fact.** How many antibodies are waiting for a consumer is
answered *to that consumer* by ``/v1/status/``; this page asks
``core.api_views.pending_antibodies_for`` — the same function — because a
partner told 37 by their own portal and 41 by this page sees the disagreement
rather than the better answer. Portal sign-ins are counted on
``impact.PORTAL_CONNECT_ENDPOINT``, never a second spelling of ``api_status``.

**Plain English is a mapping, and a mapping drifts.** ``ENDPOINT_ACTIVITY``
turns a URL name into something a scientist can read. A name with no entry is
drawn as itself rather than dropped — a silently missing row would understate
the very thing the page exists to show — and ``tests_consumer_activity.py``
derives the expected keys from ``core/api_urls.py``, so a new endpoint fails
loudly here instead of appearing as a bare ``gene_progress`` in a sentence.

**What it cannot say, it says.** ``ApiUsageDay`` is one row per consumer per day
per endpoint, so there are no clocks on this page and there will not be any
without a per-request log — which that model's docstring refuses on purpose.
Three further silences matter enough to print: a caller refused by the throttle
is **never counted at all**, a call carrying a dead key is counted as *keyless*
and cannot be attributed to the consumer whose key it was, and ``/api/v1/`` and
``/api/v1/openapi.json`` pass through no counter at all. A quiet page here is
not proof of a quiet partner.

The cross-database part is free for the counter — the router sends
``APIConsumer``, ``ReviewedAntibody`` and ``ApiUsageDay`` to ``academy_db`` — but
it is not free for the antibodies a consumer has ticked off: those are stored as
catalogue *strings* while the antibodies are pipeline rows, and ``allow_relation``
refuses that pair outright. So the two are matched in Python, over a bounded
slice, never with a join the ORM would not permit anyway.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from pipeline.services.impact import PORTAL_CONNECT_ENDPOINT, RECENT_DAYS

#: How many of a consumer's active days the page lists. Bounded so a partner
#: pulling nightly for two years does not draw 700 rows; the total is printed
#: beside the list, so a truncated list never reads as the whole history.
DAYS_LISTED = 30

#: How many ticked-off antibodies are resolved back to a gene and drawn. Each
#: one costs nothing extra — they are resolved in a single query — but the list
#: is a sample and the page says so.
REVIEWED_LISTED = 25

#: A quiet stretch worth a sentence of its own.
QUIET_DAYS = RECENT_DAYS

#: Plain English for each endpoint, keyed on the URL **name** — which is what
#: `core/api_usage.py::endpoint_of` writes into the counter, deliberately, "because
#: a path carries the query string's worth of variation".
#:
#: Written from the consumer's side of the transaction ("they asked", not "the
#: server served"), because the sentence this feeds is about a partner's
#: behaviour. Two of these can never appear in the data — see ``UNCOUNTED`` — and
#: are kept anyway so the mapping is the full catalogue rather than the subset
#: that happens to be observable.
ENDPOINT_ACTIVITY = {
    'api_index': 'read the API catalogue',
    'openapi': 'read the API schema',
    'manifest': 'asked for their download manifest',
    'download': 'downloaded the bulk archive',
    'antibodies_feed': 'pulled the antibody feed',
    'genes_feed': 'listed the genes with data',
    'gene_detail': 'looked up every antibody for one gene',
    'api_status': 'opened the portal',
    'portal_config_update': 'changed their download settings',
    'mark_reviewed': 'marked everything as reviewed',
    'reviewed_antibodies': 'ticked off individual antibodies',
    'clear_reviewed': 'cleared their review ticks',
    'report_issue': 'reported a problem with a record',
    'pipeline_data': 'fetched pre-release data on their own reagents',
    'pipeline_image': 'fetched a pre-release figure',
    'gene_progress': 'checked how far their genes have got',
    'not_supportive': 'reviewed their antibodies with no supportive result',
    'not_supportive_csv': 'downloaded their not-supportive list as a spreadsheet',
    # `api_usage.endpoint_of` writes this literal when a request resolves to no
    # URL name. It has never been seen in the data; if it ever is, it is a fact
    # about the counter and not about the partner, and it says so.
    'unknown': 'called something the counter could not name',
}

#: Endpoints that reach no counter, because they call neither guard: the two
#: discovery URLs a new integrator hits first. Named on the page, since "they
#: have never read the schema" is a conclusion the data cannot support.
UNCOUNTED = ('api_index', 'openapi')

#: The paths those two are served at. The page names the URLs rather than the
#: activity labels: a caveat whose parenthetical repeats the clause before it in
#: verb form tells the reader nothing, and the point of naming them is so a
#: reader can tell which endpoint the claim is about.
UNCOUNTED_PATHS = ('/api/v1/', '/api/v1/openapi.json')

#: The portal's own actions, as against its sign-in. Every one is a keyed HTTP
#: endpoint with a documented curl example, so a script reaches them without
#: ever fetching ``/v1/status/`` — which is why no connects is not no portal
#: work. Counted apart from ``PORTAL_CONNECT_ENDPOINT``, which stays the sign-in
#: and nothing else, or this page and the impact page's "portal connects" would
#: give two answers to one question.
PORTAL_ACTION_ENDPOINTS = ('portal_config_update', 'mark_reviewed',
                           'reviewed_antibodies', 'clear_reviewed',
                           'report_issue')


def activity_label(endpoint):
    """Plain English for one endpoint name, or the name itself.

    Never ``None`` and never blank: an unlabelled endpoint is drawn as its own
    URL name, because a row that vanished for want of a translation would make
    the page quietly understate what the partner did.
    """
    return ENDPOINT_ACTIVITY.get(endpoint) or f'called {endpoint}'


def _plural(n, singular, plural=None):
    """Verbs agree with counts, and counts are often 1."""
    return singular if n == 1 else (plural or singular + 's')


def _usage_rows(consumer):
    from core.models import ApiUsageDay

    return ApiUsageDay.objects.filter(consumer_id=consumer.pk)


def _totals(rows):
    return rows.aggregate(requests=Sum('count'),
                          first=Min('date'),
                          last=Max('date'),
                          days=Count('date', distinct=True))


def _by_endpoint(rows):
    """What they did, most-done first — one row per endpoint, in plain English.

    ``days`` is beside ``requests`` on purpose: 40 requests on one day and 40
    across 40 days are the same total and completely different partners.
    """
    out = []
    for row in (rows.values('endpoint')
                    .annotate(requests=Sum('count'),
                              days=Count('date', distinct=True),
                              last=Max('date'))
                    .order_by('-requests')):
        out.append({
            'endpoint': row['endpoint'],
            'activity': activity_label(row['endpoint']),
            'requests': row['requests'],
            'days': row['days'],
            'last': row['last'],
        })
    return out


def _by_day(rows):
    """Their most recent active days, newest first, with the day's total.

    Only days they actually called on. A 90-cell calendar of mostly-blank
    squares says less than a short list of real dates, and the empty squares
    would be doing the work of a claim the counter cannot make anyway — a
    throttled or keyless call is a day that looks empty and was not.
    """
    return list(rows.values('date')
                    .annotate(requests=Sum('count'))
                    .order_by('-date')[:DAYS_LISTED])


def _reviewed(consumer):
    """The antibodies they have ticked off, resolved back to genes.

    This is the closest thing in the database to what a partner actually looked
    at, and until now nothing outside the portal's own JavaScript read it.

    The rows hold a catalogue **string** and live in ``academy_db``, while the
    antibodies are pipeline rows — a pair the router refuses to relate — so the
    match is done in Python over the drawn slice.

    **A catalogue number is not an identity.** The key is
    ``(catalogue, company, target)``; ``catalogue_number`` carries no unique
    constraint of its own, and `DUPLICATE_ANTIBODY_FINDINGS.md` records live rows
    where one number sits on two different targets. Taking whichever row the
    database returned first meant the tie was broken by ``Antibody.Meta.ordering``
    — the *protein name* — so a partner's tick could be labelled with another
    supplier's gene, with nothing on the page saying a choice had been made. So:
    narrowed to the consumer's own suppliers first, and where the surviving rows
    still disagree the gene is left **ambiguous** rather than picked. A wrong
    gene here is worse than no gene: it is the one column a reader would quote.

    **Matched case-insensitively**, like every other catalogue lookup in this
    repo. Both sides are typed by somebody else — the consumer POSTs whatever
    their own system calls the product, and 19 live rows carry the wrong case
    already — and an exact match draws a stocked product as *no longer on file*.

    Three outcomes, deliberately distinct: a gene, ``None`` for a catalogue that
    matched nothing, and ``AMBIGUOUS`` for one that matched rows disagreeing
    about the gene. The unreachable fourth — matched, but the target's gene name
    is blank — is 8 live rows, and it is a match, so it must not draw as the
    tick having gone missing.
    """
    from core.models import ReviewedAntibody

    rows = ReviewedAntibody.objects.filter(consumer_id=consumer.pk)
    total = rows.count()
    recent = list(rows.order_by('-reviewed_at')[:REVIEWED_LISTED])
    last = recent[0].reviewed_at if recent else None

    resolved = _resolve_catalogues(consumer,
                                   [r.antibody_catalogue for r in recent])

    listed = []
    for r in recent:
        match = resolved.get((r.antibody_catalogue or '').strip().casefold())
        listed.append({
            'catalogue': r.antibody_catalogue,
            'gene': None if match is None else match.get('gene'),
            'target_id': None if match is None else match.get('target_id'),
            'ambiguous': bool(match and match.get('ambiguous')),
            'matched': match is not None,
            'reviewed_at': r.reviewed_at,
        })

    return {
        'total': total,
        'last': last,
        'listed': listed,
        'more': max(0, total - len(recent)),
        # A read that failed is not an empty result: without this the page would
        # draw every tick as "no longer on file", which is a statement about the
        # partner rather than about the database that did not answer.
        'unresolved': resolved is _UNREADABLE,
    }


#: Returned by `_resolve_catalogues` when the pipeline database could not be
#: read. A distinct object rather than `{}`, because "nothing matched" and "we
#: could not look" are different facts and the page says which.
_UNREADABLE = {}


def _resolve_catalogues(consumer, catalogues):
    """Catalogue string → ``{'gene', 'target_id', 'ambiguous'}``, case-folded.

    Narrowed to the consumer's own companies where they have a supplier scope,
    since a tick is against a product in *their* feed. An RRID consumer sees
    everybody's antibodies, so for them there is nothing to narrow by and a
    genuine collision stays ambiguous.
    """
    if not catalogues:
        return {}

    from core.api_views import _get_supplier_company_ids
    from pipeline.models import Antibody

    wanted = {(c or '').strip().casefold() for c in catalogues if (c or '').strip()}
    if not wanted:
        return {}

    # One `iexact` per catalogue, OR'd — bounded by REVIEWED_LISTED, so this is
    # one query of at most 25 terms however much the antibody table grows. The
    # obvious `catalogue_number__in` is exact on PostgreSQL and would draw a
    # stocked product as missing on a case difference either side.
    match = Q()
    for value in {(c or '').strip() for c in catalogues if (c or '').strip()}:
        match |= Q(catalogue_number__iexact=value)

    try:
        qs = Antibody.objects.filter(match)
        if consumer.consumer_type == 'manufacturer' and consumer.supplier_filter:
            company_ids = _get_supplier_company_ids(consumer.supplier_filter)
            if company_ids is not None:
                qs = qs.filter(company_id__in=company_ids)
        rows = list(qs.select_related('target')
                      .only('catalogue_number', 'target__gene_name'))
    except Exception:  # noqa: BLE001 — an outage is a gap, not an empty result
        return _UNREADABLE

    out = {}
    for ab in rows:
        key = (ab.catalogue_number or '').strip().casefold()
        if key not in wanted:
            continue
        gene = (ab.target.gene_name or '').strip() or None
        seen = out.get(key)
        if seen is None:
            out[key] = {'gene': gene, 'target_id': ab.target_id,
                        'ambiguous': False}
        elif not seen['ambiguous'] and seen['gene'] != gene:
            # Two products, one number, different genes. Neither is "the"
            # answer, so the page stops claiming one.
            out[key] = {'gene': None, 'target_id': None, 'ambiguous': True}
    return out


def _pending(consumer):
    """How many antibodies are waiting for them, asked the way they are told it.

    Returns ``None`` when the count could not be taken — the pipeline database
    is a different server, and a reporting page that turned an outage into a
    confident **0** would say "nothing is waiting for them" about a partner with
    a backlog. The page draws the gap instead.
    """
    from core.api_views import pending_antibodies_for

    try:
        return pending_antibodies_for(consumer)
    except Exception:  # noqa: BLE001 — any DB or scope failure is the same gap
        return None


def _scope_sentences(consumer):
    """What this key may see, said plainly."""
    said = []
    if consumer.consumer_type == 'rrid':
        said.append('A registry key: every published antibody, whoever makes it.')
    elif (consumer.supplier_filter or '').strip():
        names = [s.strip() for s in consumer.supplier_filter.split(',') if s.strip()]
        said.append('Scoped to {}: {}.'.format(
            _plural(len(names), 'one supplier name', f'{len(names)} supplier names'),
            ', '.join(names)))
    else:
        # `core/api_pipeline.py` refuses an unscoped key by name. Worth saying
        # here, because from the partner's side this looks like the pre-release
        # endpoints being broken rather than the key being unfinished.
        said.append('A manufacturer key with no supplier scope, so the '
                    'pre-release endpoints refuse it by name. The published '
                    'feeds still work.')

    genes = consumer.get_gene_filter_list()
    if genes:
        said.append('Restricted to {} {}: {}. A gene restriction is a trial '
                    'setting — a live account normally has none.'.format(
                        len(genes), _plural(len(genes), 'gene'),
                        ', '.join(genes)))
    else:
        said.append('No gene restriction.')
    return said


def _story(consumer, totals, portal_requests, other_requests, reviewed, pending,
           portal_actions=0):
    """The paragraph at the top: what they have done, and how.

    Every branch here answers with a sentence rather than leaving a blank,
    because the interesting states on this page are the empty ones — a key
    issued and never used, a portal never opened, a cursor that never moved.

    **No sentence here may contradict the tables under it.** The paragraph is
    read first and quoted; the tables are checked afterwards, if at all. So the
    portal branch asks about the portal's *actions* as well as its sign-in, and
    the cursor and the backlog are two sentences rather than one — an outage
    counting the backlog must not also swallow where their cursor is.
    """
    said = []
    requests = totals['requests'] or 0
    days = totals['days'] or 0
    name = consumer.name

    if not consumer.is_active:
        said.append(f'{name}’s key is switched off. Calls carrying it are '
                    'refused and counted as keyless, so nothing they try from '
                    'now on will appear on this page.')

    if not requests:
        said.append(f'{name} has never used this key. It was issued on '
                    f'{consumer.created_at:%-d %B %Y}.')
    else:
        # One active day is the commonest non-empty state on this page — a key
        # somebody tried once — and "between 5 August and 5 August" lands
        # squarely on it.
        if days == 1:
            said.append('{} has made {} {} on one day, {:%-d %B %Y}.'.format(
                name, requests, _plural(requests, 'request'), totals['first']))
        else:
            said.append(
                '{} has made {} {} on {} days, between {:%-d %B %Y} and '
                '{:%-d %B %Y}.'.format(name, requests,
                                       _plural(requests, 'request'), days,
                                       totals['first'], totals['last']))

        if portal_requests and other_requests:
            # "those requests" refers back to the total in the sentence before,
            # so it agrees with `requests` — never with the subset being named.
            said.append('They use both the portal and something automated — '
                        f'{portal_requests} of those {_plural(requests, "request")} '
                        'opened the portal, the rest did not.')
        elif portal_requests:
            said.append('Everything they have done has been through the portal. '
                        'There is no sign of a script.')
        elif portal_actions:
            # Not "all machine traffic against the feeds": the table below is
            # about to print "marked everything as reviewed", and a sentence
            # denying what the next table shows is a contradiction rather than
            # a caveat. Every portal action is a keyed HTTP endpoint a script
            # can call without ever fetching /v1/status/.
            said.append(
                'They have never opened the portal, but {} of those {} did '
                'something the portal does — marking records reviewed, or '
                'changing their settings — so a script is driving that too.'
                .format(portal_actions, _plural(requests, 'request')))
        else:
            said.append('They have never opened the portal. All of this is '
                        'machine traffic against the feeds.')

        last = totals['last']
        quiet = (timezone.localdate() - last).days if last else None
        if quiet is not None and quiet >= QUIET_DAYS:
            said.append(f'Nothing since {last:%-d %B %Y} — {quiet} days quiet.')

    # Where their cursor is, and how much sits beyond it — two facts, two
    # sentences. Folded into one branch, the outage message was reachable only
    # for a consumer whose cursor had moved, so the commonest case left the
    # missing count with no reason anywhere on the page.
    if consumer.last_queried_at is None:
        said.append('Their delta cursor has never moved, so the next feed call '
                    'would return everything in their scope rather than a '
                    'change since last time.')
    else:
        said.append('Their feed is caught up to '
                    f'{consumer.last_queried_at:%-d %B %Y}.')

    if pending is None:
        said.append('How much is waiting for them could not be counted just now '
                    '— the pipeline database did not answer, so that figure is '
                    'missing rather than zero.')
    elif consumer.last_queried_at is None:
        if pending:
            said.append('{} {} in their scope, and a first call would return '
                        'all of them.'.format(
                            f'{pending} '
                            + _plural(pending, 'antibody', 'antibodies'),
                            _plural(pending, 'is', 'are')))
        else:
            said.append('There is nothing in their scope for a call to return '
                        'yet.')
    elif pending:
        said.append('{} {} been published in their scope since.'.format(
            f'{pending} ' + _plural(pending, 'antibody', 'antibodies'),
            _plural(pending, 'has', 'have')))
    else:
        said.append('Nothing has been published in their scope since — there is '
                    'nothing waiting for them.')

    # "with their key", not "in the portal": `/v1/reviewed/` is a documented
    # POST, so a tick says which key was used and never which client sent it.
    if reviewed['total']:
        said.append('They have ticked off {} {} with their key, most recently '
                    'on {:%-d %B %Y}.'.format(
                        reviewed['total'],
                        _plural(reviewed['total'], 'antibody', 'antibodies'),
                        reviewed['last']))
    else:
        said.append('They have not ticked off any individual antibodies.')

    return said


def for_consumer(consumer):
    """Everything this page draws about one consumer.

    One call, a fixed number of queries whatever the consumer has done — the
    slices are bounded and the aggregates happen in SQL, so a partner pulling
    nightly for two years costs the same as one who has never called.
    """
    rows = _usage_rows(consumer)
    totals = _totals(rows)
    by_endpoint = _by_endpoint(rows)

    portal_requests = sum(r['requests'] for r in by_endpoint
                          if r['endpoint'] == PORTAL_CONNECT_ENDPOINT)
    other_requests = (totals['requests'] or 0) - portal_requests
    portal_actions = sum(r['requests'] for r in by_endpoint
                         if r['endpoint'] in PORTAL_ACTION_ENDPOINTS)

    reviewed = _reviewed(consumer)
    pending = _pending(consumer)

    by_day = _by_day(rows)
    return {
        'consumer': consumer,
        'story': _story(consumer, totals, portal_requests, other_requests,
                        reviewed, pending, portal_actions),
        'scope': _scope_sentences(consumer),
        'totals': totals,
        'requests': totals['requests'] or 0,
        'days': totals['days'] or 0,
        'portal_requests': portal_requests,
        'other_requests': other_requests,
        'by_endpoint': by_endpoint,
        'by_day': by_day,
        # A chart's bars must be in proportion, and `widthratio` is all a
        # template has — so the maximum is computed here, `or 1` because a
        # zero denominator is a division error on an empty page.
        'busiest_day': max((d['requests'] for d in by_day), default=0) or 1,
        'days_more': max(0, (totals['days'] or 0) - len(by_day)),
        'reviewed': reviewed,
        'pending': pending,
        # A separate boolean rather than `{% if pending is None %}` in the
        # template. An expression that raises inside a template does not fail
        # loudly, it silently picks a branch — and the branch it would pick here
        # draws an outage as a confident "0 waiting".
        'pending_known': pending is not None,
        'portal_config': consumer.portal_config or {},
        'portal_actions': portal_actions,
        'uncounted': list(UNCOUNTED_PATHS),
        'recent_days': RECENT_DAYS,
    }
