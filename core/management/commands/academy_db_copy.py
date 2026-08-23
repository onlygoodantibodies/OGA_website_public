"""Copy every academy row from one database to another, keys and all.

    python manage.py academy_db_copy                      # dry run, changes nothing
    python manage.py academy_db_copy --apply
    python manage.py academy_db_copy --source academy_sqlite --target academy_db --apply

Dry run by default, like every other command in this project that writes.

**It moves rows through the ORM, not through JSON.** ``dumpdata | loaddata`` is
the obvious route and it has one trap that matters more than everything it gets
right: ``--natural-primary`` renumbers users, and ``django_session.session_data``
carries the signed-in user's id as a plain string inside the encoded blob. Every
live session would then point at the wrong person, or at nobody, with no error
anywhere. Reading and writing model instances keeps every primary key by
construction, and lets each backend do its own type conversion — a UUID goes out
of SQLite as 32 hex characters and into PostgreSQL as a native ``uuid`` without a
text representation in between to get wrong.

**The whole copy is one transaction, so a failure leaves the target untouched.**
Foreign keys are deferred inside it (Django creates them ``DEFERRABLE INITIALLY
DEFERRED`` on PostgreSQL, which is the same mechanism ``loaddata`` relies on), so
the order models are copied in does not matter, and ``check_constraints()`` at
the end is what says the graph came over whole.

**It is re-runnable.** The target is emptied of academy rows first, so a run that
went wrong is fixed by running it again rather than by rebuilding the database.
That is deliberate: the real run happens inside a maintenance window, and a
window is a bad place to be inventing recovery steps.

**It empties the content types and permissions that ``migrate`` just created.**
Those rows are made by a ``post_migrate`` signal on the fresh target, with ids
that have nothing to do with the source's. Left in place they would collide; and
copying the source's own rows is what keeps ``django_admin_log`` and every
per-user permission pointing where they pointed before.

What it does **not** do: create the target, migrate it, or decide when to switch
over. Migrate the target first, run this, then run ``academy_db_compare`` and
read its exit status before touching ``ACADEMY_DATABASE_URL``.
"""
from __future__ import annotations

import contextlib

from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, transaction

from OGA_website import academy_db

#: Rows per INSERT. Small enough that SQLite's variable limit is never the thing
#: that fails a copy of a table with many columns.
BATCH = 200


class Command(BaseCommand):
    help = ("Copy every academy_db row from one alias to another, preserving "
            "primary keys. Dry run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument('--source', default=academy_db.SOURCE_ALIAS,
                            help=f'Read from (default: {academy_db.SOURCE_ALIAS}).')
        parser.add_argument('--target', default=academy_db.ALIAS,
                            help=f'Write to (default: {academy_db.ALIAS}).')
        parser.add_argument('--apply', action='store_true',
                            help='Actually write. Without it nothing is changed.')

    def handle(self, *args, **options):
        source, target = options['source'], options['target']
        apply_it = options['apply']

        for alias in (source, target):
            _refuse_bad_url(alias)
            if alias not in connections:
                raise CommandError(
                    f"No database alias called {alias!r}. The migration's source "
                    "alias only exists when ACADEMY_SQLITE_URL is set — point it "
                    "at the SQLite file, e.g. "
                    "ACADEMY_SQLITE_URL=sqlite:////var/data/db_academy.sqlite3")

        if source == target:
            # Worth refusing by name rather than trusting nobody will: the copy
            # empties the target before it reads, so a self-copy would delete
            # every academy row and then have nothing left to put back.
            raise CommandError(
                f"--source and --target are both {source!r}. This empties the "
                "target before writing, so copying a database onto itself would "
                "destroy it. Name the two databases you mean.")

        models = academy_db.models()
        if not models:
            raise CommandError(
                "The router puts no models in academy_db, which cannot be right "
                "— check DATABASE_ROUTERS and OGA_website/db_router.py.")

        counts = {}
        for model in models:
            try:
                counts[model] = model._base_manager.using(source).count()
            except Exception as exc:                       # noqa: BLE001
                raise CommandError(
                    f"Could not read {model._meta.label} from {source}: {exc}")

        total = sum(counts.values())
        self.stdout.write(f"{source} -> {target}: {total} rows across "
                          f"{len(models)} models")
        for model, count in counts.items():
            if count:
                self.stdout.write(f"  {model._meta.label}: {count}")

        existing = _target_row_count(target, models)
        if existing:
            self.stdout.write(self.style.WARNING(
                f"\n{target} already holds {existing} academy row(s). They will "
                "be deleted first — including the content types and permissions "
                "`migrate` created, which is intended."))

        if not apply_it:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing written. Re-run with --apply."))
            return

        written = self._copy(models, source, target)

        self.stdout.write(self.style.SUCCESS(
            f"\nCopied {written} rows into {target}."))
        self.stdout.write(
            "Now run: manage.py academy_db_compare "
            f"--source {source} --target {target}\n"
            "and read its exit status before setting ACADEMY_DATABASE_URL.")

    def _copy(self, models, source, target):
        connection = connections[target]
        written = 0

        with transaction.atomic(using=target):
            with connection.constraint_checks_disabled():
                with connection.cursor() as cursor:
                    for model in reversed(models):
                        cursor.execute(
                            f'DELETE FROM {connection.ops.quote_name(model._meta.db_table)}')

                with _frozen_timestamps(models):
                    for model in models:
                        written += _copy_model(model, source, target)

            # Inside the transaction, so a foreign key that did not come over
            # rolls the whole copy back rather than leaving a half-populated
            # database that looks finished.
            connection.check_constraints()

            statements = connection.ops.sequence_reset_sql(no_style(), models)
            if statements:
                with connection.cursor() as cursor:
                    for statement in statements:
                        cursor.execute(statement)

        return written


