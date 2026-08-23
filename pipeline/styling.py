"""Where the pipeline's stylesheet comes from — the one reader.

Two sources, and the app prefers the first:

1. ``pipeline/static/pipeline/tailwind.css``, built by ``bin/build_css.sh`` on
   deploy. Served from this site, so it is in the page before first paint, costs
   no third party, and cannot be blocked by a network that blocks CDNs.
2. ``cdn.tailwindcss.com``, which builds the CSS in the browser on every page
   load. Tailwind's own development build, and what this app ran on until now.

The fallback to the CDN is what keeps a local checkout working with no build
step: run ``runserver`` on a fresh clone and the pages still have styling. What
it must never do is happen quietly in production, so ``pipeline.W003`` says at
deploy time when the built file is missing outside DEBUG.

Cached, because otherwise this stats a file on every request of every page. The
cost is that a first local build needs a restart to be picked up — said out loud
in the build script's own output rather than left to be discovered.
"""
from __future__ import annotations

from functools import lru_cache

STYLESHEET = "pipeline/tailwind.css"


@lru_cache(maxsize=1)
def built_stylesheet_url() -> str | None:
    """The built stylesheet's URL, or ``None`` if it has not been built.

    Asked of the staticfiles finders rather than of the filesystem: that is the
    same question ``collectstatic`` answers, so a file this returns a URL for is
    one the deploy will actually have served.
    """
    from django.contrib.staticfiles import finders
    from django.templatetags.static import static

    if finders.find(STYLESHEET) is None:
        return None
    return static(STYLESHEET)
