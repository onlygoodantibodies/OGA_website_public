"""Public pages answer on one hostname, and the origin's own name is not it.

Render gives every web service a hostname of its own —
``oga-website.onrender.com`` — and this site answers on it as well as on the
apex, because the apex is a proxied CNAME *to* it. That second door was
harmless while nobody knew it was there. On **19 Sep 2026** the owner found it
in Google's index, which means it is not a second door any more: it is a second
site, carrying the same 585 gene pages under a different name.

**Neither of the protections the site has reaches it.** The `public html` Cache
Rule that holds public pages at the edge for 7 days and the *page crawl
throttle* that blocks 30 GETs in 10 seconds from one IP are both rules on the
Cloudflare **zone**, and ``onrender.com`` is Render's zone, not ours — nothing
we can configure there will ever apply. So a request addressed to the origin
hostname arrives at four gunicorn threads with nothing in front of it, which is
the exact ceiling that produced four health-check restarts in the week to
18 Sep 2026 (DECISIONS.md). Measured overnight on 18–19 Sep: crawl-shaped
traffic carrying ``Referer: https://oga-website.onrender.com``, rotating
desktop user agents, walking ``/antibodies/<GENE>`` unslashed, in clusters of
about six requests in four seconds. It did no harm that night. It arrived by
the one path where nothing would have stopped it if it had.

**301, not 302**, because the job is to collapse the duplicate in search rather
than to deflect a visit — a permanent redirect is what tells Google to fold the
origin's pages into the apex's. The cost is that browsers cache it hard and for
a long time, so a person who has once been redirected cannot easily reach the
origin hostname from that browser again. That is why ``/healthz`` stays
answerable below: it is the one thing you might still want from the origin, and
``curl`` caches nothing.

**Since 27 Sep 2026 this is the second line, not the first.** The owner switched
off Render's public subdomain on the service (*Render Subdomain* on its Settings
page; the API reports ``renderSubdomainPolicy: disabled``), so
``oga-website.onrender.com`` now answers 404 at Render and no request on it
reaches this code. The middleware stays because it costs nothing and because
switching the subdomain back on is one toggle nobody would connect to Google's
index. It also means the origin is no longer reachable from outside at all —
``/healthz`` included — and the MCP's usage reports go over Render's private
network instead (``mcp_servers/common/usage.py``).

Three refusals shape it.

- **GET and HEAD only.** A 301 turns a POST into a GET and drops the body, so
  every other method passes through untouched. Losing a write to a redirect is
  the silent-omission failure this repo keeps meeting; a redirect is not worth
  one.
- **An explicit list of hostnames to redirect, never "anything that is not the
  apex".** The list fails safe: a hostname nobody wrote down goes on being
  served, exactly as it is today. The inclusive spelling fails the other way —
  a custom domain added next year would start redirecting itself with nothing
  on any screen saying why. Same reasoning as the cron jobs' ignored-paths
  filter in CLAUDE.md, and the same trade.
- **Two prefixes are never redirected**, named outright below.

**The .com folds into the .co.uk too** (owner's decision, 27 Sep 2026). It was
left off this list until then on purpose, because which domain is the site is
the owner's call, not a bug fix's. ``onlygoodantibodies.com`` is registered to
the university, whose DNS points it straight at Render (an ``A`` record to
Render's load balancer, not through our Cloudflare zone), and on 27 Sep it was
moved from a suspended Render service onto this one. So it has the same
exposure as the onrender name had: no edge cache and no crawl throttle in front
of it, which is exactly why the redirect lives here, above everything that
costs anything. ``www.onlygoodantibodies.com`` is listed too, although the
university's DNS has no record for it yet; if one is ever added it redirects
with no further change here.
"""
from __future__ import annotations

from django.http import HttpResponsePermanentRedirect

#: Where a redirected request is sent. A constant, so this can never be turned
#: into an open redirect by anything a caller sends.
CANONICAL_ORIGIN = 'https://onlygoodantibodies.co.uk'

#: The hostnames that are not the site's name. None of them passes through our
#: Cloudflare zone.
REDIRECTED_HOSTS = frozenset({
    # This service's own Render names.
    'oga-website.onrender.com',
    'only-good-antibodies.onrender.com',
    # The .com, registered to the university and pointed straight at Render.
    'onlygoodantibodies.com',
    'www.onlygoodantibodies.com',
})

#: What must keep answering on the origin hostname whatever else changes.
#:
#: ``/healthz`` is Render's health check, and a health check that is answered
#: with a 301 fails exactly as surely as one answered with a 500 — the new
#: instance is never promoted and the deploy hangs on the old code. In practice
#: ``HealthCheckMiddleware`` sits above this one and the probe never reaches
#: here at all; it is named anyway because belt and braces is free and the
#: consequence of the ordering being changed in a merge is the whole site.
#:
#: ``/internal/`` is how the MCP connector reports its usage
#: (``MCP_USAGE_URL``), pointed at the origin *on purpose*: the same POST to the
#: apex is bounced 403 by Cloudflare's Bot Fight Mode, which reads
#: ``Python-urllib`` and never reaches Django at all. Since 27 Sep 2026 it
#: arrives as ``oga-website`` over Render's private network, which is not in
#: ``REDIRECTED_HOSTS`` either; the exemption stays so that pointing it back at
#: an onrender name can never turn a report into a redirect.
NEVER_REDIRECTED_PREFIXES = ('/healthz', '/internal/')


def _redirect_target(request) -> str | None:
    """The canonical URL for this request, or ``None`` to leave it alone."""
    if request.method not in ('GET', 'HEAD'):
        return None
    if request.path.startswith(NEVER_REDIRECTED_PREFIXES):
        return None
    # The raw header, not `get_host()`: that one validates against
    # ALLOWED_HOSTS and raises, and this middleware has no business deciding
    # what an unknown host gets — CommonMiddleware already answers that, later
    # and in one place. A header that is not on the list is simply not ours.
    host = request.META.get('HTTP_HOST', '').split(':')[0].lower()
    if host not in REDIRECTED_HOSTS:
        return None
    return CANONICAL_ORIGIN + request.get_full_path()


class CanonicalHostMiddleware:
    """Send a public page request on the origin's own hostname to the site.

    Sits above everything that costs anything — no session, no database, no
    template — so a crawler that has found the origin hostname is answered with
    a redirect and none of the four threads is held while a page renders.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        target = _redirect_target(request)
        if target is not None:
            return HttpResponsePermanentRedirect(target)
        return self.get_response(request)
