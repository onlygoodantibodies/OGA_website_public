"""Take an off-platform backup of ``academy_db`` and put it in private storage.

    ACADEMY_SQLITE_URL=sqlite:////tmp/academy-backup.sqlite3 \\
        python manage.py academy_backup              # take one, upload it, prune
    python manage.py academy_backup --dry-run        # say what it would do
    python manage.py academy_backup --keep 60
    python manage.py academy_backup --dir /tmp       # keep the file locally too

Built for a **Render Cron Job**, alongside the two that already exist. Register
#82 is the reason: when academy was one SQLite file on a disk, anybody could copy
it in one command, and people did. On PostgreSQL its whole safety net is Render
PITR (**3 days** on Basic) plus a manual Export (7 days) — both inside the Render
account, so both share one fate, and neither is scheduled.

**It needs a scratch SQLite database to copy into, and that is
``ACADEMY_SQLITE_URL``** — the same variable and the same ``academy_sqlite``
alias the PostgreSQL move used. Any writable path in the cron container will do:
it is emptied and rewritten every run and dies with the container.

Naming it in the environment rather than conjuring an alias at runtime is
deliberate, and was arrived at the hard way. ``db_router`` decides which aliases
may carry the academy schema from a hardcoded pair, so an invented third name is
refused by ``allow_migrate`` and ``migrate`` then reports success over a database
with **no tables in it** — the quiet direction. Registering the known alias
dynamically instead works, and then fights every layer that also manages it: the
test runner opens an atomic block on it, closing it strands the cached
connection, and tearing it down on the *refusal* path destroys the very migration
window the refusal exists to protect. One mechanism, which the router, settings
and both existing commands already agree about, is worth more than the
convenience of not setting a variable.

**Not dry-run by default, unlike the commands that change live data.** That rule
protects production rows from a write nobody previewed; this reads ``academy_db``
and writes nowhere near it. A backup command that previewed by default would run
every night, report success, and store nothing — this project's characteristic
defect, a silent omission reading as success, applied to the one thing whose
whole job is to still be there later.

**It refuses to write to a public bucket, and that refusal is the point.**
``attachment_storage()`` deliberately falls back to the media bucket when
``R2_ATTACHMENTS_BUCKET`` is unset, because for a gel scan "stored somewhere
readable" beats "the site is down". That trade inverts here. This file holds
**every password hash and every live API key in the system**, the media bucket is
served by a public Cloudflare domain, and the object key is derived from the
date — so a fallback would put a guessable, permanently public URL on other
people's credentials, and nothing on any screen would say so. Nothing is up or
down if this refuses: it costs one night and a failure email.

**Why a SQLite copy rather than ``pg_dump``.** Two reasons, one decisive. The
copy is **provable** — ``academy_db_compare --deep`` reads back every field of
every row across both engines and answers in its exit status, so the backup is
verified at the moment it is taken rather than hoped about until a restore; a
``pg_dump`` cannot be checked without restoring it somewhere. And the restore
path is one that has already done this exact job for real: ``academy_db_copy``
is what moved 7,541 rows onto PostgreSQL on 23 Aug 2026, keys intact. Secondary
but it would have bitten: ``pg_dump`` is a binary that may not be in Render's
Python image at all, and must not be older than the server — which is PostgreSQL
18. This route is pure Python and cannot go stale that way.

**It refuses to back up the wrong database.** Without ``ACADEMY_DATABASE_URL``
the alias falls back to a SQLite file the container invents, and a copy of *that*
succeeds, verifies clean and stores — an empty backup, every night, reported
green. ``academy_db.refusal()`` already answers "this is not the durable academy
database"; here it is fatal rather than a warning, because nothing is serving.

**The verify gates the upload.** A backup that failed its own check and was
stored anyway is worse than no backup, because the failure is now recorded as a
success. If ``--deep`` disagrees, nothing is uploaded and the command exits
non-zero — which is what makes Render mark the run failed and send the email.

**Restoring**, when it is needed: create the database, ``migrate
--database=academy_db``, then point ``ACADEMY_SQLITE_URL`` at the decompressed
file and run ``academy_db_copy --source academy_sqlite --target academy_db
--apply``, then ``academy_db_compare --deep`` to prove it landed. The same two
commands as the original move, in the same direction.
"""
from __future__ import annotations

import gzip
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from OGA_website import academy_db

#: Object keys sort lexicographically into date order, so "the newest" and "the
#: oldest N" are both answerable without reading any metadata.
PREFIX = "academy-backup/"
KEEP = 30