@contextlib.contextmanager
def _frozen_timestamps(models):
    """Stop ``auto_now`` / ``auto_now_add`` rewriting the copy's own timestamps.

    Both are implemented in ``DateTimeField.pre_save``, and ``bulk_create`` calls
    ``pre_save`` exactly as ``save()`` does — so without this the copy stamps every
    one of them with the moment it ran. On this database that is ten columns
    across eight models, and the harm is not cosmetic: ``academy.Certificate.
    issued_at`` is the date a learner earned their certificate, and
    ``LessonProgress.completed_at`` is when they finished the lesson. Every one of
    them would silently become migration day, on a screen that would go on
    displaying it as fact.

    Found by running ``academy_db_compare --deep`` against a real PostgreSQL in the
    dress rehearsal; the ordinary compare cannot see it, because it only reads the
    UUID and JSON columns.

    These flags live on the field instances, which are class-level and shared, so
    the restore is in a ``finally``. A management command is single-threaded, which
    is the only reason mutating them is safe at all — do not reach for this from a
    request.
    """
    touched = []
    for model in models:
        for field in model._meta.concrete_fields:
            if getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
                touched.append((field, field.auto_now, field.auto_now_add))
                field.auto_now = False
                field.auto_now_add = False
    try:
        yield
    finally:
        for field, auto_now, auto_now_add in touched:
            field.auto_now = auto_now
            field.auto_now_add = auto_now_add


def _copy_model(model, source, target):
    rows = list(model._base_manager.using(source).all())
    if not rows:
        return 0

    for row in rows:
        # These instances were loaded from the source, so Django believes they
        # already exist there. Saying otherwise is what makes the write an
        # INSERT that keeps the primary key rather than an UPDATE of nothing.
        row._state.db = target
        row._state.adding = True

    manager = model._base_manager.using(target)
    if model._meta.parents:
        # Multi-table inheritance: bulk_create refuses it, and a child row has to
        # be written after its parent. None of these models use it today; falling
        # back rather than failing means adding one later is slow, not broken.
        for row in rows:
            row.save(using=target, force_insert=True)
    else:
        manager.bulk_create(rows, batch_size=BATCH)
    return len(rows)


def _target_row_count(target, models):
    total = 0
    for model in models:
        try:
            total += model._base_manager.using(target).count()
        except Exception:                                  # noqa: BLE001
            # An unmigrated target is a legitimate thing to point this at; the
            # copy itself will say so far more clearly than a count would.
            return total
    return total


def _refuse_bad_url(alias):
    """Say which variable is wrong before Django says the database name is long."""
    why = academy_db.config_refusal(alias)
    if why:
        raise CommandError(why)
