"""Counting API requests — the baseline that did not exist.

``core/api_throttle.py`` decides whether a request may proceed; this records
that it happened. They are deliberately separate: the throttle's counters live
in a cache and are meant to evaporate, and this has to survive a restart or it
answers nothing.

**Why it is worth a write at all.** The API key was doing double duty — an
entitlement *and*, in everyone's head, an audit trail. It was never the second:
``APIConsumer`` stores one timestamp that is really the delta feed's cursor, and
nothing else about a request was kept anywhere. So "who is using this" had no
answer, and any decision about opening the data up would have been a change with
no before to compare an after against. One row per consumer per endpoint per
day, incremented in place, is the cheapest thing that can answer it.

**It must never break a request.** The counter is telemetry about a service, not
part of it, so every database error here is swallowed to the log. That matters
more than usual: these rows live in ``academy_db``, which is SQLite on a Render
disk and also holds the site's logins, so a write that contends must not be able
to turn a rate-limit counter into a failed sign-in. The volume this is measuring
is currently ~0.03 requests/second, so contention is theoretical — but a
counter that can take the site down is not worth having at any volume.

**A keyless row is not always a request served.** ``_guard`` counts a keyless
request before refusing it, so only ``SERVED_WITHOUT_KEY`` ever answered one;
``refused_without_key_q`` is the one reader for telling the two apart.

**It counts requests that were allowed, not requests that arrived.** A throttled
caller is already refused in the cache and never reaches here, which keeps an
anonymous flood from turning into a database write per attempt. The cost is that
the numbers understate a runaway client; the alternative is a write path a
stranger controls.

**The supplier portal is an API client, so the counter says which requests it
made.** Every click in the portal is a keyed request to ``/api/v1/`` — a sign-in
alone is three — so until 13 Oct 2026 the impact page credited nine
organisations with "API use" that was 96% the portal, and the one partner who
had called the API directly (8 requests over two days) was invisible inside it.
``route_of`` is the one reader for *portal*, *direct* or *cannot tell*.
"""
from __future__ import annotations

import logging
from datetime import date

from django.db import DatabaseError, IntegrityError
from django.db.models import CharField, Case, Exists, F, OuterRef, Q, Value, When
from django.utils import timezone

from .models import ApiUsageDay

logger = logging.getLogger(__name__)

#: The endpoints that answer a request carrying no key
#: (``api_views._guard_optional``, owner, 12 Aug 2026). Every other counted
#: endpoint goes through ``_guard``, which counts a keyless request **and then
#: refuses it** — 401, nothing returned — so a keyless row anywhere else is a
#: refusal written in exactly the shape of a request served. Read as served, the
#: usage download said "no key · fetched a pre-release figure" six times and was
#: read as a leak (13 Oct 2026). ``tests_api_usage`` asks each endpoint, so
#: opening a third one fails a test rather than silently re-reading history.
SERVED_WITHOUT_KEY = ('antibodies_feed', 'genes_feed', 'uniprot_mapping')

#: The day those two opened. The counter is older (9 Aug), and a keyless row on
#: them before this date was a 401 like any other. ``uniprot_mapping`` was
#: keyless from its first day (12 Nov 2026), so no row of it predates this.
SERVED_WITHOUT_KEY_SINCE = date(2026, 8, 12)


def refused_without_key_q():
    """The rows that counted a keyless request the endpoint then refused.

    Certain from the row alone. The converse is not quite: a *wrong* key is
    also counted keyless (the row is keyed on a caller the request failed to
    identify), so on the two open feeds a refused bad key and a served
    keyless read share one row and cannot be told apart.
    """
    return Q(consumer__isnull=True) & (
        ~Q(endpoint__in=SERVED_WITHOUT_KEY)
        | Q(date__lt=SERVED_WITHOUT_KEY_SINCE))


def refused_without_key(consumer_id, endpoint, day):
    """``refused_without_key_q`` for one row already read."""
    return consumer_id is None and (
        endpoint not in SERVED_WITHOUT_KEY or day < SERVED_WITHOUT_KEY_SINCE)

