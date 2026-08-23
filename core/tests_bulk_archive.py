"""The one-file download — what it contains, and who is allowed the shared copy.

Three failures are possible here and only one of them is loud.

**Handing a narrowed consumer the shared archive.** It holds every public figure,
so redirecting a manufacturer scoped to their own catalogue would deliver every
competitor's figures in one file, with a 302 and no error anywhere. The owner's
decision that the competitor view is manufacturer-only exists precisely to stop
that, and a download endpoint is a second door to the same rows. This is the test
that matters most in the file.

**Serving an archive that no longer matches the dataset.** The key carries the
manifest's fingerprint so this cannot happen by construction — but "by
construction" is a claim about code that can be edited, so it is pinned: publish a
figure, and the key the endpoint asks for must move.

**A manifest.csv that does not join to the files beside it.** The CSV names a
`filename` column and the zip has a `figures/` directory; if those two are built
from different passes they drift, and the reader finds rows matching nothing.

What is deliberately not tested is the size or the speed of a real build. The
dataset is 473 MB in object storage and the fixture is four 1-pixel PNGs; a test
asserting bytes would be measuring the fixture.
"""
from __future__ import annotations

import base64
import csv
import io
import os
import shutil
import tempfile
import zipfile

from django.core.files.storage import default_storage
from django.test import override_settings
from django.urls import reverse

from core import bulk_archive
from core.api_manifest import STREAM_MAX_FILES, archive_inputs, is_full_scope
from core.models import APIConsumer
from core.tests_api_manifest import ManifestCase
from pipeline.models import PublicationImage

# A valid 1×1 PNG. Real bytes, because the builder opens the file — a fixture of
# empty strings passes every test a missing object also passes.
PIXEL = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAF'
    'AAH/iZk9HQAAAABJRU5ErkJggg==')


