"""The endpoint Render asks "is the new instance ready yet?".

Why this exists at all: without a Health Check Path, a deploy is a stop-then-
start. On the 23 Aug 2026 deploy the old instance logged ``Shutting down:
Master`` at 07:57:21 and the new one logged ``Listening at 0.0.0.0:10000`` at
07:58:01 — forty seconds with nothing behind the proxy, which is the **502**
readers hit while an update is going out. Pointed at this path, Render keeps
the old instance serving until the new one answers, and the gap closes rather
than shortening.

It is **middleware, and first in the list**, rather than a view — deliberately,
because every ordinary way of answering this can fail in a way that is worse
than the 502 it is meant to remove. A health check that never passes does not
degrade the deploy, it **hangs** it: the new instance is never promoted, and the
site stays on the old code with no screen anywhere saying why.

Three things it therefore does not do.

It never calls ``request.get_host()``. ``ALLOWED_HOSTS`` is a fixed list with no
wildcard (settings.py), so a probe arriving with an internal address in the Host
header would be answered **400**, and 400 is not 200.

It never touches a database. Answering "ready" only when PostgreSQL is also
reachable sounds more honest and is the trap: a brief pipeline_db wobble would
then read as *the new instance is broken*, and roll the whole deploy back over a
database that was fine a second later. This answers one question — can this
process serve HTTP — and that is the question Render is asking.

And it short-circuits above ``SecurityMiddleware``, so an internal probe over
plain HTTP cannot be answered with a redirect if ``SECURE_SSL_REDIRECT`` is ever
turned on. A 301 is not 200 either.

``no-store`` because a cached "ok" is not a health check; Cloudflare sits in
front of this origin and would otherwise be entitled to answer for it.
"""
from __future__ import annotations

from django.http import HttpResponse

#: Both spellings, so it does not matter whether the dashboard field is typed
#: with a trailing slash. APPEND_SLASH would answer the bare one with a 301,
#: and a redirect fails a health check as surely as an error does.
HEALTH_PATHS = frozenset({"/healthz", "/healthz/"})


class HealthCheckMiddleware:
    """Answer ``/healthz`` with 200 before anything else can refuse it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # request.path is set from the raw path and needs no host validation.
        if request.path in HEALTH_PATHS:
            response = HttpResponse("ok\n", content_type="text/plain")
            response["Cache-Control"] = "no-store"
            return response
        return self.get_response(request)
