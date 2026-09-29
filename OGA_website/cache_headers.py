"""How long a public page may be held by a cache, and which pages may not be.

Django sends no `Cache-Control` header at all on the public site, and a cache
told nothing does nothing: Cloudflare sits in front of this origin and, on
20 Aug 2026, was serving 22 MB of a 147 MB day from cache — 14.93%. The other
125 MB came from Render, on a plan that includes 5 GB a month. This module is
the missing half of that sentence.

**It is one reader, not a decorator on thirty views.** A public page added next
month gets the header without anybody remembering to ask for it, which is the
failure this repo keeps meeting from the other direction — the rule that was
applied to one surface and not the next.

Four conditions decide it, and three of them are refusals:

- **A response that already carries `Cache-Control` is left exactly alone.**
  `core/api_pipeline.py` decides `public` versus `private` per key and
  WhiteNoise stamps a year on a content-hashed static file; both know more
  about their own reply than this does. This never overrides a decision
  somebody else made deliberately.
- **A response carrying `Set-Cookie` is never marked cacheable.** That is what
  makes the rule safe without a list of exceptions to maintain: a page with a
  CSRF form sets `csrftoken`, so `/contact/` and the validation recorder drop
  out by themselves rather than by being remembered. Checked 20 Aug 2026:
  `/champions/`, `/privacy-policy/` and `/robots.txt` set no cookie, and
  `/contact/` sets one.
- **A signed-in request is never marked cacheable**, so a member's own view of
  a page is never the copy that gets stored.
- And the member areas are named outright below, because belt and braces is
  cheap and a future page under `/pipeline/` must not depend on it having
  remembered to set a cookie.

**What this deliberately accepts.** Cloudflare stores one copy of a page and
serves it to everybody, and it honours `Vary` only for `Accept-Encoding` — so a
signed-in member reading a public page may get the copy rendered for a stranger.
That was checked before turning this on rather than assumed: the *only* thing
that differs is the Academy link's destination (`academy_home` versus `login`)
in `core/templates/core/header.html`, plus the words "Continue learning" versus
"Start learning" on the roadmap. **No public template renders a name, an email,
or anything else belonging to a person** — so what a cache can hand to the wrong
reader here is a wrong hyperlink, not somebody's data. If that link starts to
matter, the fix is to stop the page varying at all, not to stop caching it.

**And `Vary: Cookie` has to come off, or none of the above ever happens.** That
paragraph read Cloudflare's Vary support as *it will ignore the header and share
the copy anyway*. It does not: a response varying on anything but
`Accept-Encoding` is one Cloudflare declines to store at all. So from the day
this shipped until 23 Aug 2026 the home page went out with
`Cache-Control: public, max-age=300, s-maxage=3600` **and**
`Vary: Cookie, Accept-Encoding`, and every reply came back
`cf-cache-status: DYNAMIC` — asking to be cached and being refused, with nothing
on any screen saying so. The Cloudflare Cache Rule added that day changed
nothing on its own, which is what finally made it visible.

The header is there because every public page renders the Academy link, which
reads `request.user`, and Django marks any response that touches the session as
varying on `Cookie`. Dropping it is safe **only** on the replies `_may_be_cached`
has already cleared — anonymous, no `Set-Cookie`, 200, a GET, outside the member
areas — and for those, saying "this does not vary by cookie" is the same claim
the paragraph above already audited and accepted. `Accept-Encoding` stays: GZip
needs it and Cloudflare honours it.

**The freshness cost is stated, not hidden — and since 18 Sep 2026 this module
no longer sets it.** A gene page that gains a published figure used to be at
most `SHARED_MAX_AGE` out of date at the edge. The `public html` Cloudflare
Cache Rule now holds public pages for **7 days without revalidating**, set to
keep nightly crawls off a four-thread origin, so it overrides the value below
and `SHARED_MAX_AGE` governs only caches that do respect the origin. `MAX_AGE`
still governs browsers and is unaffected.

**What that leaves here is a header that no longer decides anything at the
edge**, which is worth knowing before tuning it: raising `SHARED_MAX_AGE` to
cut origin load would now change nothing, because the rule is already longer.
The escape hatch after a release is `bin/cloudflare_purge.py`, run once the new
instance is live — see the deploy chapter of CLAUDE.md, which also records that
this docstring and that chapter have disagreed about Purge Everything since
before the rule change, and that the disagreement is still open.
"""

