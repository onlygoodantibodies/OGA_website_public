"""Build the one-file download of every published figure.

    python manage.py build_bulk_archive              # build if the dataset moved
    python manage.py build_bulk_archive --force      # rebuild even if unchanged
    python manage.py build_bulk_archive --dry-run    # say what it would do
    python manage.py build_bulk_archive --keep-old   # do not prune older builds

Run it from a **Render Cron Job**, daily, for the reason
``pipeline/management/commands/dataset_snapshot.py`` gives at more length: a
cron job has no disk, but it does have the bucket, and the bucket is the one
thing the cron job and the web service can both see. Doing this behind a request
would sit inside gunicorn's worker timeout while moving a few hundred megabytes.

**Idempotent by construction.** The object key carries the dataset fingerprint
(``core/bulk_archive.py``), so a run against an unchanged dataset finds its own
output already there and stops. Running it hourly costs one ``exists()`` call
per hour and nothing else.

It writes only to the ``bulk/`` prefix, and the only thing it deletes is an
archive it built earlier. It reaches no scientific record, which is why it is
not dry-run-by-default the way the ``production-data`` commands are — there is
nothing here a mistake could cost.
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from core import api_manifest, bulk_archive


def _mb(n):
    return f'{n / 1_048_576:.1f} MB'


class Command(BaseCommand):
    help = 'Build the downloadable archive of every published figure'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Rebuild even if an archive for this dataset version exists.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be built and written, and write nothing.')
        parser.add_argument(
            '--keep-old', action='store_true',
            help='Leave archives for superseded dataset versions in place. '
                 'Nothing then bounds the prefix — for a one-off inspection, '
                 'not for the cron job.')
        parser.add_argument(
            '--keep', type=int, default=bulk_archive.KEEP_ARCHIVES,
            help=f'How many archives to keep, current one included '
                 f'(default {bulk_archive.KEEP_ARCHIVES}).')

    def _say(self, message, style=None):
        """Write a line and push it out immediately.

        Python block-buffers stdout when it is a pipe rather than a terminal,
        and a Render cron job's stdout is a pipe. So on the second real run the
        system-check warnings appeared instantly — they go to stderr, which is
        not block-buffered — and every line this command wrote sat in a 4 KB
        buffer while it read four thousand objects over the network. The job was
        working; from outside it was six minutes of total silence after a wall
        of warnings, which reads as hung, and the natural response to a hung job
        is to cancel it.
        """
        self.stdout.write(style(message) if style else message)
        self.stdout.flush()

    def _progress(self, done, total, uploading=False, part=None, sent=None):
        if part is not None:
            # The reads ticked and the upload did not, so a stall gave no way to
            # tell whether it was stuck fetching a figure or stuck pushing a
            # part. Two different faults, one symptom.
            self._say(f'    part {part} sent ({_mb(sent)} so far)')
        elif uploading:
            self._say(f'  read {total}/{total} — finishing the upload...')
        else:
            self._say(f'  {done}/{total}...')

    def handle(self, *args, **options):
        started = time.monotonic()

        # Before any work: a run that cannot keep what it writes must not do the
        # writing. Checked here rather than after building because the failure
        # is in the environment, and reading 4,321 objects to discover it wastes
        # minutes and produces a confusing wall of per-file errors.
        refusal = bulk_archive.storage_refusal()
        if refusal:
            raise CommandError(f'Not building the archive.\n{refusal}')

        self._say('Reading the dataset...')
        inputs = api_manifest.archive_inputs()
        version = inputs['version']
        rows = inputs['rows']
        key = bulk_archive.key_for(version)

        if not rows:
            # Almost always the local SQLite fallback rather than live
            # PostgreSQL — the same trap build_extension_index calls out.
            self.stderr.write(self.style.WARNING(
                'No published figures found, so there is nothing to archive. Is '
                'PIPELINE_DATABASE_URL pointing at the live pipeline database?'))
            return

        self._say(f'  dataset version : {version}')
        self._say(f'  figures         : {len(rows)}')
        self._say(f'  archive key     : {key}')

        from django.core.files.storage import default_storage
        if default_storage.exists(key) and not options['force']:
            self._say(self.style.SUCCESS(
                'Already built for this dataset version — nothing to do.'))
            self._say(f'  {default_storage.url(key)}')
            self._prune(version, options)
            return

        if options['dry_run']:
            self._say(self.style.WARNING(
                f'Dry run: would build {len(rows)} figures into {key}.'))
            self._prune(version, options, dry_run=True)
            return

        self._say('Building...')
        try:
            _key, written, missing = bulk_archive.build(
                rows, version, inputs['manifest_csv'], inputs['readme'],
                progress=self._progress)
        except bulk_archive.IncompleteArchive as exc:
            # CommandError exits non-zero, which is the whole point: the first
            # real run of this job read none of its 4,321 figures, wrote a zip
            # containing only a manifest, and Render printed "Cron job run
            # finished successfully". A job that reports success having done
            # nothing is worse than one that crashes.
            raise CommandError(str(exc))

        size = default_storage.size(key)
        elapsed = time.monotonic() - started
        self._say(self.style.SUCCESS(
            f'Built {written} figures into {_mb(size)} in {elapsed:.0f}s.'))
        self._say(f'  {default_storage.url(key)}')

        if missing:
            # Not fatal — 4,000 good files beat a failed run — but never silent:
            # a figure listed in manifest.csv and absent from figures/ is a
            # discrepancy the reader will find, so say it here first.
            self.stderr.write(self.style.WARNING(
                f'{len(missing)} figure(s) could not be read and are listed in '
                f'manifest.csv but absent from the archive:'))
            for line in missing[:20]:
                self.stderr.write(f'  - {line}')
            if len(missing) > 20:
                self.stderr.write(f'  ... and {len(missing) - 20} more')

        self._prune(version, options)

    def _prune(self, version, options, dry_run=False):
        dry = dry_run or options['dry_run']

        if options['keep_old']:
            self._say(self.style.WARNING(
                '--keep-old: superseded archives left in place, so nothing is '
                'bounding this prefix.'))
        else:
            removed = bulk_archive.prune(version, keep=options['keep'],
                                         dry_run=dry)
            if removed is None:
                # Nothing was deleted and nothing is known. Said out loud, or a
                # prefix that cannot be listed grows for ever under a log line
                # reporting a clean run every night.
                self.stderr.write(self.style.WARNING(
                    f'Could not list {bulk_archive.PREFIX}/, so nothing was '
                    f'pruned and superseded archives may be accumulating. The '
                    f'archive itself was written; this is about tidying up '
                    f'after it.'))
            elif removed:
                verb = 'Would remove' if dry else 'Removed'
                self._say(f'{verb} {len(removed)} superseded archive(s):')
                for key in removed:
                    self._say(f'  - {key}')

        # Printed on every run, including the no-op one. A scheduled job that
        # writes a few hundred megabytes needs its footprint in its own log, or
        # the first anybody knows about drift is a storage bill.
        count, total = bulk_archive.usage()
        if count is None:
            self.stderr.write(self.style.WARNING(
                f'{bulk_archive.PREFIX}/ could not be read, so its size is '
                f'unknown — this is not a report of zero.'))
        else:
            self.stdout.write(
                f'{bulk_archive.PREFIX}/ now holds {count} archive(s), '
                f'{_mb(total)}.')
