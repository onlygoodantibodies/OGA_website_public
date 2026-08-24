"""What `academy_backup` must never get wrong.

Three things are pinned here, and they are the three whose failure is *silent*:
the backup going somewhere public, a backup that failed its own verification
being stored anyway, and retention deleting the wrong end of the list. Each of
those reports success while being wrong, which is the shape this project keeps
paying for.

The happy path is pinned too: the command runs and a file lands.

What is deliberately *not* here is an assertion that the stored file contains
rows. It needs committed data, so it needs a `TransactionTestCase`, and one of
those truncates rather than rolling back — which broke four `tests_browser_board`
tests hundreds of tests later, the worst possible distance between cause and
symptom. Narrowing its `databases` and adding `serialized_rollback` did not fix
it. With CI off, a suite that can be trusted is worth more than one commissioning
assertion, so that check was made by running the command for real against a
SQLite academy_db and reading `auth_user` back out of the gzip with plain
`sqlite3` — see the commit message. Re-run it that way if the format changes.
"""
from __future__ import annotations

import gzip
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings as dj_settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import SimpleTestCase, TestCase, override_settings

from core.management.commands import academy_backup


#: The academy test database is a SQLite file under BASE_DIR, so
#: `academy_db.refusal()` correctly calls it non-durable and the command
#: correctly refuses to back it up. Naming BASE_DIR persistent is how a test says
#: "assume this is the real database" — better than flipping DEBUG, which would
#: also switch off the production behaviour these tests exist to exercise.
def _durable(**extra):
    return override_settings(
        USE_R2=False, PERSISTENT_MEDIA_ROOTS=(str(dj_settings.BASE_DIR),), **extra)


class _Bucket:
    """Stands in for an S3Storage, which is all `destination_refusal` reads."""

    def __init__(self, custom_domain=None):
        self.custom_domain = custom_domain


class BackupDestinationTests(SimpleTestCase):
    """The refusal that keeps 84 password hashes off a public URL."""

    def test_refuses_when_the_private_bucket_is_unset(self):
        # attachment_storage() falls back to the *media* bucket here, which is
        # served by a public Cloudflare domain. Right for a gel scan, ruinous
        # for this file.
        with override_settings(USE_R2=True, R2_ATTACHMENTS_BUCKET=''):
            why = academy_backup.destination_refusal(_Bucket())
        self.assertIn('R2_ATTACHMENTS_BUCKET', why)
        self.assertIn('Nothing has been written', why)

    def test_refuses_a_bucket_that_has_a_public_domain(self):
        # The setting being present is not the same as it having taken effect.
        with override_settings(USE_R2=True, R2_ATTACHMENTS_BUCKET='oga-private'):
            why = academy_backup.destination_refusal(
                _Bucket(custom_domain='media.onlygoodantibodies.co.uk'))
        self.assertIn('public custom domain', why)

    def test_silent_for_a_private_bucket(self):
        with override_settings(USE_R2=True, R2_ATTACHMENTS_BUCKET='oga-private'):
            self.assertEqual(
                academy_backup.destination_refusal(_Bucket()), '')

    def test_silent_when_r2_is_off(self):
        # A local run writes under MEDIA_ROOT, which is the point of local.
        with override_settings(USE_R2=False):
            self.assertEqual(academy_backup.destination_refusal(None), '')


class BackupArgumentTests(TestCase):
    databases = {'academy_db', 'academy_sqlite', 'pipeline_db'}

    def test_refuses_to_back_up_into_the_database_being_backed_up(self):
        # academy_db_copy empties its target before it writes.
        with self.assertRaises(CommandError) as caught:
            call_command('academy_backup', '--target', 'academy_db')
        self.assertIn('destroy it', str(caught.exception))

    def test_refuses_an_alias_that_does_not_exist(self):
        with _durable(), self.assertRaises(CommandError) as caught:
            call_command('academy_backup', '--target', 'no_such_alias')
        self.assertIn('no_such_alias', str(caught.exception))

    def test_refuses_to_keep_nothing(self):
        with self.assertRaises(CommandError) as caught:
            call_command('academy_backup', '--keep', '0')
        self.assertIn('--keep', str(caught.exception))


class BackupRunTests(TestCase):
    """The command driven for real, against two real databases."""

    databases = {'academy_db', 'academy_sqlite', 'pipeline_db'}

    def setUp(self):
        self.media = tempfile.mkdtemp(prefix='backup-media-')

    def _run(self, *args):
        from io import StringIO
        out = StringIO()
        with _durable(MEDIA_ROOT=self.media):
            call_command('academy_backup', '--target', 'academy_sqlite',
                         *args, stdout=out)
        return out.getvalue()

    def _stored(self):
        root = Path(self.media) / academy_backup.PREFIX.rstrip('/')
        return sorted(p.name for p in root.glob('*.sqlite3.gz')) if root.is_dir() else []

    def test_it_writes_one_backup(self):
        self._run()
        self.assertEqual(len(self._stored()), 1)

    def test_a_failed_verification_stores_nothing(self):
        """The gate. A backup that failed its own check must not be kept."""
        real = call_command

        def fail_the_compare(name, *a, **kw):
            if name == 'academy_db_compare':
                raise SystemExit(1)
            return real(name, *a, **kw)

        with mock.patch.object(academy_backup, 'call_command',
                               side_effect=fail_the_compare):
            with self.assertRaises(CommandError) as caught:
                self._run()

        self.assertIn('does not match', str(caught.exception))
        self.assertEqual(self._stored(), [],
                         "a backup that failed verification was stored anyway")

    def test_dry_run_writes_nothing(self):
        out = self._run('--dry-run')
        self.assertEqual(self._stored(), [])
        self.assertIn('Would copy', out)

    def test_retention_deletes_the_oldest_and_keeps_the_newest(self):
        root = Path(self.media) / academy_backup.PREFIX.rstrip('/')
        root.mkdir(parents=True, exist_ok=True)
        # Keys sort lexicographically into date order, which is what retention
        # relies on. Oldest first.
        for stamp in ('2026-08-01T000000Z', '2026-08-02T000000Z',
                      '2026-08-03T000000Z'):
            (root / f'academy-{stamp}.sqlite3.gz').write_bytes(b'old')

        self._run('--keep', '2')

        kept = self._stored()
        self.assertEqual(len(kept), 2, f"expected 2 kept, got {kept}")
        self.assertNotIn('academy-2026-08-01T000000Z.sqlite3.gz', kept,
                         "retention deleted the newest instead of the oldest")
        # The one just taken is the newest by name, so it must survive.
        self.assertNotIn('academy-2026-08-02T000000Z.sqlite3.gz', kept)


