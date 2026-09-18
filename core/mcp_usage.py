"""Counting MCP tool calls — the baseline that did not exist.

``core/api_usage.py`` does this for the web API and the reasoning there applies
here unchanged. What is different is *where the call happens*: the MCP server is
a separate Render service (`OGA_MCP`, running ``mcp_servers/http_server.py``),
so nothing in this process ever sees the request. Its own log was the only
record, Render keeps seven days of it, and everything before that is gone — so
on 31 Aug 2026 "has anybody outside the consortium used it?" was answerable for
one week and unanswerable for the six before.

**The MCP service reports; this counts.** One POST per tool call to
``/internal/mcp-usage/``, carrying the tool's name and the client's, gated by a
shared token. The alternative was to let the MCP service write the row itself,
which means handing it a credential for ``academy_db`` — the database that also
holds every login — to increment a counter. A narrow endpoint is the smaller
thing to get wrong.

**The reporter never blocks a tool call and this must never make it want to.**
``mcp_servers/common/usage.py`` posts from a background thread and drops what it
cannot send, so a slow or missing reply here costs a number and never a reader's
answer. That is why this view does as little as possible and why nothing in it
is worth retrying.

**Ours is counted apart, never dropped.** A report says whether the connector
attributed the call to one of our own OAuth subjects, and that flag is part of
the row's key. The impact page keeps internal calls out of the headline and
prints what it kept out — a total that silently swallowed our own testing could
not tell a quiet month from a broken counter, which is the confusion this whole
counter exists to end.

**The day is this server's, not the caller's.** A reporter that could choose the
date could backdate rows, and the row count is bounded precisely because the
date is bounded. Same reason ``count`` is clamped: a batch is an optimisation,
never a way to write an arbitrary number into a total somebody will quote.
"""
from __future__ import annotations

import hmac
import json
import logging

from django.conf import settings
from django.db import DatabaseError, IntegrityError
from django.db.models import F
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import McpUsageDay

logger = logging.getLogger(__name__)

#: The most one report may add. A report carries a count so a busy minute can be
#: folded into one POST; it is capped so a mistake (or a leaked token) writes a
#: wrong number rather than an absurd one.
MAX_REPORT = 500

#: Column widths, kept here so the truncation and the model cannot disagree.
_TOOL_MAX = McpUsageDay._meta.get_field('tool').max_length
_CLIENT_MAX = McpUsageDay._meta.get_field('client').max_length


def record(tool: str, client: str = '', count: int = 1,
           internal: bool = False) -> None:
    """Add ``count`` calls of ``tool`` by ``client`` to today's row.

    ``UPDATE`` first and ``INSERT`` only when nothing matched, so the common
    case is a single statement against one indexed row. Every database error is
    swallowed: telemetry about a service is never allowed to break it, and these
    rows share a database with the site's logins.

    ``internal`` is part of the row's identity and so of every query here — our
    call and a reader's call to the same tool on the same day are two rows, or
    the count is right and the attribution silently wrong.
    """
    tool = (tool or '')[:_TOOL_MAX]
    client = (client or '')[:_CLIENT_MAX]
    if not tool:
        return
    today = timezone.localdate()
    key = dict(date=today, tool=tool, client=client, internal=bool(internal))

    try:
        rows = McpUsageDay.objects.filter(**key).update(count=F('count') + count)
        if rows:
            return
        try:
            McpUsageDay.objects.create(count=count, **key)
        except IntegrityError:
            # Another report created the day's first row between the update and
            # the insert. The unique constraint is what makes that fail rather
            # than quietly produce a second row for the count to split across,
            # so losing the race is safe and the increment is simply retried.
            McpUsageDay.objects.filter(**key).update(count=F('count') + count)
    except DatabaseError:
        logger.warning('MCP usage counter failed for tool %r', tool,
                       exc_info=True)


def _authorised(request) -> bool:
    """Constant-time check of the shared token.

    ``hmac.compare_digest`` rather than ``==`` for the usual reason, and the
    whole header is compared rather than a split field so a malformed one
    simply fails.
    """
    expected = f"Bearer {settings.MCP_USAGE_TOKEN}"
    given = request.META.get('HTTP_AUTHORIZATION', '')
    return hmac.compare_digest(given, expected)


@csrf_exempt
def report(request):
    """Take one usage report from the MCP service.

    **404 when no token is configured**, rather than an open door or a 500: an
    endpoint that writes to the database on an unauthenticated POST is not
    something to leave switched on by default, and a deployment that has not
    been given the token has not asked for this endpoint to exist.
    """
    if not settings.MCP_USAGE_TOKEN:
        raise Http404('MCP usage reporting is not configured.')
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    if not _authorised(request):
        return JsonResponse({'error': 'unauthorized'}, status=401)

    try:
        payload = json.loads(request.body or b'{}')
        if not isinstance(payload, dict):
            raise ValueError('not an object')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'expected a JSON object'}, status=400)

    tool = str(payload.get('tool') or '').strip()
    if not tool:
        return JsonResponse({'error': 'tool is required'}, status=400)
    client = str(payload.get('client') or '').strip()
    try:
        count = int(payload.get('count', 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, MAX_REPORT))
    # Absent means external. A reporter too old to send the flag, or one that
    # could not attribute its caller, must not have its calls quietly filed as
    # ours — over-reporting our own testing is the honest direction, inventing
    # somebody else's reach is not.
    internal = bool(payload.get('internal'))

    record(tool, client, count, internal=internal)
    # No body: the reporter drops the reply on the floor by design, and a
    # counter that answers with data invites somebody to read it back.
    return HttpResponse(status=204)
