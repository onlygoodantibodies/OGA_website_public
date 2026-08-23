"""Request throttling for the data API — the guard that was not running.

There was one protection, ``api_views._check_rate_limit``: one call per consumer
per hour, measured off ``APIConsumer.last_queried_at``. Three things were wrong
with it, and together they meant nothing was throttled at all.

**It was bypassable, and the bypass is the front door.** ``antibodies_feed``
skips the check entirely when ``?preview=true``, and the portal's own front end
requests exactly that on every connect. So the throttled path was the one nobody
called.

**The clock was also the cursor.** ``last_queried_at`` is simultaneously the
rate-limit timer, the ``since=`` cursor for the delta feed, and the portal's
"new since review" marker. A read advanced it, so a client that failed
mid-parse lost that delta permanently and a retry inside the hour got 429
instead of the same bytes. A read a computer makes has to be repeatable.

**It disagreed with itself.** ``genes_feed`` *checked* the timestamp and never
set it, while ``antibodies_feed`` set it — so calling antibodies and then genes
inside the hour returned 429 for the genes, and the reverse order worked.

This module is the replacement: a fixed-window counter per consumer, applied to
every authenticated endpoint including preview, that never touches any field the
data feed reads. The per-hour delta gate stays where it is — it is a documented
part of that endpoint's contract, not an abuse guard — and it is now the only
thing ``last_queried_at`` is used for.

**The limit is approximate, deliberately.** There is no shared cache configured,
so Django falls back to ``LocMemCache``, which is per gunicorn worker: with four
workers a consumer can get up to four times the nominal rate. That is fine for
what this is — a guard against a runaway script hammering PostgreSQL, not a
billing meter — and the data behind it is public anyway. Point ``CACHES`` at a
shared backend and the same code becomes exact; ``core.W003`` says which one is
in force so the answer is never a guess.

The headers matter more than the ceiling. A machine client that is told its
remaining budget and when the window resets will pace itself, and every
well-behaved consumer is one that never reaches the limit.
"""
from __future__ import annotations

import time

from django.core.cache import cache
from django.http import JsonResponse

# Per-consumer ceilings. Generous: the portal issues three requests on connect
# and one per gene-detail click, and a nightly sync client makes a handful.
# Anything near these numbers is a loop, not a user.
BURST_LIMIT = 60           # requests per minute
BURST_WINDOW = 60

SUSTAINED_LIMIT = 1000     # requests per hour
SUSTAINED_WINDOW = 3600

_WINDOWS = (
    ('burst', BURST_LIMIT, BURST_WINDOW),
    ('sustained', SUSTAINED_LIMIT, SUSTAINED_WINDOW),
)

# Requests that never got as far as a valid key. Tighter, because a caller with
# no key has nothing legitimate to do here at volume.
ANON_BURST_LIMIT = 30
ANON_SUSTAINED_LIMIT = 300


def _hit(key, window):
    """Count one request in a fixed window; returns the running count.

    ``add`` then ``incr`` rather than get/set, so two workers racing on the same
    key cannot both read 4 and both write 5.
    """
    if cache.add(key, 1, window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # Expired between the add and the incr — start the next window.
        cache.set(key, 1, window)
        return 1


def client_ip(request):
    """Best-effort caller identity for a request with no key.

    ``CF-Connecting-IP`` is what Cloudflare puts the real client in, and it is
    trustworthy for traffic that came through Cloudflare — which is all of it,
    unless somebody reaches the origin directly, and then they can spell this
    header however they like. That is a real limit and the reason this is
    defence in depth rather than the control: a rate-limiting rule at Cloudflare
    sees the connection itself and cannot be talked out of it.
    """
    forwarded = request.META.get('HTTP_CF_CONNECTING_IP')
    if forwarded:
        return forwarded.strip()
    chain = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if chain:
        return chain.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def check_anonymous(request):
    """Throttle a request that carried no usable key. Returns a 429 or None.

    Authentication runs before throttling, because the per-consumer counters are
    keyed on a consumer that a bad key does not have — so until this existed a
    request with a wrong key was refused **and counted nowhere**. Guessing a key
    is hopeless (they are UUID4s, 122 bits), which is not the point: every
    attempt cost one ``APIConsumer`` lookup against the SQLite database that
    also holds the site's logins, and nothing anywhere put a ceiling on how fast
    those could be asked for.

    Keyed on the caller rather than on the key, because the key is the thing
    they do not have.
    """
    now = time.time()
    who = client_ip(request)
    for name, limit, window in (('anon-burst', ANON_BURST_LIMIT, BURST_WINDOW),
                                ('anon-sustained', ANON_SUSTAINED_LIMIT,
                                 SUSTAINED_WINDOW)):
        index = int(now // window)
        count = _hit(f'oga:api:anon:{who}:{name}:{index}', window)
        if count > limit:
            resets_in = int((index + 1) * window - now)
            response = JsonResponse(
                {'error': f'Too many requests without a valid API key: more '
                          f'than {limit} in {window // 60} minute(s).',
                 'retry_after_seconds': resets_in},
                status=429)
            response['Retry-After'] = str(resets_in)
            return response
    return None


def check(consumer):
    """Count this request and decide whether to serve it.

    Returns ``(headers, error_response)``. ``error_response`` is None when the
    request may proceed; ``headers`` is always worth applying, because a client
    that can read its remaining budget is one that never gets refused.
    """
    now = time.time()
    headers = {}
    refusal = None

    for name, limit, window in _WINDOWS:
        index = int(now // window)
        count = _hit(f'oga:api:{consumer.pk}:{name}:{index}', window)
        resets_in = int((index + 1) * window - now)

        headers[f'X-RateLimit-Limit-{name.capitalize()}'] = str(limit)
        headers[f'X-RateLimit-Remaining-{name.capitalize()}'] = str(
            max(0, limit - count))

        if count > limit and refusal is None:
            refusal = JsonResponse(
                {
                    'error': f'Rate limit exceeded: more than {limit} requests '
                             f'in {window // 60} minute(s).',
                    'retry_after_seconds': resets_in,
                    'limit': limit,
                    'window_seconds': window,
                },
                status=429,
            )
            refusal['Retry-After'] = str(resets_in)

    if refusal is not None:
        for header, value in headers.items():
            refusal[header] = value

    return headers, refusal


def budget_headers(view):
    """Put the rate-limit budget on every reply the view produces.

    A decorator rather than a call at each ``return``, because these endpoints
    have between three and six exits apiece and the ones easiest to forget are
    the error paths — which are exactly the replies a client most needs the
    budget on, since a 400 still spent a request.
    """
    from functools import wraps

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        for header, value in getattr(request, '_oga_throttle_headers', {}).items():
            if not response.has_header(header):
                response[header] = value
        return response

    return wrapper


def stash(request, headers):
    """Hold the budget on the request until ``budget_headers`` applies it."""
    request._oga_throttle_headers = headers


def using_shared_cache():
    """Whether the throttle counts are shared across workers.

    Local-memory and dummy caches are per process, so the effective ceiling is
    the nominal one multiplied by the worker count.
    """
    from django.conf import settings

    backend = (settings.CACHES.get('default', {}).get('BACKEND', '')
               if hasattr(settings, 'CACHES') else '')
    if not backend:
        # Django's own default when CACHES is unset is local memory.
        return False
    return not backend.endswith(('LocMemCache', 'DummyCache'))