class BackupTargetTests(TestCase):
    """Where the copy goes is named in the environment, not decided here."""

    databases = {'academy_db', 'academy_sqlite', 'pipeline_db'}

    def test_refuses_by_name_when_no_scratch_database_is_configured(self):
        # The cron job's own failure mode: ACADEMY_SQLITE_URL not set, so the
        # alias settings would have registered is simply absent. The refusal has
        # to name the variable, because nothing else on any screen would.
        with mock.patch.dict(connections.databases):
            del connections.databases['academy_sqlite']
            with _durable(), self.assertRaises(CommandError) as caught:
                call_command('academy_backup')
        why = str(caught.exception)
        self.assertIn('ACADEMY_SQLITE_URL', why)
        self.assertIn('Nothing has been written', why)

    def test_defaults_to_the_academy_sqlite_alias(self):
        # No --target: it must pick up the alias settings registered, not
        # invent one. Proven by it working rather than refusing.
        media = tempfile.mkdtemp(prefix='backup-default-')
        with _durable(MEDIA_ROOT=media):
            call_command('academy_backup', verbosity=0)
        blobs = list((Path(media) / academy_backup.PREFIX.rstrip('/'))
                     .glob('*.sqlite3.gz'))
        self.assertEqual(len(blobs), 1)


class BackupSourceGuardTests(TestCase):
    """Refuse to back up a database the container invented."""

    databases = {'academy_db', 'academy_sqlite', 'pipeline_db'}

    def test_refuses_when_academy_db_is_not_the_durable_one(self):
        # Exactly the cron job's worst case: ACADEMY_DATABASE_URL missing, so
        # academy_db is an ephemeral SQLite file. A copy of it would succeed and
        # verify clean, and the backup would be empty.
        media = tempfile.mkdtemp(prefix='backup-guard-')
        with override_settings(DEBUG=False, USE_R2=False, MEDIA_ROOT=media,
                               PERSISTENT_MEDIA_ROOTS=('/var/data',)):
            with self.assertRaises(CommandError) as caught:
                call_command('academy_backup', verbosity=0)
        self.assertIn('Refusing to back up', str(caught.exception))
        self.assertEqual(
            list((Path(media) / academy_backup.PREFIX.rstrip('/')).glob('*'))
            if (Path(media) / academy_backup.PREFIX.rstrip('/')).is_dir() else [],
            [])


class BackupDebugNoticeTests(TestCase):
    """DEBUG defaults to True, so its effect on the guard must be visible."""

    databases = {'academy_db', 'academy_sqlite', 'pipeline_db'}

    def test_it_says_when_the_durability_check_was_skipped(self):
        from io import StringIO
        out = StringIO()
        media = tempfile.mkdtemp(prefix='backup-debug-')
        with override_settings(DEBUG=True, USE_R2=False, MEDIA_ROOT=media):
            call_command('academy_backup', '--dry-run', stdout=out)
        self.assertIn('DEBUG is on', out.getvalue())
        self.assertIn('DEBUG=False', out.getvalue())


class BackupUploadFailureTests(TestCase):
    """A storage error is a sentence, not a botocore traceback.

    Reproduces the real 23 Aug failure: the copy and the verification both
    succeeded against live data and the upload was refused 403, because the R2
    token did not carry the new bucket.
    """

    databases = {'academy_db', 'academy_sqlite', 'pipeline_db'}

    def _refused(self, exc):
        media = tempfile.mkdtemp(prefix='backup-upload-')
        broken = mock.Mock()
        broken.bucket_name = 'oga-private'
        broken.save.side_effect = exc
        broken.listdir.return_value = ([], [])
        with mock.patch('pipeline.storages.attachment_storage',
                        return_value=broken):
            with _durable(MEDIA_ROOT=media):
                with self.assertRaises(CommandError) as caught:
                    call_command('academy_backup', '--target', 'academy_sqlite',
                                 verbosity=0)
        return str(caught.exception)

    def test_a_403_names_the_token_and_the_bucket(self):
        err = Exception("Forbidden")
        err.response = {'ResponseMetadata': {'HTTPStatusCode': 403}}
        why = self._refused(err)
        self.assertIn('oga-private', why)
        self.assertIn('token', why)
        # The distinction that makes it diagnosable rather than just loud.
        self.assertIn('404', why)
        self.assertIn('Nothing has been stored', why)

    def test_any_other_storage_error_still_says_it_was_the_upload(self):
        why = self._refused(Exception("connection reset"))
        self.assertIn('connection reset', why)
        self.assertIn('upload rather than the data', why)