# Browsers. Short on purpose: a person who reloads a page because a number
# looked wrong should get the current one, and a browser hit costs us nothing
# either way — the bandwidth this exists to save is at the edge, below.
MAX_AGE = 300

# Cloudflare. This is the number that does the work, and the one to raise if
# the origin is still serving too much. See the freshness note above.
SHARED_MAX_AGE = 3600

# Anything a member signs in to reach. None of these should ever be held by a
# shared cache, whatever headers the view happens to set.
NEVER_CACHED_PREFIXES = (
    '/pipeline/',
    '/academy/',
    '/accounts/',
    '/admin/',
    '/admin-tools/',
    '/api/',
    '/portal/',
)


# What a release changes, as Cloudflare sees it. Every reply is tagged with
# this except the few below, and releasing or withdrawing a figure purges the
# tag (`edge_cache.purge_public_pages`) rather than the whole zone.
RELEASE_TAG = 'oga-release'

# The replies a release never changes, and the only ones kept at the edge
# through it. **A list of what to keep, never of what to purge**, so it fails
# safe: a page nobody thought of is tagged and purged, where a list of pages
# to purge is the list that misses the next page to start showing a count.
# These are what made purging everything expensive (29 Sep 2026): the hashed
# static files, whose name changes when their bytes do, and the extension's
# 741 KB citation snapshot, which changes only when the CiteAb snapshot is
# rebuilt — both re-fetched from the origin by every Cloudflare location after
# each release. A deploy still purges everything (`bin/cloudflare_purge.py`).
KEPT_ACROSS_RELEASES = (
    '/static/',
    '/extension/citations.json',
    '/extension/firefox.xpi',
)


def _tag_for_release_purge(request, response):
    """Mark a reply as one a release may change. Cloudflare strips the header
    before it reaches a visitor."""
    if not request.path.startswith(KEPT_ACROSS_RELEASES):
        response['Cache-Tag'] = RELEASE_TAG


def _may_be_cached(request, response):
    """Whether this exact reply is safe for a shared cache to keep and reuse."""
    if request.method not in ('GET', 'HEAD'):
        return False
    if response.status_code != 200:
        return False
    # Somebody already decided this one. See the module docstring.
    if response.has_header('Cache-Control'):
        return False
    # A reply that sets a cookie is a reply about one visitor.
    if getattr(response, 'cookies', None):
        return False
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return False
    return not request.path.startswith(NEVER_CACHED_PREFIXES)


class PublicCacheHeadersMiddleware:
    """Stamp anonymous public pages with how long a cache may keep them.

    Sets nothing on anything it is not sure about, so the failure mode is the
    behaviour this site had yesterday — an uncached page — rather than a page
    held by a cache that should not have it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        _tag_for_release_purge(request, response)
        if _may_be_cached(request, response):
            response['Cache-Control'] = (
                f'public, max-age={MAX_AGE}, s-maxage={SHARED_MAX_AGE}')
            _stop_varying_on_cookie(response)
        return response


def _stop_varying_on_cookie(response):
    """Drop `Cookie` from `Vary`, keeping everything else.

    Only ever called on a response `_may_be_cached` has cleared. See the module
    docstring for why that is the whole of the safety argument, and for what
    this cost while it was missing.
    """
    values = [v.strip() for v in response.get('Vary', '').split(',') if v.strip()]
    kept = [v for v in values if v.lower() != 'cookie']
    if kept:
        response['Vary'] = ', '.join(kept)
    elif response.has_header('Vary'):
        del response['Vary']
