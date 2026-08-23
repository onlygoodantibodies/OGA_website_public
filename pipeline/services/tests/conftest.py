"""Test harness for the pipeline services: an isolated, migrated SQLite
``pipeline_db``. Wires PIPELINE_DATABASE_URL to a throwaway file BEFORE Django
boots (mirrors mcp_servers/tests/conftest.py), then migrates + yields the ORM.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_services.sqlite3")
os.environ["PIPELINE_DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OGA_website.settings")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


@pytest.fixture(scope="session", autouse=True)
def _pipeline_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    import django
    django.setup()
    from django.core.management import call_command
    call_command("migrate", "--database=pipeline_db", "--run-syncdb",
                 verbosity=0, interactive=False)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