#: The header ``core/templates/core/portal.html`` sets on every request it
#: makes. Not a credential and not a secret — anybody can send it, and a script
#: that does is choosing to be counted as the portal. It is a label, read for
#: an impact figure, and nothing is allowed or refused on it.
PORTAL_HEADER = 'X-OGA-Client'
PORTAL_HEADER_VALUE = 'portal'


def from_portal(request):
    """Did the supplier portal make this request?

    Two signals, either enough. The **label** is the portal's own header. The
    **browser's** is ``Sec-Fetch-Site: same-origin`` on a request carrying an
    ``X-API-Key`` header: every current browser sets the first on a ``fetch``
    and no script does, a link cannot set the second, and the portal is the
    only page this site serves that sends a key (``data_access.html`` shows
    ``curl`` examples and calls nothing). Without the key condition, somebody
    clicking through to an open feed from one of our own pages would count as
    the portal. The second signal covers what the first cannot: a portal tab
    opened before the label shipped, which goes on clicking for days, and a
    ``fetch`` added to the portal next year by somebody who did not know the
    header mattered — either would otherwise be counted as somebody calling the
    API directly, which is the very misreading this exists to stop.
    """
    headers = request.headers
    if (headers.get(PORTAL_HEADER) or '').strip().lower() == PORTAL_HEADER_VALUE:
        return True
    return ((headers.get('Sec-Fetch-Site') or '').strip().lower() == 'same-origin'
            and 'X-API-Key' in headers)


#: Fallback when a request somehow has no resolved URL name. Never expected —
#: `resolver_match` is set before any view runs — but a counter that raises on
#: a surprise is a counter that takes an endpoint down with it.
UNKNOWN_ENDPOINT = 'unknown'


def endpoint_of(request):
    """Which endpoint this was, as its URL name.

    The URL *name* rather than the path, because a path carries the query
    string's worth of variation and a name is the thing anybody reading these
    numbers actually asks about ("how often is the manifest fetched?").
    """
    match = getattr(request, 'resolver_match', None)
    name = getattr(match, 'url_name', None) if match else None
    return (name or UNKNOWN_ENDPOINT)[:64]


def record(request, consumer=None):
    """Count one allowed request. ``consumer=None`` means it carried no key.

    ``UPDATE`` first and ``INSERT`` only when nothing matched, so the common
    case — every request after the day's first — is a single statement against
    one indexed row, with no read and no race to lose.
    """
    endpoint = endpoint_of(request)
    today = timezone.localdate()
    # Always True or False, never blank: a blank row means "recorded before the
    # counter could tell", and `route_of` reads those by inference.
    key = dict(consumer=consumer, date=today, endpoint=endpoint,
               via_portal=from_portal(request))

    try:
        rows = ApiUsageDay.objects.filter(**key).update(count=F('count') + 1)
        if rows:
            return

        try:
            ApiUsageDay.objects.create(count=1, **key)
        except IntegrityError:
            # Another worker created the day's first row between the update and
            # the insert. The partial unique constraints are what make this
            # fail rather than quietly produce a second row to split the count
            # across — so losing the race is safe, and the increment is simply
            # retried against the row that won it.
            ApiUsageDay.objects.filter(**key).update(count=F('count') + 1)

    except DatabaseError:
        # Telemetry never breaks the thing it is measuring. `academy_db` is
        # SQLite and also holds the logins; a locked counter must not become a
        # failed request, let alone a failed sign-in.
        logger.warning('API usage counter failed for endpoint %r', endpoint,
                       exc_info=True)


# ── Which route a request came by: the portal, direct, or cannot tell ────────

#: The three answers. ``UNPLACED`` is not a hedge: it is the honest reading of
#: an old row the day rule below cannot place, and is drawn as its own number.
PORTAL = 'portal'
DIRECT = 'direct'
UNPLACED = 'unplaced'

#: The portal's sign-in: ``connect()`` calls ``/v1/status/``, then
#: ``/v1/antibodies/``, then ``/v1/reviewed/``, every time the page loads.
PORTAL_CONNECT = 'api_status'

