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

DB = "pipeline_db"

# roles.sql placeholder  ->  env var holding the real password
PW_ENV = {
    "CHANGE_ME_readonly": "MCP_READONLY_PW",
}

# role name  ->  env var (for the connection-URL hints at the end)
ROLE_ENV = {
    "mcp_readonly": "MCP_READONLY_PW",
}

VERIFY = [
    ("mcp_readonly", "pipeline_antibody", "SELECT", True),
    ("mcp_readonly", "pipeline_antibody", "INSERT", False),
    ("mcp_readonly", "pipeline_antibody", "DELETE", False),
    ("mcp_readonly", "auth_user", "SELECT", False),
]


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

        # --- verify the grants from the owner connection ---
        self.stdout.write("\nVerifying grants:")
        all_ok = True
        with conn.cursor() as cur:
            for role, table, priv, expect in VERIFY:
                cur.execute("SELECT has_table_privilege(%s, %s, %s)", [role, table, priv])
                got = bool(cur.fetchone()[0])
                ok = got == expect
                all_ok = all_ok and ok
                mark = self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL")
                verb = "can" if expect else "cannot"
                self.stdout.write(f"  {mark}  {role} {verb} {priv} {table}")

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
