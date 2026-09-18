"""Pre-live safety check: verify the read-only role's grants enforce the policy.

Run this AFTER applying ``roles.sql`` and BEFORE trusting Server A against a real
database. It connects with the role's own DSN and checks — using
``has_table_privilege`` (no data is written) plus one behavioural write attempt
(rolled back) — that:

  * readonly (Server A): can SELECT exactly the tables in
    ``mcp_servers/common/grants.py::ALLOWED`` and the recommendation view, CANNOT
    read any other ``pipeline_*`` table, CANNOT write, and CANNOT read
    ``auth_user`` at all; session is read-only with a statement timeout.

The allow-list is not restated here. One list, read by this, by
``manage.py apply_mcp_roles`` and by ``tests/test_grant_scope.py``.

(The write servers B/C were retired, so there are no writer/admin roles to check.)

The DSN comes from the same env var the server uses:
  readonly -> MCP_READONLY_DATABASE_URL
The role is skipped if the var is unset. Exit code is non-zero if any check fails.

    MCP_READONLY_DATABASE_URL=... python mcp_servers/preflight.py
"""
from __future__ import annotations

import os
import sys

# Runnable either way: `python mcp_servers/preflight.py` puts mcp_servers/ on the
# path, `python -m mcp_servers.preflight` puts the repo root there. Add the root
# explicitly so the shared allow-list imports under both.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.common import grants  # noqa: E402

# The table the behavioural write is attempted against. Still hardcoded, and
# that is fine: it proves the role cannot write, which is true of every table or
# none. WHICH tables it may READ is grants.ALLOWED's question, not this one —
# conflating the two is how this file came to check a single table and report a
# healthy role while six of nine tools were down.
LAB_TABLE = "pipeline_antibody"
VIEW = "antibody_recommendations"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _connect(dsn):
    import psycopg2

    if dsn.startswith("sqlite"):
        raise RuntimeError("preflight is for PostgreSQL roles, not SQLite.")
    return psycopg2.connect(dsn)


def _scalar(cur, sql, params=None):
    cur.execute(sql, params or [])
    return cur.fetchone()[0]


def _priv(cur, table, priv):
    return _scalar(cur, "SELECT has_table_privilege(%s, %s)", [table, f"{priv}"])


def _expect(results, label, ok):
    results.append((PASS if ok else FAIL, label))


def check_readonly(dsn):
    results = []
    conn = _connect(dsn)
    try:
        cur = conn.cursor()
        # Asked as the role itself, so has_table_privilege takes two arguments;
        # apply_mcp_roles asks the same questions as the owner with three. That
        # is the whole reason grants.audit takes `priv` as a parameter.
        cur.execute(grants.LAB_TABLES_SQL)
        present = [r[0] for r in cur.fetchall()]
        results.extend(grants.audit(present, lambda t, p: _priv(cur, t, p)))
        _expect(results, "can SELECT recommendation view", _priv(cur, VIEW, "SELECT"))
        ro = _scalar(cur, "SHOW default_transaction_read_only")
        _expect(results, "session default_transaction_read_only = on", ro == "on")
        timeout = _scalar(cur, "SHOW statement_timeout")
        _expect(results, f"statement_timeout set ({timeout})", timeout not in ("0", "0ms"))
        # Behavioural: an actual write must fail, then roll back.
        wrote = False
        try:
            cur.execute(f"UPDATE {LAB_TABLE} SET comments = comments WHERE false")
            wrote = True
        except Exception:
            wrote = False
        conn.rollback()
        _expect(results, "a real write is rejected", not wrote)
    finally:
        conn.close()
    return results


def _run(name, env_var, fn):
    dsn = os.environ.get(env_var)
    print(f"\n[{name}]  ({env_var})")
    if not dsn:
        print(f"  {SKIP}  not set — skipping")
        return True
    if dsn.startswith("sqlite"):
        print(f"  {SKIP}  SQLite DSN — preflight is for PostgreSQL roles")
        return True
    try:
        results = fn(dsn)
    except Exception as e:
        print(f"  {FAIL}  could not run checks: {e}")
        return False
    ok = True
    for status, label in results:
        print(f"  {status}  {label}")
        ok = ok and status == PASS
    return ok


def main():
    print("Preflight — verifying the read-only role's grants against the database")
    ok = True
    ok &= _run("readonly / Server A", "MCP_READONLY_DATABASE_URL", check_readonly)
    print("\n" + ("ALL CHECKS PASSED ✓" if ok else "SOME CHECKS FAILED ✗"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