#: What a portal tab already signed in can call without signing in again —
#: every keyed call in ``portal.html`` except the two ``connect()`` makes
#: first. ``not_supportive_pdf`` was withdrawn on 14 Sep 2026 and is here for
#: the rows written before. ``antibodies_feed`` is deliberately absent: the
#: portal fetches it only straight after a sign-in, so a day holding the feed
#: and no sign-in was not the portal. **Frozen on purpose**: it reads only rows
#: written before the portal labelled its requests, so it describes the portal
#: as it was then, and a call the portal gains later needs no entry here.
PORTAL_OPEN_TAB_ENDPOINTS = (
    'reviewed_antibodies', 'gene_detail', 'pipeline_data', 'pipeline_image',
    'gene_progress', 'not_supportive', 'not_supportive_csv',
    'not_supportive_pdf', 'portal_config_update', 'mark_reviewed',
    'clear_reviewed', 'report_issue',
)


def route_of():
    """``PORTAL``, ``DIRECT`` or ``UNPLACED`` for each ``ApiUsageDay`` row, as an
    expression to annotate with — the one reader for the question, so the
    impact page, an organisation's own page and the download cannot disagree.

    A row written since the portal labelled its requests says which it was.
    An older row (``via_portal`` blank) is read by **the day it fell on**,
    because the counter kept no times:

    - **A day that organisation signed in to the portal is a portal day**, and
      every request on it is the portal's. The live rows bear it out: on every
      such day the mix is exactly what the portal sends. It is an inference,
      and it misfiles a script that also called ``/v1/status/`` on a day
      somebody opened the portal — ``data_access.html``'s first ``curl``
      example calls that endpoint.
    - **A day with anything the portal cannot do without signing in** — the
      feed, the manifest, the bulk download — is direct.
    - **Only what an open tab does, before they had ever signed in**, is
      direct: a tab cannot be open on a portal nobody has opened.
    - **Anything else cannot be placed**: only calls an already-open portal tab
      also makes, on a day with no sign-in, after one. A tab left open
      overnight looks exactly like that (the counter dates in UTC, so a US
      afternoon runs into the next day), and so does a script. Neither guess is
      safe — calling it the portal hides a script, calling it direct is the
      misreading this was written to end — so it is counted apart.
    - **A keyless row** has no organisation to have a sign-in day. A served
      feed is direct (the portal never fetches one without a key that worked);
      anything else was refused, and could be a portal sign-in with a mistyped
      key or anything at all, so it cannot be placed.
    """
    from .models import ApiUsageDay

    same_day_untold = ApiUsageDay.objects.filter(
        consumer_id=OuterRef('consumer_id'), date=OuterRef('date'),
        via_portal__isnull=True)
    signed_in = same_day_untold.filter(endpoint=PORTAL_CONNECT)
    not_an_open_tab = same_day_untold.exclude(
        endpoint__in=PORTAL_OPEN_TAB_ENDPOINTS + (PORTAL_CONNECT,))
    signed_in_by_then = ApiUsageDay.objects.filter(
        consumer_id=OuterRef('consumer_id'), date__lte=OuterRef('date'),
        endpoint=PORTAL_CONNECT, via_portal__isnull=True)

    return Case(
        When(via_portal=True, then=Value(PORTAL)),
        When(via_portal=False, then=Value(DIRECT)),
        When(Q(consumer__isnull=True) & Q(endpoint__in=SERVED_WITHOUT_KEY),
             then=Value(DIRECT)),
        When(consumer__isnull=True, then=Value(UNPLACED)),
        When(Exists(signed_in), then=Value(PORTAL)),
        When(Exists(not_an_open_tab), then=Value(DIRECT)),
        When(~Exists(signed_in_by_then), then=Value(DIRECT)),
        default=Value(UNPLACED),
        output_field=CharField(),
    )


def with_route(rows):
    """``rows`` annotated with ``route`` — ``PORTAL``, ``DIRECT`` or ``UNPLACED``."""
    return rows.annotate(route=route_of())
