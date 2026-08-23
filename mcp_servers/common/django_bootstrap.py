"""Boot Django once for the MCP test harness + data seed.

Server A itself talks to the database directly (see ``dbconn.py``) and needs no
Django. But the test suite migrates + seeds a throwaway ``pipeline_db`` and
renders a few app templates, and the ``seed_scratch`` helper uses the ORM — all
of which need a configured Django. This module sets that up idempotently.

By default this points Django at the LOCAL SQLite ``pipeline_db`` — it never
touches production. Setting ``PIPELINE_DATABASE_URL`` aims it elsewhere; until
then ``settings.py`` falls back to ``db_pipeline.sqlite3`` on its own.
"""
from __future__ import annotations

import os
import sys

from .paths import REPO_ROOT

_READY = False


def setup_django():
    """Idempotently configure and initialise Django. Returns the settings module."""
    global _READY
    if _READY:
        return os.environ["DJANGO_SETTINGS_MODULE"]

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OGA_website.settings")
    # Local dev/test default: keep DEBUG on so settings.py doesn't demand prod-only
    # config. The owner overrides this in their real environment.
    os.environ.setdefault("DEBUG", "True")

    # Allow synchronous ORM calls from an async context. FastMCP invokes tool
    # handlers inside an asyncio event loop, and Django normally refuses a sync ORM
    # call from there ("SynchronousOnlyOperation"). The MCP server is single-
    # threaded and its DB ops are short, so running them inline on the loop is fine.
    os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

    import django

    django.setup()
    _READY = True
    return os.environ["DJANGO_SETTINGS_MODULE"]