#: The scratch alias to copy into. Registered by settings only when
#: ``ACADEMY_SQLITE_URL`` is set, which is what makes "where does this write"
#: a question with an answer rather than something this command decides.
TARGET_ALIAS = academy_db.SOURCE_ALIAS


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def destination_refusal(storage) -> str:
    """Why this file must not go where it is about to go, or ''.

    Read by the command and by the test. The check is on the bucket the storage
    actually resolved to, not on the setting: the setting being present is not
    the same as it having taken effect.
    """
    from django.conf import settings

    if not getattr(settings, "USE_R2", False):
        return ""  # Local run: the file lands under MEDIA_ROOT, which is the point.

    if not getattr(settings, "R2_ATTACHMENTS_BUCKET", ""):
        return (
            "R2_ATTACHMENTS_BUCKET is not set, so this backup would be written to "
            "the media bucket — which is served by a public Cloudflare domain, "
            "under a key derived from today's date. That would publish every "
            "password hash and every live API key in the academy database at a "
            "guessable URL. Set R2_ATTACHMENTS_BUCKET to a bucket with no public "
            "access and run this again. Nothing has been written."
        )
    if getattr(storage, "custom_domain", None):
        return (
            f"The backup storage resolved to a bucket with a public custom domain "
            f"({storage.custom_domain}), which would make this file readable by "
            "anyone with the URL. Nothing has been written."
        )
    return ""


