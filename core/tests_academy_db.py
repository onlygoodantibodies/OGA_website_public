"""What must not go wrong while academy_db moves from SQLite to PostgreSQL.

Four things are pinned here, chosen because each fails *silently*:

1. **Which models the copy walks** is derived from the router. A hand-written
   list that drifted would move some tables and not others, and the site would
   come up looking fine with a chunk of itself missing.
2. **Primary keys survive a copy.** A renumbered user is a live session pointing
   at the wrong person, and nothing downstream raises.
3. **The durability guard fires**, and — the half that is easy to get wrong —
   stays quiet on the rollback path, where SQLite on the attached Render disk is
   the correct answer rather than a mistake.
4. **A self-copy is refused.** The copy empties the target before it reads, so
   `--source X --target X` would delete every academy row and have nothing left
   to put back.

Not pinned: the PostgreSQL side. CI runs every alias on SQLite, so no test here
can vouch for a UUID crossing engines or a sequence being reset — that is what
the dress rehearsal against a scratch Postgres database is for, and what
`academy_db_compare` is written to answer.
"""
from __future__ import annotations

import io
import uuid

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from OGA_website import academy_db
from academy.models import Certificate as AcademyCertificate, Lesson
from core.models import APIConsumer

ALL = {'academy_db', 'academy_sqlite', 'pipeline_db'}


class AcademyModelSelectionTests(TestCase):
    """`academy_db.models()` asks the router, so it cannot drift from routing."""
    databases = {"academy_db", "pipeline_db"}

    def test_includes_every_kind_of_academy_model(self):
        labels = {m._meta.label for m in academy_db.models()}
        for expected in ('auth.User', 'auth.Group', 'auth.Permission',
                         'contenttypes.ContentType', 'sessions.Session',
                         'admin.LogEntry', 'academy.Lesson',
                         'academy.Certificate', 'credentials.Certificate',
                         'selector.SelectionRecord', 'core.APIConsumer',
                         'core.ApiUsageDay', 'account.EmailAddress'):
            self.assertIn(expected, labels)

    def test_excludes_every_pipeline_model(self):
        """The five legacy `core` models used to be excluded here too.

        They were deleted with the `default` database on 23 Aug 2026, so
        asserting their absence now asserts nothing — a model that does not
        exist cannot be in any list. What is still worth pinning is the
        boundary that remains: `academy_db.models()` must not claim anything
        the pipeline owns.
        """
        labels = {m._meta.label for m in academy_db.models()}
        self.assertFalse([label for label in labels
                          if label.startswith('pipeline.')])
        # And it must still claim the `core` models that survived.
        for kept in ('core.APIConsumer', 'core.ReviewedAntibody',
                     'core.ApiUsageDay'):
            self.assertIn(kept, labels)

    def test_includes_the_automatic_many_to_many_tables(self):
        """`User.user_permissions` is a table of its own and holds real rows.

        Walking only the declared models would copy every user and quietly drop
        what each of them is allowed to do.
        """
        tables = {m._meta.db_table for m in academy_db.models()}
        self.assertIn('auth_user_user_permissions', tables)
        self.assertIn('auth_user_groups', tables)
        self.assertIn('auth_group_permissions', tables)


