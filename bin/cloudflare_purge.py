#!/usr/bin/env python3
"""
Purge the Cloudflare edge cache for onlygoodantibodies.co.uk.

Why this exists
---------------
The "public html" cache rule now tells Cloudflare to keep every public page
for 7 days without asking Render whether it has changed. That is what stops
nightly crawls from reaching the origin -- four health-check restarts in the
week to 18 Sep 2026, every one of them a crawler walking the site at three to
four pages a second while the whole site is four gunicorn threads. It also
means a deploy or a data refresh is invisible to visitors until the stored
copies are dropped. This script drops them.

**The freshness cost is now a week, not an hour, and that is the whole reason
this script exists.** Before the rule change a stale page corrected itself
within the hour and purging was optional. It is not optional now: a deploy
nobody purges after is a deploy readers may not see until the following
weekend.

When to run it -- and when NOT to
---------------------------------
Run it **after the new code is live**, i.e. after Render's Manual Deploy has
finished and the new instance is serving. The owner presses Manual Deploy by
hand, so running this by hand a minute later is the natural pairing.

**Do not put it in the Build Command, and do not put it in the Pre-Deploy
Command.** Both run while the *old* instance is still serving traffic: Render
builds, migrates, starts the new instance, waits for `/healthz`, and only then
switches traffic over. A purge before that switch empties the cache and the
old instance immediately refills it with the old pages -- which are now held
for **seven days**. That turns a tidy-looking deploy step into a week of stale
content, and it is the one wiring that is worse than not purging at all.
Render has no post-deploy hook, which is why this is a manual step.

Setup (one-off, done by you, not by this script)
------------------------------------------------
1. Cloudflare dashboard -> My Profile -> API Tokens -> Create Token.
   Use the "Custom token" option with exactly one permission:
       Zone : Cache Purge : Purge
   and scope it to the single zone onlygoodantibodies.co.uk.
   That token can do nothing except purge this site's cache.
2. Export the two variables wherever you run this from (your laptop, or
   Render Shell on the web service):
       CLOUDFLARE_ZONE_ID      (Overview page of the zone, right-hand column)
       CLOUDFLARE_PURGE_TOKEN  (the token from step 1)
   It does not need to be set on the Render service itself unless you intend
   to run it from Render Shell.

Usage
-----
    python bin/cloudflare_purge.py            # purge everything (default)
    python bin/cloudflare_purge.py /antibodies/APOE/ /about/
                                              # purge just these paths

It exits non-zero if the purge fails, or if the two variables are not set, so
a broken token is noticed rather than silently leaving stale pages up for a
week.

Two things to know about the choice between them
------------------------------------------------
**Purging everything also drops `/extension/*.json` and the hashed static
assets.** CLAUDE.md's deploy chapter says never to Purge Everything for
exactly that reason. That rule was written when the edge TTL was an hour and
waiting was cheap; with a seven-day TTL the trade has moved, and on the free
plan there is no third option -- Cloudflare offers purge-by-prefix and
purge-by-tag only on Enterprise, so "everything" and "a list of URLs" are the
only two tools available. Resolve which way you want it before wiring this
into anything routine; the conflict is real and this docstring is not the
place it gets settled.

**Purging by path only purges the hostname spelled in SITE.** The site also
answers on `www.onlygoodantibodies.co.uk` (both are in ALLOWED_HOSTS), and a
`files` purge matches the exact URL, so the www copy of a page survives a
purge aimed at the apex. Purging everything covers every hostname in the zone
and does not have this hole.
"""

import json
import os
import sys
import urllib.error
import urllib.request

SITE = "https://onlygoodantibodies.co.uk"


def purge(zone_id: str, token: str, paths: list[str]) -> dict:
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache"
    if paths:
        body = {"files": [SITE + p if p.startswith("/") else p for p in paths]}
    else:
        body = {"purge_everything": True}

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main(argv: list[str]) -> int:
    zone_id = os.environ.get("CLOUDFLARE_ZONE_ID")
    token = os.environ.get("CLOUDFLARE_PURGE_TOKEN")
    if not zone_id or not token:
        print("cloudflare_purge: CLOUDFLARE_ZONE_ID and CLOUDFLARE_PURGE_TOKEN "
              "must be set; skipping purge", file=sys.stderr)
        # Exit 0 here if you would rather a missing token not fail a deploy;
        # non-zero is deliberate so misconfiguration is loud.
        return 1

    paths = argv[1:]
    try:
        result = purge(zone_id, token, paths)
    except urllib.error.HTTPError as e:
        print(f"cloudflare_purge: HTTP {e.code}: {e.read().decode(errors='replace')}",
              file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"cloudflare_purge: request failed: {e.reason}", file=sys.stderr)
        return 1

    if not result.get("success"):
        print(f"cloudflare_purge: API reported failure: {result.get('errors')}",
              file=sys.stderr)
        return 1

    what = f"{len(paths)} path(s)" if paths else "everything"
    print(f"cloudflare_purge: purged {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