class Command(BaseCommand):
    help = "Back up academy_db to a verified SQLite copy in private storage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep", type=int, default=KEEP, metavar="N",
            help=f"How many backups to retain in storage (default {KEEP}). "
                 "Older ones are deleted after a successful upload.")
        parser.add_argument(
            "--target", default="", metavar="ALIAS",
            help=f"Scratch alias to copy into (default {TARGET_ALIAS!r}, which "
                 "settings registers when ACADEMY_SQLITE_URL is set). It is "
                 "emptied before the copy, so it must not name anything worth "
                 "keeping.")
        parser.add_argument(
            "--dir", default="", metavar="PATH",
            help="Also leave the .sqlite3.gz here. For a Shell session or a "
                 "local run; the cron job does not use it.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Say what would happen. Takes no copy and writes nothing.")

    # ------------------------------------------------------------------ main
    def handle(self, *args, **options):
        from pipeline.storages import attachment_storage

        # Argument errors first, and named whatever the environment looks like:
        # a mistyped flag should not be reported as a missing variable.
        keep = options["keep"]
        if keep < 1:
            raise CommandError("--keep must be at least 1: this command deletes "
                               "old backups, and keeping none deletes them all.")

        target = options["target"] or TARGET_ALIAS
        if target == academy_db.ALIAS:
            raise CommandError(
                f"--target is {target!r}, which is the database being backed up. "
                "The copy empties its target before it writes, so this would "
                "destroy it.")

        # Back up the real database, or nothing. Without ACADEMY_DATABASE_URL the
        # alias falls back to a SQLite file the container invents, and a copy of
        # that would succeed, verify clean and store — an empty backup, every
        # night, reported green. `academy_db.refusal()` is the existing reader for
        # "this is not the durable academy database"; core.W004 says the same at
        # deploy, where it is only a warning because refusing to boot would take
        # the site down. Here refusing costs one run.
        # `refusal()` is silent under DEBUG, where a local SQLite file is the
        # point — and DEBUG defaults to *True*, so an unset variable on a fresh
        # Render service turns the guard below off without saying so. Say so.
        from django.conf import settings
        if settings.DEBUG:
            self.stdout.write(self.style.WARNING(
                "DEBUG is on, so the durability check is skipped and academy_db "
                "is whatever this environment points at. Expected locally; on a "
                "Render cron job it means DEBUG is unset, and the default is "
                "True — set DEBUG=False there."))

        source_why = academy_db.refusal()
        if source_why:
            raise CommandError(
                f"Refusing to back up: {source_why}\n\nA backup of the wrong "
                "database is worse than none, because it reports success. "
                "Nothing has been written.")

        # Twice, and in this order. The settings-level half must run *before*
        # `attachment_storage()` is built, because a half-configured R2 makes
        # constructing it raise on a missing AWS_* name — and an AttributeError
        # is not a refusal: it tells the operator nothing about which variable to
        # set. The second call is the one that reads the bucket the storage
        # actually resolved to, which is the check that matters.
        why = destination_refusal(None)
        if why:
            raise CommandError(why)

        storage = attachment_storage()
        why = destination_refusal(storage)
        if why:
            raise CommandError(why)

        if target not in connections.databases:
            raise CommandError(
                f"No database alias called {target!r}, so there is nowhere to copy "
                "into. Set ACADEMY_SQLITE_URL to a scratch file for this run, e.g. "
                "ACADEMY_SQLITE_URL=sqlite:////tmp/academy-backup.sqlite3 — it is "
                "emptied and rewritten each time, so any path the container can "
                "write to will do. Nothing has been written.")

        name = f"academy-{_stamp()}.sqlite3.gz"
        key = f"{PREFIX}{name}"

        if options["dry_run"]:
            self.stdout.write(
                f"Would copy {academy_db.ALIAS} into {target!r}, verify it with "
                f"academy_db_compare --deep, and store it as {key}.")
            self._report_retention(storage, keep)
            return

        workdir = Path(tempfile.mkdtemp(prefix="academy-backup-"))
        try:
            gz_path = self._build_verified_copy(workdir, name, target)
            size_kb = gz_path.stat().st_size / 1024

            with gz_path.open("rb") as fh:
                stored = self._store(storage, key, fh)
            self.stdout.write(self.style.SUCCESS(
                f"Stored {stored} ({size_kb:,.0f} KB), verified field-by-field."))

            if options["dir"]:
                dest = Path(options["dir"]) / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gz_path, dest)
                self.stdout.write(f"Also written to {dest}")

            self._prune(storage, keep)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _store(self, storage, key: str, fh):
        """Upload, and turn a storage error into a sentence.

        Every other refusal in this command names what to set; this one is the
        step most likely to fail while somebody is wiring it up, and it was the
        one that answered with a raw botocore traceback. A **403** here is
        specifically a token problem rather than a missing object: a token that
        can see the bucket gets 404 for a key that is not there, so Forbidden
        means the credentials do not carry this bucket.
        """
        try:
            return storage.save(key, fh)
        except Exception as exc:
            code = getattr(exc, "response", {}).get(
                "ResponseMetadata", {}).get("HTTPStatusCode")
            bucket = getattr(storage, "bucket_name", "(unknown)")
            if code in (401, 403):
                raise CommandError(
                    f"The R2 credentials were refused ({code}) by bucket "
                    f"{bucket!r}. The copy was made and verified — only the "
                    "upload failed — so this is the API token, not the data. A "
                    "token that can reach a bucket answers 404 for a key that is "
                    "not there; 403 means this one does not carry this bucket. "
                    "Add the bucket to the existing token's scope in Cloudflare → "
                    "R2 → API → Manage API tokens, with Object Read & Write. "
                    "Prefer editing that token over issuing a new one: the media "
                    "bucket shares these credentials, so a new key has to be set "
                    "on the web service too. Nothing has been stored."
                ) from exc
            raise CommandError(
                f"Storing the backup in {bucket!r} failed: {exc}. The copy was "
                "made and verified, so this is the upload rather than the data. "
                "Nothing has been stored."
            ) from exc

    def _build_verified_copy(self, workdir: Path, name: str, target: str) -> Path:
        """Copy, verify, compress. Raises rather than returning a bad file."""
        sqlite_path = Path(connections.databases[target]["NAME"])

        self.stdout.write("Creating the schema…")
        call_command("migrate", database=target, verbosity=0, interactive=False)

        self.stdout.write(f"Copying {academy_db.ALIAS} → {target}…")
        call_command("academy_db_copy", source=academy_db.ALIAS,
                     target=target, apply=True, verbosity=0)

        # The gate. `academy_db_compare` answers in its exit status, which
        # `call_command` surfaces as SystemExit — so a mismatch stops the upload
        # rather than being printed above a success line nobody re-reads.
        self.stdout.write("Verifying every field of every row…")
        try:
            call_command("academy_db_compare", source=academy_db.ALIAS,
                         target=target, deep=True, verbosity=1)
        except SystemExit as exc:
            if exc.code:
                raise CommandError(
                    "The copy does not match academy_db, so nothing has been "
                    "stored. The comparison output above names the tables that "
                    "disagree. A backup that failed its own check and was kept "
                    "anyway is worse than no backup: it records a success."
                ) from exc

        gz_path = workdir / name
        with sqlite_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return gz_path

    # ------------------------------------------------------------- retention
    def _existing(self, storage) -> list[str]:
        try:
            _dirs, files = storage.listdir(PREFIX.rstrip("/"))
        except (FileNotFoundError, NotImplementedError):
            return []
        return sorted(f for f in files if f.endswith(".sqlite3.gz"))

    def _prune(self, storage, keep: int) -> None:
        names = self._existing(storage)
        doomed = names[:-keep] if len(names) > keep else []
        for n in doomed:
            storage.delete(f"{PREFIX}{n}")
        # Say what was kept and what went. A retention policy that reports
        # nothing is indistinguishable from one that is not running.
        if len(doomed) > 1:
            self.stdout.write(f"Kept the newest {keep}; deleted {len(doomed)} "
                              f"older ({doomed[0]} … {doomed[-1]}).")
        elif doomed:
            self.stdout.write(f"Kept the newest {keep}; deleted 1 older "
                              f"({doomed[0]}).")
        else:
            self.stdout.write(f"{len(names)} backup(s) in storage; retention is "
                              f"{keep}, so nothing was deleted.")

    def _report_retention(self, storage, keep: int) -> None:
        names = self._existing(storage)
        over = max(0, len(names) + 1 - keep)
        self.stdout.write(f"{len(names)} backup(s) already stored; keeping {keep} "
                          f"would delete {over}.")