class AcademyFallbackTests(TestCase):
    """With no ACADEMY_DATABASE_URL, the alias must land on the same file as before.

    This is the one line in the change that could break the live site on the
    deploy that carries it, and it would do so silently: `dj_database_url` parsing
    `sqlite:////var/data/db_academy.sqlite3` to anything but that exact path would
    point the alias at a *different* SQLite file, which SQLite would then create,
    empty. Every login would be gone and the deploy would look like it worked.

    Four slashes, not three — three is a relative path — and the count comes out
    of an f-string joining an absolute path onto the scheme, which is exactly the
    sort of thing nobody re-reads.
    """
    databases = {"academy_db", "pipeline_db"}

    def test_the_no_url_fallback_is_the_render_disk_path(self):
        import dj_database_url

        for path in ('/var/data/db_academy.sqlite3',
                     '/opt/render/project/src/db_academy.sqlite3'):
            config = dj_database_url.config(
                env='ACADEMY_DATABASE_URL_DELIBERATELY_UNSET',
                default=f'sqlite:///{path}')
            self.assertEqual(config['ENGINE'], 'django.db.backends.sqlite3')
            self.assertEqual(config['NAME'], path)

    def test_a_url_wins_over_the_fallback(self):
        import dj_database_url

        config = dj_database_url.parse(
            'postgres://user:pw@host:5432/oga_academy_db')
        self.assertEqual(config['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(config['NAME'], 'oga_academy_db')


class AcademyDurabilityGuardTests(TestCase):
    """`refusal()` speaks only when a deploy would eat the file."""
    databases = {"academy_db", "pipeline_db"}

    POSTGRES = {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'oga_academy_db'}
    ON_DISK = {'ENGINE': 'django.db.backends.sqlite3',
               'NAME': '/var/data/db_academy.sqlite3'}
    EPHEMERAL = {'ENGINE': 'django.db.backends.sqlite3',
                 'NAME': '/opt/render/project/src/db_academy.sqlite3'}

    def test_silent_for_postgresql(self):
        with override_settings(DEBUG=False, PERSISTENT_MEDIA_ROOTS=('/var/data',)):
            self.assertEqual(academy_db.refusal(config=self.POSTGRES), '')

    def test_silent_in_debug(self):
        with override_settings(DEBUG=True, PERSISTENT_MEDIA_ROOTS=('/var/data',)):
            self.assertEqual(academy_db.refusal(config=self.EPHEMERAL), '')

    def test_silent_on_the_persistent_disk(self):
        """The rollback path: disk attached, no URL set, and that is correct.

        Getting this wrong would be worse than not having the guard, because it
        would block the one-flag rollback the whole cutover depends on.
        """
        with override_settings(DEBUG=False, PERSISTENT_MEDIA_ROOTS=('/var/data',)):
            self.assertEqual(academy_db.refusal(config=self.ON_DISK), '')

    def test_refuses_sqlite_the_container_would_rebuild(self):
        with override_settings(DEBUG=False, PERSISTENT_MEDIA_ROOTS=('/var/data',)):
            why = academy_db.refusal(config=self.EPHEMERAL)
            self.assertIn('not on a persistent disk', why)
            self.assertIn('ACADEMY_DATABASE_URL', why)
            with self.assertRaises(ImproperlyConfigured):
                academy_db.enforce(config=self.EPHEMERAL)

    def test_wsgi_guards_before_django_boots(self):
        """Ordering is the whole point: a refusal after `get_wsgi_application()`
        would have already opened the invented database."""
        from pathlib import Path
        from django.conf import settings

        source = (Path(settings.BASE_DIR) / 'OGA_website' / 'wsgi.py').read_text()
        self.assertLess(source.index('academy_db.enforce()'),
                        source.index('get_wsgi_application()'))


class AcademyCopyTests(TestCase):
    """A real copy between two aliases that both carry the academy schema."""

    databases = ALL

    def setUp(self):
        self.out = io.StringIO()

    def _seed(self, alias):
        user = User.objects.using(alias).create(
            username='rosalind', email='r@example.org', password='x')
        other = User.objects.using(alias).create(
            username='dorothy', email='d@example.org', password='y')
        # A permission through-row: the thing a declared-models-only walk drops.
        permission = Permission.objects.using(alias).first()
        user.user_permissions.through.objects.using(alias).create(
            user_id=user.pk, permission_id=permission.pk)
        APIConsumer.objects.using(alias).create(
            name='Abcam', consumer_type='manufacturer',
            api_key=uuid.UUID('11111111-2222-3333-4444-555555555555'))
        Lesson.objects.using(alias).create(
            title='Controls', slug='controls', order=1)
        Session.objects.using(alias).create(
            session_key='abc123', session_data='x',
            expire_date='2030-01-01T00:00:00Z')
        return user, other

    def test_copy_preserves_every_primary_key_and_value(self):
        source, target = 'academy_sqlite', 'academy_db'
        user, other = self._seed(source)

        call_command('academy_db_copy', '--source', source, '--target', target,
                     '--apply', stdout=self.out)

        copied = User.objects.using(target).get(username='rosalind')
        self.assertEqual(copied.pk, user.pk)
        self.assertEqual(
            User.objects.using(target).get(username='dorothy').pk, other.pk)

        consumer = APIConsumer.objects.using(target).get(name='Abcam')
        self.assertEqual(str(consumer.api_key),
                         '11111111-2222-3333-4444-555555555555')

        self.assertEqual(
            Session.objects.using(target).get(pk='abc123').session_data, 'x')
        self.assertEqual(
            User.user_permissions.through.objects.using(target).count(), 1)

    def test_copy_moves_content_types_rather_than_keeping_the_targets(self):
        """`migrate` makes its own content types on the target with ids of their
        own. The admin log and every per-user permission point at the source's,
        so the source's are what has to survive."""
        source, target = 'academy_sqlite', 'academy_db'
        self._seed(source)
        expected = dict(ContentType.objects.using(source)
                        .values_list('pk', 'model'))

        call_command('academy_db_copy', '--source', source, '--target', target,
                     '--apply', stdout=self.out)

        self.assertEqual(
            dict(ContentType.objects.using(target).values_list('pk', 'model')),
            expected)

    def test_dry_run_writes_nothing(self):
        source, target = 'academy_sqlite', 'academy_db'
        self._seed(source)
        before = User.objects.using(target).count()

        call_command('academy_db_copy', '--source', source, '--target', target,
                     stdout=self.out)

        self.assertEqual(User.objects.using(target).count(), before)
        self.assertIn('Dry run', self.out.getvalue())

    def test_copy_does_not_restamp_auto_now_columns(self):
        """`bulk_create` calls `pre_save`, so `auto_now_add` would re-date the row.

        Ten columns across eight models are `auto_now`/`auto_now_add`, and they are
        not bookkeeping: `academy.Certificate.issued_at` is the day a learner earned
        their certificate. Without the freeze every one of them silently becomes the
        day the migration ran, and the certificate page goes on printing it as fact.

        Found by `academy_db_compare --deep` against a real PostgreSQL, which is the
        only reason it is pinned rather than shipped.
        """
        from datetime import datetime, timezone as tz

        source, target = 'academy_sqlite', 'academy_db'
        user = User.objects.using(source).create(username='rosalind', password='x')
        lesson = Lesson.objects.using(source).create(
            title='Controls', slug='controls', order=1)
        long_ago = datetime(2019, 3, 14, 9, 26, 53, tzinfo=tz.utc)
        certificate = AcademyCertificate.objects.using(source).create(
            user_id=user.pk, lesson_id=lesson.pk, score=88.0)
        # auto_now_add already stamped it "now"; put it back to the real date.
        AcademyCertificate.objects.using(source).filter(pk=certificate.pk).update(
            issued_at=long_ago)

        call_command('academy_db_copy', '--source', source, '--target', target,
                     '--apply', stdout=self.out)

        copied = AcademyCertificate.objects.using(target).get(pk=certificate.pk)
        self.assertEqual(copied.issued_at, long_ago)

    def test_the_timestamp_freeze_is_put_back_afterwards(self):
        """The flags are class-level and shared, so a copy that left them off would
        stop every later save stamping anything — in the same process."""
        source, target = 'academy_sqlite', 'academy_db'
        User.objects.using(source).create(username='rosalind', password='x')

        call_command('academy_db_copy', '--source', source, '--target', target,
                     '--apply', stdout=self.out)

        field = AcademyCertificate._meta.get_field('issued_at')
        self.assertTrue(field.auto_now_add)

    def test_refuses_to_copy_a_database_onto_itself(self):
        with self.assertRaises(CommandError) as caught:
            call_command('academy_db_copy', '--source', 'academy_db',
                         '--target', 'academy_db', '--apply', stdout=self.out)
        self.assertIn('destroy it', str(caught.exception))

    def test_refuses_an_alias_that_is_not_configured(self):
        with self.assertRaises(CommandError) as caught:
            call_command('academy_db_copy', '--source', 'nowhere',
                         '--target', 'academy_db', stdout=self.out)
        self.assertIn('ACADEMY_SQLITE_URL', str(caught.exception))


class AcademyCompareTests(TestCase):
    databases = ALL

    def setUp(self):
        self.out = io.StringIO()

    def test_a_faithful_copy_compares_clean(self):
        source, target = 'academy_sqlite', 'academy_db'
        User.objects.using(source).create(username='rosalind', password='x')
        call_command('academy_db_copy', '--source', source, '--target', target,
                     '--apply', stdout=io.StringIO())

        call_command('academy_db_compare', '--source', source,
                     '--target', target, stdout=self.out)

        self.assertIn('matches', self.out.getvalue())

    def test_a_missing_row_is_reported_and_exits_non_zero(self):
        """The case counts alone would catch. It is here because the exit status
        is what gates the cutover, so it has to be wrong when the data is."""
        source, target = 'academy_sqlite', 'academy_db'
        Session.objects.using(source).create(
            session_key='abc123', session_data='x',
            expire_date='2030-01-01T00:00:00Z')
        call_command('academy_db_copy', '--source', source, '--target', target,
                     '--apply', stdout=io.StringIO())
        # Session rather than User: `pipeline.Member` has a cross-database FK to
        # auth.User, so deleting one here sends Django's collector looking for
        # `pipeline_member` in academy_db, where it correctly does not exist.
        Session.objects.using(target).filter(pk='abc123').delete()

        with self.assertRaises(SystemExit):
            call_command('academy_db_compare', '--source', source,
                         '--target', target, stdout=self.out)
        self.assertIn('missing from', self.out.getvalue())

    def test_a_changed_uuid_is_reported_even_though_the_counts_agree(self):
        """The failure the row counts cannot see, on the column where it would
        cost a manufacturer their API key."""
        source, target = 'academy_sqlite', 'academy_db'
        APIConsumer.objects.using(source).create(
            name='Abcam', consumer_type='manufacturer')
        call_command('academy_db_copy', '--source', source, '--target', target,
                     '--apply', stdout=io.StringIO())
        APIConsumer.objects.using(target).update(api_key=uuid.uuid4())

        with self.assertRaises(SystemExit):
            call_command('academy_db_compare', '--source', source,
                         '--target', target, stdout=self.out)
        self.assertIn('api_key', self.out.getvalue())

    def test_refuses_to_compare_a_database_with_itself(self):
        with self.assertRaises(CommandError) as caught:
            call_command('academy_db_compare', '--source', 'academy_db',
                         '--target', 'academy_db', stdout=self.out)
        self.assertIn('proves nothing', str(caught.exception))


class AcademyUrlParseTests(TestCase):
    """A connection string that was not read as one, refused by name.

    Django's own message for this names a 150-character *database name* — a thing
    nobody chose and nobody can shorten — and never mentions the variable or the
    slash that caused it. It cost a cutover attempt on 23 Aug 2026.
    """
    databases = {"academy_db", "pipeline_db"}

    GOOD = {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'oga_academy_db',
            'HOST': 'dpg-xxxxxxxxxxxxxxxxxxxx-a'}
    # What `postgresql:/user:pw@host/db` actually parses to: everything after the
    # scheme lands in NAME and HOST is left empty.
    MANGLED = {'ENGINE': 'django.db.backends.postgresql', 'HOST': '',
               'NAME': 'oga_academy_db_user:pw@dpg-xxxxxxxxxxxxxxxxxxxx-a'
                       '.frankfurt-postgres.render.com/oga_academy_db'}
    # The same slip on a short URL: under the length limit, so only the empty
    # host gives it away — and it would otherwise connect to a local socket that
    # does not exist on any Render service.
    SHORT_MANGLED = {'ENGINE': 'django.db.backends.postgresql',
                     'HOST': '', 'NAME': 'u:p@h/oga_academy_db'}

    def test_a_good_url_is_silent(self):
        self.assertEqual(academy_db.config_refusal(config=self.GOOD), '')

    def test_sqlite_is_not_its_business(self):
        self.assertEqual(academy_db.config_refusal(
            config={'ENGINE': 'django.db.backends.sqlite3',
                    'NAME': '/var/data/db_academy.sqlite3'}), '')

    def test_the_whole_url_in_the_name_is_refused_by_name(self):
        why = academy_db.config_refusal(config=self.MANGLED)
        self.assertIn('ACADEMY_DATABASE_URL', why)
        self.assertIn("'://'", why)
        self.assertIn('Internal Database URL', why)

    def test_an_empty_host_is_caught_even_under_the_length_limit(self):
        why = academy_db.config_refusal(config=self.SHORT_MANGLED)
        self.assertIn('no host', why)

    def test_the_real_dj_database_url_failure_is_what_is_being_described(self):
        """Not a hand-built dict: the actual parse of the actual mistake."""
        import dj_database_url

        config = dj_database_url.parse(
            'postgresql:/oga_academy_db_user:secret@'
            'dpg-xxxxxxxxxxxxxxxxxxxx-a.frankfurt-postgres.render.com/oga_academy_db')
        self.assertGreater(len(config['NAME']), academy_db.MAX_PG_NAME)
        self.assertFalse(config['HOST'])
        self.assertIn('ACADEMY_DATABASE_URL', academy_db.config_refusal(config=config))
