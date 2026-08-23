"""Test harness: an ISOLATED, freshly-seeded local SQLite ``pipeline_db``.

Everything runs against a throwaway SQLite file (``test_pipeline.sqlite3`` in
this folder), never the developer's ``db_pipeline.sqlite3`` and never
production. We wire two env vars *before* Django boots so:

  * Django's ``pipeline_db`` (used by the migrate + seed step) -> the test file
  * Server A's ``MCP_READONLY_DATABASE_URL``                    -> the test file

The file is rebuilt (migrated + seeded) once per test session.
"""
from __future__ import annotations

import os
import sys

import pytest

# Repo root on the path so ``import mcp_servers...`` works.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_pipeline.sqlite3")

# Default: an isolated SQLite file. Override with MCP_TEST_DATABASE_URL to run the
# same suite against a real PostgreSQL (used to validate the Postgres code paths
# before going live) — e.g. postgres://user:pass@localhost:5432/pipeline_pg.
_OVERRIDE = (os.environ.get("MCP_TEST_DATABASE_URL") or "").strip()
TEST_DB_URL = _OVERRIDE or f"sqlite:///{TEST_DB}"
_IS_SQLITE = TEST_DB_URL.startswith("sqlite")

# Point every layer at the test DB. Must happen before Django loads.
os.environ["PIPELINE_DATABASE_URL"] = TEST_DB_URL
os.environ["MCP_READONLY_DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("PIPELINE_BASE_URL", "http://localhost:8000")
# Keep the audit log out of the committed tree during tests.
os.environ.setdefault(
    "MCP_AUDIT_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_audit.log"),
)


@pytest.fixture(scope="session", autouse=True)
def _seeded_pipeline_db():
    """Build a fresh migrated + seeded test pipeline_db for the whole session."""
    to_clear = [os.environ["MCP_AUDIT_LOG"]]
    if _IS_SQLITE:
        to_clear.append(TEST_DB)          # a fresh file per run (Postgres: reuse)
    for path in to_clear:
        if os.path.exists(path):
            os.remove(path)

    from mcp_servers.common.django_bootstrap import setup_django

    setup_django()

    from django.core.management import call_command

    if not _IS_SQLITE:
        # Reset the Postgres schema so each run is clean (SQLite gets a fresh file).
        call_command("flush", "--database=pipeline_db", verbosity=0, interactive=False)

    call_command("migrate", "--database=pipeline_db", "--run-syncdb",
                 verbosity=0, interactive=False)

    from mcp_servers import seed_scratch

    seed_scratch.run()
    yield


