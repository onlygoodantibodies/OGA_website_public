"""Dropping Cloudflare's stored copies after a change the public should see.

The ``public html`` Cache Rule holds every public page at the edge for seven
days without revalidating (CLAUDE.md, Deploy), so releasing a figure changed
the gene page and the home page's counts and neither showed: a reader saw the
copy from before the release, and nothing on any screen said so (25 Sep 2026 —
TMEFF2 went public, the gene page appeared, the home page counts did not move).

So releasing and withdrawing purge. Three decisions:

* **Every page, not a list of URLs — and not the files a release cannot
  change.** A release changes the gene page, the home page's three counts, the
  gene index, the sitemap, the extension index and the API — and a list someone
  keeps is the list that misses the next page to start showing a count.
  Purge-by-URL also misses ``www.``, which is a served hostname. So every reply
  carries a ``Cache-Tag`` (``cache_headers.RELEASE_TAG``) *except* the few in
  ``cache_headers.KEPT_ACROSS_RELEASES``, and this purges the tag: a list of
  what to keep fails safe, where a list of what to purge does not. Until
  29 Sep 2026 it purged the whole zone, which also emptied every Cloudflare
  location of the static images and the extension's 741 KB citation snapshot,
  each then re-fetched from the origin — and origin bytes are Render's
  bandwidth bill. Tag purges are on the free plan (5 a minute). **A page
  stored before its reply carried the tag is not reached by one**, which is why
  the deploy that brought the tag in has to purge everything, as every deploy
  does.
* **After the write has committed, never inside it.** A purge that runs before
  the commit lets the edge refill with the old page for a week — the same trap
  as purging during a deploy — and an outbound call inside a transaction is
  rule 3 of ``tests_timeouts.py``.
* **A failed purge is said, never raised.** The release has happened; the
  receipt says the public pages may take up to a week to show it and what to do.
  An unset token is the same sentence, because to the person pressing Release it
  is the same fact.

The token is scoped to *Zone : Cache Purge : Purge* on this zone only
(``bin/cloudflare_purge.py``), so holding it on the web service lets it do
nothing else.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request

from OGA_website.cache_headers import RELEASE_TAG

logger = logging.getLogger(__name__)

#: Short, because this runs on one of the site's four threads while a person
#: waits for their receipt. Cloudflare answers a purge in well under a second.
TIMEOUT_SECONDS = 5

NOT_DONE = ("The public pages may take up to a week to show this: the site's "
            "cache could not be cleared automatically. Ask for a purge (or run "
            "oga-purge) to show it now.")


def _under_test() -> bool:
    """Never from a test run. The cloud sessions that run the suite carry the
    real token (so "purge" can be asked for from a phone), and a release test
    driven through the view would otherwise empty the live site's cache every
    time the suite ran — harmless once, and a thing nobody would ever notice
    doing it. Both runners: Django's ``manage.py test`` and pytest."""
    return sys.argv[1:2] == ["test"] or "pytest" in sys.modules


def forget_public_indexes() -> None:
    """Drop the site's own cached lists of what is published.

    Cloudflare is not the only cache a release has to empty. The home page's
    search box, the public antibody search and the extension index are each
    built once from the published figures and kept for an hour in Django's
    cache, and nothing cleared them: PARP1 went public on 28 Sep 2026 and the
    search box answered "No match for parp" beside a home page already counting
    it. Local work, no network, so it runs whether or not the purge can.
    """
    from django.core.cache import cache

    from core import public_search
    from core.extension_index import EXTENSION_INDEX_CACHE_KEY
    from core.views import GENE_SEARCH_INDEX_CACHE_KEY

    cache.delete_many([GENE_SEARCH_INDEX_CACHE_KEY, public_search.CACHE_KEY,
                       EXTENSION_INDEX_CACHE_KEY])
    cache.set(INDEX_VERSION_KEY, _new_version(), None)


#: Rides on the gene list's address as ``?v=`` (``search_index_version``), so
#: a release gives browsers a new address to fetch. Emptying the server's copy
#: was not enough: the reply tells a browser it may keep the list for an hour,
#: and one that fetched it just before a release went on answering "No match"
#: for the new gene (PARP1, 28 Sep 2026). A restart starts a new version too,
#: which costs a browser one extra fetch and is never wrong.
INDEX_VERSION_KEY = "public_index_version"


def _new_version() -> str:
    import time

    return str(time.time_ns())


def index_version() -> str:
    """The current version of the published-figure indexes."""
    from django.core.cache import cache

    return cache.get_or_set(INDEX_VERSION_KEY, _new_version, None)


def purge_public_pages() -> tuple[bool, str]:
    """Purge every page a release can change. ``(done, sentence for the page)``.

    Every caller is a change to what is published, so it also forgets the
    site's own indexes of that (`forget_public_indexes`) — first, and even when
    the purge itself is skipped."""
    forget_public_indexes()
    if _under_test():
        return False, NOT_DONE
    zone = os.environ.get("CLOUDFLARE_ZONE_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_PURGE_TOKEN", "").strip()
    if not zone or not token:
        logger.warning("edge purge skipped: CLOUDFLARE_ZONE_ID / "
                       "CLOUDFLARE_PURGE_TOKEN not set")
        return False, NOT_DONE
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
        data=json.dumps({"tags": [RELEASE_TAG]}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        logger.exception("edge purge failed")
        return False, NOT_DONE
    if not body.get("success"):
        logger.error("edge purge refused: %s", body.get("errors"))
        return False, NOT_DONE
    return True, ("The site's cache has been cleared, so the public pages "
                  "show this now.")