class ArchiveCase(ManifestCase):
    """The manifest fixture, with the image bytes actually on disk."""

    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.mkdtemp(prefix='oga-archive-test-')
        # PERSISTENT_MEDIA_ROOTS so ``storage_refusal`` passes for the right
        # reason: the tests run with USE_R2 off and DEBUG off, which is exactly
        # the combination the guard refuses, and a temp directory genuinely does
        # persist for the length of a test. Faking USE_R2 instead would make the
        # guard pass by asserting something untrue of this process.
        cls._override = override_settings(MEDIA_ROOT=cls._media,
                                          PERSISTENT_MEDIA_ROOTS=(cls._media,))
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)

    def setUp(self):
        super().setUp()
        # MEDIA_ROOT is per-class, so an archive built by one test is still on
        # disk for the next — and the fixture is identical, so it is still at
        # the *same* key. Without this, "not built yet" passes or fails
        # depending on alphabetical test order, which is the worst kind of
        # green. The image bytes are rewritten for the same reason.
        shutil.rmtree(os.path.join(self._media, bulk_archive.PREFIX),
                      ignore_errors=True)
        for key in self.all_keys:
            path = os.path.join(self._media, key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as fh:
                fh.write(PIXEL)

    def _download(self, consumer, **params):
        return self.client.get(reverse('api:download'), params,
                               HTTP_X_API_KEY=str(consumer.api_key))

    def _build(self):
        """Build the archive for the current dataset and return its version."""
        inputs = archive_inputs()
        bulk_archive.build(inputs['rows'], inputs['version'],
                           inputs['manifest_csv'], inputs['readme'])
        return inputs['version']


class TheSharedArchiveIsForFullScopeOnlyTests(ArchiveCase):
    """One object holds one scope, and it is the whole public dataset."""

    def test_a_supplier_scoped_consumer_is_never_redirected_to_it(self):
        """Abcam must not be handed Proteintech's figures.

        The shape to watch for is a 302: it is a success, the client follows it,
        and the file that lands is complete and correct for somebody else.
        """
        self.assertFalse(is_full_scope(self.abcam_only))

        self._build()
        response = self._download(self.abcam_only)

        self.assertNotEqual(response.status_code, 302,
                            'A supplier-scoped key was redirected to the shared '
                            'archive, which holds every supplier.')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/zip')

    def test_a_gene_scoped_consumer_is_never_redirected_to_it(self):
        self.assertFalse(is_full_scope(self.snca_only))
        self._build()
        self.assertNotEqual(self._download(self.snca_only).status_code, 302)

    def test_a_scoped_zip_holds_only_that_scope(self):
        """The redirect is refused; now check the thing built instead is right."""
        response = self._download(self.abcam_only)
        names = self._figure_names(response)

        # Abcam's two SNCA figures, and neither Proteintech row.
        self.assertEqual(len(names), 2)
        self.assertTrue(all('ab12345' in n for n in names), names)
        self.assertFalse(any('10842' in n or '66172' in n for n in names), names)

    def test_an_unrestricted_consumer_is_redirected(self):
        """The control: without this, every test above passes on a broken gate."""
        self.assertTrue(is_full_scope(self.consumer))
        self._build()
        response = self._download(self.consumer)
        self.assertEqual(response.status_code, 302)
        self.assertIn(bulk_archive.STEM, response['Location'])

    def _figure_names(self, response):
        archive = zipfile.ZipFile(io.BytesIO(response.getvalue()))
        return [n.split('/', 1)[1] for n in archive.namelist()
                if n.startswith('figures/')]


class TheArchiveCannotGoStaleTests(ArchiveCase):
    """The key is the fingerprint, so a changed dataset names a different key."""

    def test_publishing_a_figure_moves_the_key(self):
        before = archive_inputs()['version']

        PublicationImage.objects.create(
            antibody=self.ab_pt_mapt, application_type='WB',
            image='publication_images/2026/66172-1-Ig_WB.png')

        after = archive_inputs()['version']
        self.assertNotEqual(before, after)
        self.assertNotEqual(bulk_archive.key_for(before),
                            bulk_archive.key_for(after))

    def test_a_dataset_that_moved_stops_being_offered(self):
        """The honest failure: 503 and the manifest, never yesterday's zip.

        A stale archive would be a 302 to a complete, readable, *wrong* file —
        the one outcome worse than saying nothing is ready.
        """
        self._build()
        self.assertEqual(self._download(self.consumer).status_code, 302)

        PublicationImage.objects.create(
            antibody=self.ab_pt_mapt, application_type='WB',
            image='publication_images/2026/66172-1-Ig_WB.png')

        response = self._download(self.consumer)
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertIn('manifest', body)
        # It must not read as data loss: nothing is missing, one file is unbuilt.
        self.assertIn('Nothing is missing', body['detail'])

    def test_rebuilding_an_unchanged_dataset_writes_the_same_key(self):
        first = self._build()
        second = self._build()
        self.assertEqual(first, second)

    def test_pruning_keeps_the_current_archive_and_drops_the_rest(self):
        for old in ('deadbeef', 'cafebabe', 'f00dface'):
            default_storage.save(bulk_archive.key_for(old), io.BytesIO(b'old'))

        current = self._build()
        bulk_archive.prune(current, keep=1)

        self.assertTrue(default_storage.exists(bulk_archive.key_for(current)))
        self.assertEqual(len(bulk_archive._archive_names()), 1)

    def test_the_prefix_is_bounded_however_many_times_it_is_rebuilt(self):
        """The answer to "does this keep getting bigger".

        Every dataset change writes a new key rather than overwriting one, so
        without a bound each change would leave a few hundred megabytes behind
        for ever. Rebuilt ten times here; the count must not track the number of
        rebuilds.
        """
        for n in range(10):
            default_storage.save(bulk_archive.key_for(f'old{n}'),
                                 io.BytesIO(b'x' * 100))
            bulk_archive.prune(self._build())

        count, total = bulk_archive.usage()
        self.assertLessEqual(count, bulk_archive.KEEP_ARCHIVES)
        self.assertGreater(total, 0, 'usage() reported nothing for real objects.')

    def test_one_superseded_archive_survives_a_prune(self):
        """A download in flight when the nightly build runs must not 404.

        Keeping exactly the current archive would delete the object somebody is
        part way through fetching — minutes, for a few hundred megabytes on a
        slow link, and the build runs unattended in the night.
        """
        previous = bulk_archive.key_for('deadbeef')
        default_storage.save(previous, io.BytesIO(b'still downloading'))

        bulk_archive.prune(self._build())

        self.assertTrue(default_storage.exists(previous))

    def test_a_prefix_that_cannot_be_listed_is_not_reported_as_empty(self):
        """A failed read is not an empty result.

        `0 archive(s), 0.0 MB` is what a healthy first run prints *and* what a
        bucket nobody can list would print — so if listing ever broke, the one
        number written down to catch the prefix growing is the number that would
        hide it, every night, in a log that otherwise reads clean.
        """
        from unittest import mock

        from django.core.files.storage import FileSystemStorage

        self._build()
        with mock.patch.object(FileSystemStorage, 'listdir',
                               side_effect=OSError('bucket unreachable')):
            self.assertIsNone(bulk_archive._archive_names())
            self.assertEqual(bulk_archive.usage(), (None, None))
            self.assertIsNone(bulk_archive.prune('whatever'))

    def test_the_command_says_it_could_not_look(self):
        from unittest import mock

        from django.core.files.storage import FileSystemStorage
        from django.core.management import call_command

        out = io.StringIO()
        with mock.patch.object(FileSystemStorage, 'listdir',
                               side_effect=OSError('bucket unreachable')):
            call_command('build_bulk_archive', stdout=out, stderr=out)

        output = out.getvalue()
        self.assertIn('could not be read', output)
        self.assertNotIn('holds 0 archive(s)', output)
        # The archive itself still landed — this is about tidying up after it.
        self.assertIn('Built 4 figures', output)

    def test_an_empty_prefix_still_reports_zero(self):
        """The control: without this the test above passes on a broken read."""
        count, total = bulk_archive.usage()
        self.assertEqual((count, total), (0, 0))

    def test_the_current_archive_survives_an_unreadable_timestamp(self):
        """Kept by name, never by position in a sorted list."""
        from unittest import mock

        from django.core.files.storage import FileSystemStorage

        current = self._build()
        default_storage.save(bulk_archive.key_for('deadbeef'), io.BytesIO(b'old'))

        with mock.patch.object(FileSystemStorage, 'get_modified_time',
                               side_effect=OSError('no timestamp')):
            bulk_archive.prune(current, keep=1)

        self.assertTrue(default_storage.exists(bulk_archive.key_for(current)))

    def test_pruning_never_touches_anything_else_in_the_bucket(self):
        """It deletes by a pattern only this module writes."""
        bystander = f'{bulk_archive.PREFIX}/notes.txt'
        default_storage.save(bystander, io.BytesIO(b'not mine'))

        bulk_archive.prune(self._build())

        self.assertTrue(default_storage.exists(bystander))


class TheArchiveJoinsToItsManifestTests(ArchiveCase):
    """A CSV whose filenames match nothing in the zip is worse than no CSV."""

    def test_every_csv_row_names_a_file_that_is_in_the_archive(self):
        inputs = archive_inputs()
        bulk_archive.build(inputs['rows'], inputs['version'],
                           inputs['manifest_csv'], inputs['readme'])

        with default_storage.open(bulk_archive.key_for(inputs['version'])) as fh:
            archive = zipfile.ZipFile(io.BytesIO(fh.read()))

        in_zip = {n.split('/', 1)[1] for n in archive.namelist()
                  if n.startswith('figures/')}
        rows = list(csv.DictReader(
            io.StringIO(archive.read('manifest.csv').decode())))

        self.assertTrue(rows)
        self.assertEqual({r['filename'] for r in rows}, in_zip)

    def test_the_readme_states_the_scope_of_a_recommendation(self):
        """The archive travels without the site around it.

        Somebody unzips this in six months with no page to read, so the one
        sentence that makes `not_recommended` a statement about an experiment
        rather than about a product has to be inside the file.
        """
        from core import recommendations as R

        inputs = archive_inputs()
        # Compared with whitespace collapsed: the README wraps to 78 columns for
        # a plain-text reader, so asserting the sentence verbatim would pin the
        # line breaks rather than the words.
        flat = ' '.join(inputs['readme'].split())
        self.assertIn(' '.join(R.SCOPE_NOTE.split()), flat)

    def test_the_archive_carries_real_image_bytes(self):
        """A zip of the right names and no content would pass every test above."""
        inputs = archive_inputs()
        bulk_archive.build(inputs['rows'], inputs['version'],
                           inputs['manifest_csv'], inputs['readme'])

        with default_storage.open(bulk_archive.key_for(inputs['version'])) as fh:
            archive = zipfile.ZipFile(io.BytesIO(fh.read()))

        figures = [n for n in archive.namelist() if n.startswith('figures/')]
        self.assertEqual(len(figures), 4)
        for name in figures:
            self.assertEqual(archive.read(name), PIXEL)


class TheDownloadEndpointAnswersHonestlyTests(ArchiveCase):

    def test_a_gene_filter_builds_a_zip_of_that_gene(self):
        response = self._download(self.consumer, gene='MAPT')
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.getvalue()))
        figures = [n for n in archive.namelist() if n.startswith('figures/')]
        self.assertEqual(len(figures), 1)
        self.assertIn('MAPT', figures[0])
        self.assertIn('attachment; filename="oga-MAPT.zip"',
                      response['Content-Disposition'])

    def test_a_gene_nobody_has_is_a_404_that_says_where_to_look(self):
        response = self._download(self.consumer, gene='NOTAGENE')
        self.assertEqual(response.status_code, 404)
        self.assertIn('/api/v1/genes/', response.json()['detail'])

    def test_it_needs_a_key(self):
        response = self.client.get(reverse('api:download'))
        self.assertIn(response.status_code, (401, 403))

    def test_the_cap_refuses_by_name_and_points_somewhere(self):
        """Never a silent truncation: a partial zip reads as a complete dataset."""
        from unittest import mock

        with mock.patch('core.api_manifest.STREAM_MAX_FILES', 1):
            response = self._download(self.consumer, gene='SNCA')

        self.assertEqual(response.status_code, 413)
        body = response.json()
        self.assertIn('too many', body['error'])
        self.assertIn('manifest', body)

    def test_the_cap_is_a_real_number(self):
        """Guards the mock above from passing against a cap of zero."""
        self.assertGreater(STREAM_MAX_FILES, 100)


