"""Raw DB-API connections for Server A.

Server A (read-only analytics) talks to the database *directly* rather than
through Django, with its OWN least-privilege Postgres role in production (see
``roles.sql``). This module builds a DB-API 2.0 connection from an environment
variable so no credential ever lives in code.

Dialects:
  * ``postgres`` — ``psycopg2.connect(dsn)`` when the URL is a postgres:// DSN.
    This is the production path; the owner supplies the DSN + role in their env.
  * ``sqlite``   — the local dev fallback (``db_pipeline.sqlite3``), used for all
    local development and the test suite. Never production.

The env var lets the owner hand the server an independently-revocable credential:
  * Server A -> ``MCP_READONLY_DATABASE_URL``

If the chosen var is unset (or is a ``sqlite://`` URL) we connect to the local
SQLite pipeline db — this is what makes "build and test entirely locally"
possible without any real credential.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
import os

from .paths import LOCAL_PIPELINE_SQLITE


@dataclass
class Conn:
    """A thin wrapper pairing a DB-API connection with its dialect.

    ``placeholder`` is the paramstyle marker for that dialect so callers can
    build parameterised SQL that works on both backends.
    """
    raw: object
    dialect: str          # "sqlite" | "postgres"
    placeholder: str      # "?" for sqlite, "%s" for postgres

    def cursor(self):
        return self.raw.cursor()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollback()
        self.close()
        return False


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


def resolve_url(env_var: str) -> str:
    """Return the DSN for ``env_var``, defaulting to the local SQLite fallback."""
    url = (os.environ.get(env_var) or "").strip()
    if not url:
        return f"sqlite:///{LOCAL_PIPELINE_SQLITE}"
    return url


def connect(
    env_var: str,
    *,
    read_only: bool = False,
    statement_timeout_ms: Optional[int] = None,
) -> Conn:
    """Open a connection described by ``env_var``.

    ``read_only`` / ``statement_timeout_ms`` are enforced at the *session* level
    on Postgres (belt-and-braces on top of the GRANTs in ``roles.sql``). On
    SQLite they are best-effort: read-only is enforced by the app-layer SQL
    guard instead (SQLite has no per-session read-only transaction mode that
    blocks writes the way Postgres' ``default_transaction_read_only`` does).
    """
    url = resolve_url(env_var)

    if _is_postgres(url):
        import psycopg2

        conn = psycopg2.connect(url)
        with conn.cursor() as cur:
            if statement_timeout_ms:
                cur.execute("SET statement_timeout = %s", (statement_timeout_ms,))
            if read_only:
                cur.execute("SET default_transaction_read_only = on")
        conn.commit()
        return Conn(raw=conn, dialect="postgres", placeholder="%s")

    # sqlite:///abs/path  or  sqlite:///:memory:
    path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
    if not path:
        path = LOCAL_PIPELINE_SQLITE
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    return Conn(raw=raw, dialect="sqlite", placeholder="?")
