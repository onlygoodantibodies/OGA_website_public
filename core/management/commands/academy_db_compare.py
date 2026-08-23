"""Prove that two academy databases hold the same rows.

    python manage.py academy_db_compare                    # source vs live
    python manage.py academy_db_compare --deep             # every field
    python manage.py academy_db_compare --source academy_sqlite --target academy_db

This is the step the SQLite → PostgreSQL cutover turns on: the copy runs inside
the maintenance window, this runs before the environment variable is set, and if
it disagrees nothing has changed yet. **Exit status is the answer** — 0 identical,
1 not — so it can gate the flip rather than be read hopefully.

Three layers, cheapest first, because each catches something the one above it
cannot:

**Row counts** catch a table that did not copy at all. They are also the layer
that lies most readily: two tables can hold the same number of different rows.

**Primary keys, compared as sets**, catch that. They matter more here than the
counts do, because preserving every pk is the property the whole move rests on:
``django_session.session_data`` stores the signed-in user's id as a plain string
inside the encoded blob, so a renumbered user is a live session silently pointing
at the wrong person. Nothing downstream would notice. This is why the copy does
not go through ``dumpdata --natural-primary``.

**UUID and JSON values** catch the two column types whose storage genuinely
differs between the engines — SQLite keeps a UUID as 32 hex characters and JSON
as text, PostgreSQL has native types for both. Five UUID columns carry things
whose failure is silent to us and loud to somebody else: ``APIConsumer.api_key``
is a live API key, and ``Certificate.verification_code``,
``credentials.Certificate.verification_code``, ``PendingClaim.token`` and
``SelectionRecord.public_code`` are all in URLs people follow.

One thing this deliberately does not check: **JSON key order**. ``jsonb`` does
not preserve it, dict equality does not care, and no reader in this app depends
on it — but if one ever does, this will not be the thing that tells you.

``--deep`` compares every concrete field on every row instead. It is slower and
noisier and worth running once in the dress rehearsal.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from OGA_website import academy_db

#: Never print more than this many differing rows per model. A wall of ids is
#: not more informative than a handful and a count, and the count is the part
#: that says how bad it is.
MAX_SHOWN = 10


class Command(BaseCommand):
    help = "Compare two academy databases row by row and report any difference."

    def add_arguments(self, parser):
        parser.add_argument('--source', default=academy_db.SOURCE_ALIAS,
                            help='Alias to read the expected rows from '
                                 f'(default: {academy_db.SOURCE_ALIAS}).')
        parser.add_argument('--target', default=academy_db.ALIAS,
                            help='Alias to check against '
                                 f'(default: {academy_db.ALIAS}).')
        parser.add_argument('--deep', action='store_true',
                            help='Compare every concrete field, not just keys, '
                                 'UUIDs and JSON.')

    def handle(self, *args, **options):
        source, target = options['source'], options['target']
        for alias in (source, target):
            _refuse_bad_url(alias)
            _require_alias(alias)
        if source == target:
            raise CommandError(
                f"--source and --target are both {source!r}. Comparing a "
                "database with itself proves nothing; name the two you mean.")

        models = academy_db.models()
        if not models:
            raise CommandError(
                "The router puts no models in academy_db, which cannot be right "
                "— check DATABASE_ROUTERS and OGA_website/db_router.py.")

        self.stdout.write(f"{len(models)} models routed to "
                          f"{academy_db.ALIAS}: {source} -> {target}\n")

        problems = []
        total_rows = 0
        for model in models:
            label = model._meta.label
            try:
                found = _compare_model(model, source, target,
                                       deep=options['deep'])
            except Exception as exc:                      # noqa: BLE001
                # A table missing on one side is a real answer, not a crash: it
                # is what an unmigrated target looks like, and saying which model
                # and which database beats a traceback naming neither.
                problems.append(f"{label}: could not be read ({exc})")
                self.stdout.write(self.style.ERROR(f"  {label}: {exc}"))
                continue

            total_rows += found.source_count
            if found.differences:
                problems.extend(f"{label}: {d}" for d in found.differences)
                self.stdout.write(self.style.ERROR(
                    f"  {label}: {found.source_count} -> {found.target_count}"))
                for line in found.differences:
                    self.stdout.write(self.style.ERROR(f"      {line}"))
            else:
                self.stdout.write(
                    f"  {label}: {found.source_count} rows, identical")

        for message in _sequence_problems(target, models):
            problems.append(message)
            self.stdout.write(self.style.ERROR(f"  {message}"))

        self.stdout.write("")
        if problems:
            self.stdout.write(self.style.ERROR(
                f"{len(problems)} problem(s). {target} does NOT match {source} "
                "— do not switch anything over."))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(
            f"{total_rows} rows across {len(models)} models. "
            f"{target} matches {source} exactly."))


class _Result:
    def __init__(self, source_count, target_count, differences):
        self.source_count = source_count
        self.target_count = target_count
        self.differences = differences


def _compare_model(model, source, target, *, deep=False):
    """Counts, then keys, then the values whose storage differs by engine."""
    differences = []

    source_keys = set(_keys(model, source))
    target_keys = set(_keys(model, target))
    source_count, target_count = len(source_keys), len(target_keys)

    if source_count != target_count:
        differences.append(
            f"row count {source_count} on {source}, {target_count} on {target}")

    missing = source_keys - target_keys
    extra = target_keys - source_keys
    if missing:
        differences.append(
            f"{len(missing)} row(s) missing from {target}: {_sample(missing)}")
    if extra:
        differences.append(
            f"{len(extra)} row(s) on {target} that {source} does not have: "
            f"{_sample(extra)}")

    # Comparing values only makes sense for rows both sides actually have.
    shared = source_keys & target_keys
    if shared:
        fields = _concrete_fields(model) if deep else _fragile_fields(model)
        for field in fields:
            differences.extend(
                _compare_field(model, field, shared, source, target))

    return _Result(source_count, target_count, differences)


def _compare_field(model, field, shared, source, target):
    left = _values(model, field, source)
    right = _values(model, field, target)
    changed = [pk for pk in shared if left.get(pk) != right.get(pk)]
    if not changed:
        return []
    example = changed[0]
    return [f"{field.name}: {len(changed)} row(s) differ, e.g. pk={example} "
            f"{left.get(example)!r} -> {right.get(example)!r}"]


def _keys(model, alias):
    return model._base_manager.using(alias).values_list('pk', flat=True)


def _values(model, field, alias):
    return dict(model._base_manager.using(alias)
                .values_list('pk', field.attname))


def _concrete_fields(model):
    return [f for f in model._meta.concrete_fields if not f.primary_key]


def _fragile_fields(model):
    """UUID and JSON columns — the two whose on-disk form differs by engine."""
    return [f for f in model._meta.concrete_fields
            if f.get_internal_type() in ('UUIDField', 'JSONField')]


def _sample(keys):
    ordered = sorted(keys, key=str)[:MAX_SHOWN]
    suffix = ', ...' if len(keys) > MAX_SHOWN else ''
    return ', '.join(str(k) for k in ordered) + suffix


def _require_alias(alias):
    if alias not in connections:
        raise CommandError(
            f"No database alias called {alias!r}. The migration's source alias "
            "only exists when ACADEMY_SQLITE_URL is set — point it at the "
            "SQLite file, e.g. "
            "ACADEMY_SQLITE_URL=sqlite:////var/data/db_academy.sqlite3")


def _sequence_problems(alias, models):
    """On PostgreSQL, would the next insert collide with a copied row?

    Copying rows with their primary keys does not move the sequence behind the
    column, so a target that looks perfect can still refuse the very next
    sign-up with a duplicate key error. That failure is loud, which is the only
    reason it is a footnote rather than the headline — but finding it here costs
    nothing and finding it during press week costs a sign-up.
    """
    connection = connections[alias]
    if connection.vendor != 'postgresql':
        return []

    problems = []
    with connection.cursor() as cursor:
        for model in models:
            pk = model._meta.pk
            if pk.get_internal_type() not in ('AutoField', 'BigAutoField'):
                continue
            table, column = model._meta.db_table, pk.column
            cursor.execute("SELECT pg_get_serial_sequence(%s, %s)",
                           [table, column])
            row = cursor.fetchone()
            sequence = row[0] if row else None
            if not sequence:
                continue
            cursor.execute(f'SELECT last_value, is_called FROM {sequence}')
            last_value, is_called = cursor.fetchone()
            next_value = last_value + 1 if is_called else last_value
            cursor.execute(f'SELECT MAX({column}) FROM {table}')
            highest = cursor.fetchone()[0] or 0
            if next_value <= highest:
                problems.append(
                    f"{model._meta.label}: sequence {sequence} would next issue "
                    f"{next_value}, but {table} already holds {highest} — the "
                    "next insert collides.")
    return problems


def _refuse_bad_url(alias):
    """Say which variable is wrong before Django says the database name is long."""
    why = academy_db.config_refusal(alias)
    if why:
        raise CommandError(why)
