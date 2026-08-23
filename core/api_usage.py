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

**It counts requests that were allowed, not requests that arrived.** A throttled
caller is already refused in the cache and never reaches here, which keeps an
anonymous flood from turning into a database write per attempt. The cost is that
the numbers understate a runaway client; the alternative is a write path a
stranger controls.
"""
from __future__ import annotations

import logging

from django.db import DatabaseError, IntegrityError
from django.db.models import F
from django.utils import timezone

from .models import ApiUsageDay

logger = logging.getLogger(__name__)

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

    try:
        rows = ApiUsageDay.objects.filter(
            consumer=consumer, date=today, endpoint=endpoint,
        ).update(count=F('count') + 1)
        if rows:
            return

        try:
            ApiUsageDay.objects.create(
                consumer=consumer, date=today, endpoint=endpoint, count=1)
        except IntegrityError:
            # Another worker created the day's first row between the update and
            # the insert. The partial unique constraints are what make this
            # fail rather than quietly produce a second row to split the count
            # across — so losing the race is safe, and the increment is simply
            # retried against the row that won it.
            ApiUsageDay.objects.filter(
                consumer=consumer, date=today, endpoint=endpoint,
            ).update(count=F('count') + 1)

    except DatabaseError:
        # Telemetry never breaks the thing it is measuring. `academy_db` is
        # SQLite and also holds the logins; a locked counter must not become a
        # failed request, let alone a failed sign-in.
        logger.warning('API usage counter failed for endpoint %r', endpoint,
                       exc_info=True)
