"""Apply mcp_servers/roles.sql to the live pipeline_db from the Render shell.

The gentle way to run the MCP database roles without psql: this reads the
committed ``roles.sql``, fills the passwords in from env vars (never hardcoded,
never printed), applies it using the connection the site already uses, then
verifies the grants landed correctly.

Usage (Render → OGA_website → Shell):

    export MCP_READONLY_PW='...'      # openssl rand -hex 24
    python manage.py apply_mcp_roles          # prompts before applying
    python manage.py apply_mcp_roles --dry-run  # show the plan, change nothing

roles.sql is idempotent (CREATE ROLE IF NOT EXISTS / CREATE OR REPLACE VIEW /
GRANT), so re-running is safe. Only the read-only role remains (the tools/admin
write roles were retired). The command prints the ready-to-paste connection URL
at the end.
"""
from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from mcp_servers.common import grants

DB = "pipeline_db"

# roles.sql placeholder  ->  env var holding the real password
PW_ENV = {
    "CHANGE_ME_readonly": "MCP_READONLY_PW",
}

# role name  ->  env var (for the connection-URL hints at the end)
ROLE_ENV = {
    "mcp_readonly": "MCP_READONLY_PW",
}

# Verification is `grants.audit`, over EVERY pipeline table in the database.
#
# It used to be the four lines below, all about `pipeline_antibody`:
#
#     ("mcp_readonly", "pipeline_antibody", "SELECT", True), ... etc
#
# On 30 Aug 2026 that printed four green PASS lines while six of the connector's
# nine tools were failing on `permission denied for table
# pipeline_antibodyoutcome`. Sampling one table says nothing about the other 32,
# and the one it sampled was never the broken one. A guard that cannot fail is
# not a guard.


class Command(BaseCommand):
    help = "Apply mcp_servers/roles.sql to pipeline_db (passwords from env vars)."
    requires_system_checks = []   # skip unrelated app warnings (e.g. ckeditor)

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show the target + plan without changing anything.")
        parser.add_argument("--yes", action="store_true",
                            help="Skip the confirmation prompt.")

    def handle(self, *args, **opts):
        path = os.path.join(settings.BASE_DIR, "mcp_servers", "roles.sql")
        if not os.path.exists(path):
            raise CommandError(f"roles.sql not found at {path}")
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()

        # --- passwords: required, non-placeholder, minimally strong ---
        missing = [env for env in PW_ENV.values() if not (os.environ.get(env) or "").strip()]
        if missing:
            raise CommandError(
                "Set a strong password for each role first:\n  "
                + "\n  ".join(f"export {m}='...'" for m in missing))
        for placeholder, env in PW_ENV.items():
            pw = os.environ[env].strip()
            if "CHANGE_ME" in pw or len(pw) < 12:
                raise CommandError(f"{env} is too weak/placeholder — use >= 12 random chars.")
            sql = sql.replace(f"'{placeholder}'", "'" + pw.replace("'", "''") + "'")

        # --- confirm the target database ---
        conn = connections[DB]
        cfg = conn.settings_dict
        engine, host = cfg["ENGINE"], (cfg.get("HOST") or "(local)")
        port, name = (cfg.get("PORT") or "5432"), cfg.get("NAME")
        self.stdout.write(f"Target pipeline_db:  host={host} port={port} db={name}")
        self.stdout.write(f"                     engine={engine}")
        if "postgresql" not in engine:
            raise CommandError("pipeline_db is not PostgreSQL — roles.sql is Postgres-only.")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — roles.sql would be applied (passwords redacted). No changes made."))
            self.stdout.write(
                f"\nThen SELECT would be granted on {len(grants.ALLOWED)} tables, "
                "and revoked everywhere else:")
            for table, why in sorted(grants.ALLOWED.items()):
                self.stdout.write(f"  {table:<32} {why}")
            self._print_dsns(host, port, name)
            return

        if not opts["yes"]:
            ans = input("Apply roles.sql to the database above? [y/N] ").strip().lower()
            if ans != "y":
                self.stdout.write("Aborted — nothing changed.")
                return

        # --- apply (one simple-protocol call = one server-side transaction) ---
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        except Exception as e:
            msg = str(e).lower()
            if "permission denied" in msg or "must have" in msg or "createrole" in msg:
                raise CommandError(
                    "The pipeline_db user isn't allowed to create roles.\n"
                    f"  ({e})\n"
                    "Fix: run this as a database user with the CREATEROLE privilege "
                    "(on Render, the database's owner user), or have an admin run "
                    "`ALTER ROLE <this_user> CREATEROLE;` once.")
            raise CommandError(f"roles.sql failed (nothing committed): {e}")
        self.stdout.write(self.style.SUCCESS("roles.sql applied."))

        # roles.sql grants no tables (it revokes); the allow-list is applied here
        # from the one place it is written down.
        try:
            with conn.cursor() as cur:
                for stmt in grants.grant_statements():
                    cur.execute(stmt)
        except Exception as e:
            raise CommandError(f"applying the table allow-list failed: {e}")
        self.stdout.write(self.style.SUCCESS(
            f"Allow-list applied: SELECT on {len(grants.ALLOWED)} tables."))

        # --- verify the grants from the owner connection ---
        self.stdout.write("\nVerifying grants:")
        all_ok = True
        with conn.cursor() as cur:
            cur.execute(grants.LAB_TABLES_SQL)
            present = [r[0] for r in cur.fetchall()]

            def priv(table, privilege):
                cur.execute("SELECT has_table_privilege(%s, %s, %s)",
                            [grants.ROLE, table, privilege])
                return bool(cur.fetchone()[0])

            for ok, label in grants.audit(present, priv):
                all_ok = all_ok and ok
                mark = self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL")
                self.stdout.write(f"  {mark}  {grants.ROLE} {label}")

        if not all_ok:
            raise CommandError("Some grants are not as expected — do NOT trust the roles yet.")
        self.stdout.write(self.style.SUCCESS("\nAll grants verified."))
        self._print_dsns(host, port, name)

    def _print_dsns(self, host, port, name):
        self.stdout.write("\nConnection URL for the Render service env var "
                          "(fill in the password from your env):")
        for role, env in ROLE_ENV.items():
            var = {"mcp_readonly": "MCP_READONLY_DATABASE_URL"}[role]
            self.stdout.write(
                f"  {var} = postgresql://{role}:<{env}>@{host}:{port}/{name}")