class TheManifestPointsAtTheArchiveTests(ArchiveCase):
    """A prepared archive nothing mentions is one nobody finds."""

    def test_the_manifest_carries_the_archive_url_once_built(self):
        version = self._build()
        block = self._manifest(self.consumer).json()['bulk_download']

        self.assertTrue(block['ready'])
        self.assertIn(version, block['url'])
        self.assertGreater(block['bytes'], 0)

    def test_an_unbuilt_archive_is_reported_as_not_ready_not_as_missing(self):
        block = self._manifest(self.consumer).json()['bulk_download']
        self.assertFalse(block['ready'])
        self.assertIn('can be fetched from its URL', block['note'])

    def test_a_scoped_consumer_is_told_theirs_is_built_per_request(self):
        self._build()
        block = self._manifest(self.abcam_only).json()['bulk_download']
        self.assertEqual(block['scope'], 'built_per_request')
        self.assertNotIn('url', block)

    def test_the_catalogue_lists_the_endpoint(self):
        catalogue = self.client.get(reverse('api:api_index')).json()
        paths = ' '.join(e['path'] for e in catalogue['endpoints'])
        self.assertIn('/api/v1/download/', paths)


class TheBuildCommandRunsTests(ArchiveCase):
    """The cron entry point, driven rather than read.

    ``manage.py check`` passes over a command whose error only exists when the
    line runs — the rule CLAUDE.md records about ``board.js`` helpers declared in
    the wrong function. A cron job nobody has executed is the same shape: it
    goes green in Render's dashboard by exiting 0, and the archive it was
    supposed to build is simply never there.
    """

    def _run(self, **kwargs):
        from django.core.management import call_command

        out = io.StringIO()
        call_command('build_bulk_archive', stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_it_builds_an_archive_the_endpoint_then_redirects_to(self):
        self.assertEqual(self._download(self.consumer).status_code, 503)

        output = self._run()

        self.assertIn('Built 4 figures', output)
        self.assertEqual(self._download(self.consumer).status_code, 302)

    def test_running_it_twice_does_not_rebuild(self):
        self._run()
        self.assertIn('nothing to do', self._run())

    def test_dry_run_writes_nothing(self):
        output = self._run(dry_run=True)

        self.assertIn('Dry run', output)
        self.assertEqual(self._download(self.consumer).status_code, 503)

    def test_repeated_builds_do_not_accumulate(self):
        """The bound holds through the command, not only through ``prune``.

        One superseded archive is kept on purpose, so the first rebuild leaves
        two; the point is that the *tenth* also leaves two.
        """
        self._run()
        first = bulk_archive.key_for(archive_inputs()['version'])

        # A flipped recommendation moves the fingerprint exactly as a new figure
        # does, and needs no second image: PublicationImage is unique on
        # (antibody, application_type), so one antibody cannot have two WBs.
        for antibody in (self.ab_abcam, self.ab_pt_snca, self.ab_pt_mapt):
            antibody.wb_recommended = True
            antibody.save(update_fields=['wb_recommended'])
            output = self._run()

        self.assertFalse(default_storage.exists(first),
                         'The first archive survived three rebuilds.')
        self.assertLessEqual(len(bulk_archive._archive_names()),
                             bulk_archive.KEEP_ARCHIVES)
        self.assertEqual(self._download(self.consumer).status_code, 302)
        # Every run says the footprint, or drift is invisible in a cron log.
        self.assertIn('archive(s)', output)

    def test_an_unreadable_figure_is_named_and_does_not_stop_the_build(self):
        """4,000 good files beat a failed run — but never silently."""
        os.remove(os.path.join(self._media, self.FC_KEY))

        output = self._run()

        self.assertIn('Built 3 figures', output)
        self.assertIn('could not be read', output)
        self.assertEqual(self._download(self.consumer).status_code, 302)


class ARunThatKeptNothingIsNotASuccessTests(ArchiveCase):
    """Written from the first real cron run, which reported success having
    read none of its 4,321 figures.

    The job was created without the R2 credentials, so every read went to the
    container's own filesystem and failed. The command wrote a 1.5 MB zip
    holding a manifest, a README and no images, printed
    ``bulk/ now holds 1 archive(s)`` about a file in a container that was about
    to be destroyed, exited 0, and Render printed *"Cron job run finished
    successfully"*.

    Nothing reached a reader — the web service looks for the archive in R2 and
    correctly found none, so the endpoint went on answering 503. The damage was
    entirely in the log saying the opposite of what happened, which is the shape
    this whole file exists to catch.
    """

    def _run(self, **kwargs):
        from django.core.management import call_command

        out = io.StringIO()
        call_command('build_bulk_archive', stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_it_refuses_to_run_where_it_cannot_keep_what_it_writes(self):
        from django.core.management.base import CommandError

        with override_settings(USE_R2=False, DEBUG=False,
                               PERSISTENT_MEDIA_ROOTS=()):
            self.assertTrue(bulk_archive.storage_refusal())
            with self.assertRaises(CommandError) as caught:
                self._run()

        # Names the variables: this message reaches whoever reads a cron log,
        # who is exactly the person who can set them.
        self.assertIn('USE_R2', str(caught.exception))
        self.assertIn('environment group', str(caught.exception))

    def test_it_refuses_before_reading_anything(self):
        """The refusal is worth nothing if it arrives after 4,321 failed reads."""
        from unittest import mock

        from django.core.management.base import CommandError

        with override_settings(USE_R2=False, DEBUG=False,
                               PERSISTENT_MEDIA_ROOTS=()):
            with mock.patch('core.api_manifest.archive_inputs') as inputs:
                with self.assertRaises(CommandError):
                    self._run()
        inputs.assert_not_called()

    def test_r2_being_on_is_enough(self):
        """The control: the guard must not refuse a correctly configured job."""
        with override_settings(USE_R2=True, PERSISTENT_MEDIA_ROOTS=()):
            self.assertEqual(bulk_archive.storage_refusal(), '')

    def test_losing_every_figure_writes_no_archive_at_all(self):
        from django.core.management.base import CommandError

        for key in self.all_keys:
            os.remove(os.path.join(self._media, key))

        with self.assertRaises(CommandError) as caught:
            self._run()

        self.assertIn('could not be read', str(caught.exception))
        self.assertEqual(bulk_archive._archive_names(), [],
                         'An archive with no figures in it was published.')
        # And the endpoint still says so honestly rather than serving it.
        self.assertEqual(self._download(self.consumer).status_code, 503)

    def test_losing_a_few_figures_still_builds(self):
        """The allowance is the reason this is a fraction and not zero.

        A handful of unreadable objects is a real thing and must not cost the
        other four thousand. With a four-figure fixture the threshold rounds to
        one, which is the smallest case that distinguishes the two policies.
        """
        os.remove(os.path.join(self._media, self.FC_KEY))

        output = self._run()

        self.assertIn('Built 3 figures', output)
        self.assertIn('could not be read', output)
        self.assertEqual(self._download(self.consumer).status_code, 302)


class TheArchiveIsAssembledOnDiskTests(ArchiveCase):
    """Peak memory must not scale with the size of the archive.

    It did: the zip was assembled in a ``BytesIO``, on a docstring asserting
    that "a Render cron container has more than" 473 MB. Nobody checked. The job
    runs on a **Starter** instance — 512 MB — so a few hundred megabytes of zip
    held whole, plus each figure while it is added, plus Django and boto3, is an
    out-of-memory kill part way through a multi-minute job. Render reports that
    as a failed run with no output saying why.

    A four-figure fixture cannot reproduce it, which is the whole reason to
    measure the property rather than the outcome: every other test in this file
    passes just as happily on the version that would die in production.
    """

    #: Big enough that holding the archive whole is unmistakable against the
    #: 64 KB chunks copyfileobj uses, small enough to stay a fast test.
    BLOB = b'\x89PNG\r\n\x1a\n' + b'x' * (2 * 1024 * 1024)

    def setUp(self):
        super().setUp()
        for key in self.all_keys:
            with open(os.path.join(self._media, key), 'wb') as fh:
                fh.write(self.BLOB)

    def test_peak_memory_does_not_hold_the_whole_archive(self):
        import tracemalloc

        inputs = archive_inputs()

        tracemalloc.start()
        try:
            # concurrency=1 so this measures the archive being held, not the
            # read batch. Peak tracks READ_CONCURRENCY by design — the fixture
            # is four figures, so the default 16 would fetch all of them at once
            # and the two policies would be indistinguishable here.
            bulk_archive.build(inputs['rows'], inputs['version'],
                               inputs['manifest_csv'], inputs['readme'],
                               concurrency=1)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        size = default_storage.size(bulk_archive.key_for(inputs['version']))
        self.assertGreater(size, 4 * 1024 * 1024,
                           'The fixture is too small for this test to mean '
                           'anything.')

        # The ceiling is a small multiple of ONE figure, not a fraction of the
        # archive: peak should track how many figures are in flight and nothing
        # else. Three allows for the fetched bytes plus zipfile's own copy of
        # them. Holding the archive whole peaks above the total instead, which
        # this catches however large the dataset grows.
        ceiling = 3 * len(self.BLOB)
        self.assertLess(
            peak, ceiling,
            f'Building a {size / 1048576:.1f} MB archive from four '
            f'{len(self.BLOB) / 1048576:.0f} MB figures peaked at '
            f'{peak / 1048576:.1f} MB of Python allocation, one at a time. It '
            f'is being accumulated rather than staged on disk, which is an OOM '
            f'kill on the 512 MB instance this runs on.')

    def test_reading_in_parallel_keeps_every_figure_and_its_name(self):
        """Concurrency must not reorder a name onto the wrong bytes.

        Each worker carries its own row back rather than the caller pairing
        results to inputs by position, so a batch that completes out of order
        cannot file one antibody's figure under another's catalogue number —
        which would be invisible in the zip and wrong in the manifest.
        """
        inputs = archive_inputs()
        bulk_archive.build(inputs['rows'], inputs['version'],
                           inputs['manifest_csv'], inputs['readme'],
                           concurrency=4)

        with default_storage.open(bulk_archive.key_for(inputs['version'])) as fh:
            archive = zipfile.ZipFile(io.BytesIO(fh.read()))

        for row in inputs['rows']:
            stored = archive.read(f'figures/{row["filename"]}')
            with open(os.path.join(self._media, row['_image'].name), 'rb') as f:
                self.assertEqual(stored, f.read(), row['filename'])

    def test_the_archive_is_still_correct(self):
        """The control: streaming must not have cost the contents."""
        inputs = archive_inputs()
        bulk_archive.build(inputs['rows'], inputs['version'],
                           inputs['manifest_csv'], inputs['readme'])

        with default_storage.open(bulk_archive.key_for(inputs['version'])) as fh:
            archive = zipfile.ZipFile(io.BytesIO(fh.read()))

        figures = [n for n in archive.namelist() if n.startswith('figures/')]
        self.assertEqual(len(figures), 4)
        for name in figures:
            self.assertEqual(archive.read(name), self.BLOB)
        self.assertIn('manifest.csv', archive.namelist())


class FakeS3Client:
    """Enough of the S3 multipart API to check our half of the conversation.

    Records what was sent rather than pretending to be S3. It cannot tell us
    that R2 accepts this — only a real run does that — but it can tell us the
    parts are numbered from one, in order, that every part but the last clears
    the 5 MB floor S3 enforces, and that nothing is completed when the build
    raised. Those are the things that would be wrong in *our* code.
    """

    def __init__(self):
        self.parts = []
        self.completed = None
        self.aborted = False
        self.body = b''

    def create_multipart_upload(self, **kw):
        self.created = kw
        return {'UploadId': 'test-upload'}

    def upload_part(self, *, PartNumber, Body, **kw):
        self.parts.append((PartNumber, len(Body)))
        self.body += Body
        return {'ETag': f'"etag-{PartNumber}"'}

    def complete_multipart_upload(self, *, MultipartUpload, **kw):
        self.completed = MultipartUpload['Parts']

    def abort_multipart_upload(self, **kw):
        self.aborted = True


class TheStreamingUploadTests(ArchiveCase):
    """The path production actually takes, which no other test reaches.

    Local storage has no multipart API, so every other test in this file runs
    the temp-file sink. That is precisely the trap this project keeps paying
    for: the tested path and the shipped path being different ones.
    """

    SMALL_PART = 64 * 1024

    def _build_into(self, client, rows=None, concurrency=4):
        inputs = archive_inputs()
        sink = bulk_archive._MultipartSink(
            client, 'test-bucket', 'bulk/x.zip', part_size=self.SMALL_PART)
        from unittest import mock
        with mock.patch.object(bulk_archive, '_open_sink', return_value=sink):
            bulk_archive.build(rows if rows is not None else inputs['rows'],
                               inputs['version'], inputs['manifest_csv'],
                               inputs['readme'], concurrency=concurrency)
        return inputs

    def setUp(self):
        super().setUp()
        # Big enough to span several parts at the small part size above.
        for key in self.all_keys:
            with open(os.path.join(self._media, key), 'wb') as fh:
                fh.write(b'\x89PNG\r\n\x1a\n' + os.urandom(100 * 1024))

    def test_it_uploads_parts_and_completes_once(self):
        client = FakeS3Client()
        self._build_into(client)

        self.assertGreater(len(client.parts), 1, 'Only one part — the fixture '
                                                 'no longer spans several.')
        self.assertEqual([n for n, _ in client.parts],
                         list(range(1, len(client.parts) + 1)))
        self.assertIsNotNone(client.completed)
        self.assertFalse(client.aborted)

    def test_every_part_but_the_last_clears_the_five_megabyte_floor(self):
        """S3 and R2 reject a multipart upload whose middle parts are small.

        Asserted against PART_SIZE rather than the test's own, because the
        constant is what production uses and a change to it is what would
        break this for real.
        """
        self.assertGreaterEqual(bulk_archive.PART_SIZE, 5 * 1024 * 1024)

        client = FakeS3Client()
        self._build_into(client)
        for number, size in client.parts[:-1]:
            self.assertEqual(size, self.SMALL_PART,
                             f'Part {number} is not a full part.')

    def test_the_bytes_uploaded_are_a_readable_zip(self):
        """Non-seekable writing means data descriptors — check they parse."""
        client = FakeS3Client()
        inputs = self._build_into(client)

        archive = zipfile.ZipFile(io.BytesIO(client.body))
        self.assertIsNone(archive.testzip(), 'The uploaded bytes are a corrupt '
                                             'zip.')
        figures = [n for n in archive.namelist() if n.startswith('figures/')]
        self.assertEqual(len(figures), 4)
        self.assertIn('manifest.csv', archive.namelist())
        for row in inputs['rows']:
            with open(os.path.join(self._media, row['_image'].name), 'rb') as f:
                self.assertEqual(archive.read(f'figures/{row["filename"]}'),
                                 f.read())

    def test_a_refused_build_aborts_the_upload_and_completes_nothing(self):
        """Parts are billed until aborted, and a half-upload must not become
        an object the API hands out as the dataset."""
        for key in self.all_keys:
            os.remove(os.path.join(self._media, key))

        client = FakeS3Client()
        with self.assertRaises(bulk_archive.IncompleteArchive):
            self._build_into(client)

        self.assertTrue(client.aborted)
        self.assertIsNone(client.completed,
                          'A build that was refused still completed the upload.')

    def test_an_unexpected_failure_also_aborts(self):
        from unittest import mock

        client = FakeS3Client()
        with mock.patch.object(bulk_archive, '_fetch_via_storage',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._build_into(client)

        self.assertTrue(client.aborted)
        self.assertIsNone(client.completed)

    def test_a_failing_abort_does_not_mask_the_real_error(self):
        from unittest import mock

        client = FakeS3Client()
        client.abort_multipart_upload = mock.Mock(
            side_effect=RuntimeError('abort failed too'))

        with mock.patch.object(bulk_archive, '_fetch_via_storage',
                               side_effect=ValueError('the real problem')):
            with self.assertRaises(ValueError) as caught:
                self._build_into(client)
        self.assertIn('the real problem', str(caught.exception))

    def test_s3_backed_storage_gets_the_streaming_sink(self):
        """The branch itself — the one thing that decides which path runs."""
        client = FakeS3Client()
        sink = bulk_archive._open_sink('bulk/x.zip', client, 'oga')
        self.assertIsInstance(sink, bulk_archive._MultipartSink)
        self.assertEqual(client.created['Bucket'], 'oga')

    def test_local_storage_gets_the_staged_sink(self):
        client, bucket = bulk_archive._s3_client(4)
        self.assertIsNone(client, 'Local storage was mistaken for object '
                                  'storage, so the build would try multipart.')
        sink = bulk_archive._open_sink('bulk/x.zip', client, bucket)
        self.assertIsInstance(sink, bulk_archive._StagedSink)
        sink.abort()

    def test_the_shared_client_is_pooled_wide_enough_for_the_workers(self):
        """Botocore's default pool is 10, and every worker above that queues.

        Measured: concurrency 16 through per-thread storage connections gave a
        3.2x speedup rather than ~16x, then stalled. One client is only an
        improvement if its pool fits the workers using it.
        """
        from unittest import mock

        from botocore.config import Config

        captured = {}

        def fake_client(service, **kw):
            captured.update(kw)
            return FakeS3Client()

        storage = mock.Mock(bucket_name='oga', access_key='k', secret_key='s',
                            region_name='auto')
        storage.connection.meta.client.meta.config = Config()
        storage.connection.meta.client.meta.endpoint_url = 'https://r2.example'

        with mock.patch.object(bulk_archive, 'default_storage', storage), \
                mock.patch('boto3.client', fake_client):
            bulk_archive._s3_client(16)

        self.assertGreaterEqual(captured['config'].max_pool_connections, 16)
        self.assertIsNotNone(captured['config'].read_timeout,
                             'No read timeout, so a stalled fetch hangs the '
                             'whole job with nothing in the log.')

    def test_a_part_upload_is_reported(self):
        """The reads ticked and the upload did not, so a stall was unreadable."""
        client = FakeS3Client()
        seen = []
        sink = bulk_archive._MultipartSink(client, 'oga', 'bulk/x.zip',
                                           part_size=1024)
        sink.on_part(lambda number, sent: seen.append((number, sent)))
        sink.write(b'z' * 4096)
        self.assertEqual([n for n, _ in seen], [1, 2, 3, 4])
        sink.abort()


class TheManifestStaysSerialisableTests(ArchiveCase):
    """``attach_images`` hangs a FieldFile on a row that JsonResponse also sees.

    A model object in a row is a 500 that takes the whole response down and
    reads as data loss — the rule CLAUDE.md records about board rows. The
    archive builder needs the bytes; the endpoint must never get them.
    """

    def test_the_json_manifest_still_renders(self):
        response = self._manifest(self.consumer)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['files']), 4)

    def test_no_row_the_endpoint_returns_carries_an_image_object(self):
        for row in self._manifest(self.consumer).json()['files']:
            self.assertNotIn('_image', row)

    def test_the_csv_manifest_still_renders(self):
        response = self._manifest(self.consumer, format='csv')
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(len(rows), 4)
        self.assertNotIn('_image', rows[0])
