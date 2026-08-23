"""The download manifest — what a machine is promised, and what it is told.

Every failure this file guards is the same shape: the endpoint answers 200 with a
list that is *quietly* wrong about the dataset, and the client on the other end
acts on it without a person ever reading the reply.

Four ways that happens, and each has a test below.

**A read that writes.** ``/v1/antibodies/`` moves ``last_queried_at``, which is
the review cursor, so calling it twice does not give the same answer twice. The
manifest must never touch it — a machine that fails mid-parse has to be able to
ask again.

**A cursor that misses a change.** ``PublicationImage.created_at`` is
``auto_now_add``, so replacing a figure in place leaves it where it was. Anything
keyed on time calls that dataset unchanged while the file behind the URL is a
different image. The ETag is over image identity and every verdict in scope for
exactly that reason, so a replaced figure and a flipped recommendation both break
it.

**A truncated list read as a deletion.** The manifest is what a client diffs
against what it holds, so a URL that is absent is a file it removes. A capped
reply that does not say it was capped is therefore a silent instruction to delete
data that is still in the dataset.

**A scope leak.** A manufacturer's supplier filter and a demo account's gene
filter decide what that consumer may see at all; the manifest is a second door to
the same rows as ``/v1/antibodies/`` and has to enforce them identically.

Plus the two smaller ones: a filename collision the browser zip resolves by
keeping whichever file landed last, and an API whose endpoint list exists only in
a Python file.
"""
import csv
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import recommendations as R
from core.api_manifest import CSV_COLUMNS
from core.api_views import BASE_URL
from core.models import APIConsumer
from pipeline.models import Antibody, Company, PublicationImage, Target


def _url_for(stored_name):
    """The public URL a stored figure must appear under.

    Written out rather than derived from ``image.url``, so it asserts the shape
    the consumer is promised — this origin, then ``MEDIA_URL``, then the object
    key — instead of agreeing with the view by construction.
    """
    return f'{BASE_URL}/media/{stored_name}'


class ManifestCase(TestCase):
    """Fixture and helpers. No tests of its own.

    The figures cross-cut supplier and gene on purpose: SNCA is carried by two
    suppliers and Proteintech carries two genes, so a supplier filter and a gene
    filter select genuinely different subsets. With a fixture where they select
    the same rows, one filter silently doing the other's job is invisible.
    """
    databases = {'default', 'pipeline_db', 'academy_db'}

    # Stored object keys, kept as constants so the URL and filename assertions
    # below can name them without repeating the fixture.
    WB_KEY = 'publication_images/2026/ab12345_WB.png'
    IP_KEY = 'publication_images/2026/ab12345_IP.png'
    FC_KEY = 'publication_images/2026/10842-1-AP_FC.png'
    ICC_KEY = 'publication_images/2026/66172-1-Ig_ICC.png'

    @classmethod
    def setUpTestData(cls):
        cls.abcam = Company.objects.create(name='Abcam')
        cls.proteintech = Company.objects.create(name='Proteintech')

        cls.snca = Target.objects.create(protein_name='Synuclein',
                                         gene_name='SNCA')
        cls.mapt = Target.objects.create(protein_name='Tau', gene_name='MAPT')

        # Abcam, SNCA — two figures for one antibody, which is what makes a
        # filename pattern without {application} collide.
        cls.ab_abcam = Antibody.objects.create(
            target=cls.snca, company=cls.abcam, catalogue_number='ab12345')
        cls.wb = PublicationImage.objects.create(
            antibody=cls.ab_abcam, application_type='WB', image=cls.WB_KEY)
        cls.ip = PublicationImage.objects.create(
            antibody=cls.ab_abcam, application_type='IP', image=cls.IP_KEY)

        # Proteintech, SNCA — same gene, different supplier.
        cls.ab_pt_snca = Antibody.objects.create(
            target=cls.snca, company=cls.proteintech,
            catalogue_number='10842-1-AP')
        cls.fc = PublicationImage.objects.create(
            antibody=cls.ab_pt_snca, application_type='FC', image=cls.FC_KEY)

        # Proteintech, MAPT — same supplier, different gene.
        cls.ab_pt_mapt = Antibody.objects.create(
            target=cls.mapt, company=cls.proteintech,
            catalogue_number='66172-1-Ig')
        cls.icc = PublicationImage.objects.create(
            antibody=cls.ab_pt_mapt, application_type='ICC-IF',
            image=cls.ICC_KEY)

        cls.all_keys = {cls.WB_KEY, cls.IP_KEY, cls.FC_KEY, cls.ICC_KEY}

        # Sees everything, and is entitled to verdicts.
        cls.consumer = APIConsumer.objects.create(
            name='Test Manufacturer', consumer_type='manufacturer', tier='data')
        # Same scope, no verdicts — the registry entitlement.
        cls.registry = APIConsumer.objects.create(
            name='Antibody Registry', consumer_type='rrid', tier='data')
        # Below the gate.
        cls.free_tier = APIConsumer.objects.create(
            name='Free Reader', consumer_type='manufacturer', tier='free')
        # Narrowed by supplier: Abcam's two SNCA figures only.
        cls.abcam_only = APIConsumer.objects.create(
            name='Abcam', consumer_type='manufacturer', tier='data',
            supplier_filter='Abcam')
        # Narrowed by gene: the three SNCA figures, both suppliers.
        cls.snca_only = APIConsumer.objects.create(
            name='Demo Account', consumer_type='manufacturer', tier='data',
            gene_filter='SNCA')

    def setUp(self):
        # LocMemCache outlives a test, and the throttle counts live in it — so
        # without this the later tests in a run inherit the earlier ones' budget.
        cache.clear()

    # --- helpers -------------------------------------------------------

    def _manifest(self, consumer, if_none_match=None, **params):
        extra = {'HTTP_X_API_KEY': str(consumer.api_key)}
        if if_none_match is not None:
            extra['HTTP_IF_NONE_MATCH'] = if_none_match
        return self.client.get(reverse('api:manifest'), params, **extra)

    def _etag(self, consumer=None):
        response = self._manifest(consumer or self.consumer)
        self.assertEqual(response.status_code, 200)
        return response['ETag']


class TheManifestNeverWritesTests(ManifestCase):

    def test_reading_the_manifest_does_not_move_the_review_cursor(self):
        """A read a computer makes must be repeatable.

        ``last_queried_at`` is the review cursor and the ``since`` for the delta
        feed, so a manifest call that advanced it would silently change what the
        *next* ``/v1/antibodies/`` request returns — and a client that failed
        while parsing would have lost that delta by asking for a file list. The
        second half of this test is the control: it proves the cursor on this
        consumer moves at all, so the first half cannot pass by accident.
        """
        stamp = timezone.now() - timedelta(days=3)
        APIConsumer.objects.filter(pk=self.consumer.pk).update(
            last_queried_at=stamp)
        before = APIConsumer.objects.get(pk=self.consumer.pk).last_queried_at

        response = self._manifest(self.consumer)
        self.assertEqual(response.status_code, 200)

        after = APIConsumer.objects.get(pk=self.consumer.pk).last_queried_at
        self.assertEqual(after, before)

        # Control: the endpoint that is *supposed* to write still does.
        feed = self.client.get(reverse('api:antibodies_feed'),
                               HTTP_X_API_KEY=str(self.consumer.api_key))
        self.assertEqual(feed.status_code, 200)
        moved = APIConsumer.objects.get(pk=self.consumer.pk).last_queried_at
        self.assertGreater(moved, before)


class TheEtagTests(ManifestCase):

    def test_sending_the_etag_back_answers_304_with_no_body(self):
        """The whole reason the default is the complete manifest.

        Returning everything on every reconnect is only affordable because an
        unchanged dataset costs a header exchange. If the conditional request
        were not honoured, every client would be pulling the full list on every
        poll, and the advice in the reply — reconnect with If-None-Match —
        would be wrong.
        """
        first = self._manifest(self.consumer)
        self.assertEqual(first.status_code, 200)
        etag = first['ETag']
        self.assertTrue(etag)

        second = self._manifest(self.consumer, if_none_match=etag)
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b'')
        self.assertEqual(second['ETag'], etag)

    def test_a_weakened_etag_still_answers_304(self):
        """The form production actually sends back, and it used to 200.

        A cache or proxy that changes the entity body must weaken the tag it
        forwards, and Cloudflare compresses ours — so what a partner stores is
        `W/"abc-json"` and what they send back is `W/"abc-json"`. The
        comparison was `header == etag`, exact, so it never matched: every
        reconnect got a full 200 and the whole 1.5 MB manifest, on the one
        endpoint built to make that unnecessary.

        Every test above passed throughout, because the Django test client
        speaks to the app directly and no proxy is in the way. It took running
        `bin/check_api.py` against the live API with a real key (7 Aug 2026).
        RFC 9110 §13.1.2 requires weak comparison here, so this was also just
        wrong, not merely unlucky.
        """
        first = self._manifest(self.consumer)
        strong = first['ETag']

        for label, sent in [
            ('weak', f'W/{strong}'),
            ('lowercase weak prefix', f'w/{strong}'),
            ('in a list', f'"something-else", {strong}'),
            ('weak, in a list', f'W/"something-else", W/{strong}'),
            ('star', '*'),
        ]:
            with self.subTest(form=label):
                again = self._manifest(self.consumer, if_none_match=sent)
                self.assertEqual(
                    304, again.status_code,
                    f'If-None-Match: {sent} did not match {strong}')
                self.assertEqual(b'', again.content)

    def test_an_unrelated_etag_still_answers_200(self):
        """The other direction, so the fix above cannot become "always 304"."""
        again = self._manifest(self.consumer, if_none_match='W/"not-ours-json"')
        self.assertEqual(200, again.status_code)

    def test_following_the_reply_s_own_reconnect_instruction_gives_a_304(self):
        """Do exactly what the manifest tells you to do, and it must work.

        ``sync.reconnect_with`` used to read ``If-None-Match: <dataset_version>``
        while the view compared against the ETag — which is deliberately not
        ``dataset_version``, since it names the format too (a client that cached
        the CSV and asked for JSON with one tag would be told 304 and read CSV
        bytes as JSON). So a client that followed the instruction literally got
        200 every time and re-downloaded the whole manifest for ever, with
        nothing anywhere reporting a fault: the endpoint answered, the data was
        right, and the one feature the instruction exists to enable never
        engaged.

        Asserting the two strings relate to each other is the weaker test — it
        passes for any consistent pair. This parses the header out of the
        sentence the API itself prints and sends that.
        """
        first = self._manifest(self.consumer)
        instruction = first.json()['sync']['reconnect_with']
        name, _, value = instruction.partition(':')
        self.assertEqual(name.strip(), 'If-None-Match')

        again = self._manifest(self.consumer, if_none_match=value.strip())
        self.assertEqual(
            again.status_code, 304,
            f'Following {instruction!r} returned {again.status_code}, so the '
            'conditional request the manifest advertises does not work.')

    def test_the_etag_is_per_format(self):
        """A cached CSV must not satisfy a request for JSON.

        An ETag identifies a representation, not a dataset. Share one across
        formats and a client that holds the CSV, then asks for JSON with that
        tag, is told 304 and goes on parsing CSV bytes as JSON.
        """
        as_json = self._manifest(self.consumer)
        as_csv = self._manifest(self.consumer, format='csv')
        self.assertNotEqual(as_json['ETag'], as_csv['ETag'])

        crossed = self._manifest(self.consumer, format='csv',
                                 if_none_match=as_json['ETag'])
        self.assertEqual(crossed.status_code, 200)

        # dataset_version stays format-free: it is what two clients compare to
        # decide whether they hold the same data.
        self.assertIn(as_json.json()['dataset_version'], as_csv['ETag'])

    def test_replacing_a_figure_breaks_the_etag(self):
        """A timestamp cursor cannot see this, which is why the ETag is a hash.

        ``created_at`` is ``auto_now_add``, so re-cropping a figure and saving a
        new file over the same row leaves the timestamp exactly where it was.
        Anything keyed on time therefore reports the dataset as unchanged while
        the bytes behind that URL are a different image — the client keeps the
        old one for good. Hashing the stored object key catches it, because a
        replacement lands on a new key.
        """
        before = self._etag()
        created_before = PublicationImage.objects.get(pk=self.wb.pk).created_at

        replaced = PublicationImage.objects.get(pk=self.wb.pk)
        replaced.image = 'publication_images/2026/ab12345_WB_v2.png'
        replaced.save(update_fields=['image'])

        # The point of the test: the timestamp did not move.
        self.assertEqual(
            PublicationImage.objects.get(pk=self.wb.pk).created_at,
            created_before)

        after = self._etag()
        self.assertNotEqual(after, before)

        # And the client holding the old version is told to re-read, not 304.
        stale = self._manifest(self.consumer, if_none_match=before)
        self.assertEqual(stale.status_code, 200)

    def test_flipping_a_verdict_breaks_the_etag(self):
        """No file changed, and what the manifest says about a product did.

        A recommendation being set changes the verdict this endpoint reports for
        a named commercial product. A consumer sitting on a 304 would go on
        publishing the previous answer, so the flags are in the hash alongside
        the file names.
        """
        before = self._etag()

        antibody = Antibody.objects.get(pk=self.ab_abcam.pk)
        antibody.wb_recommended = True
        antibody.save(update_fields=['wb_recommended'])

        self.assertNotEqual(self._etag(), before)


class TheManifestSaysWhenItIsIncompleteTests(ManifestCase):

    def test_the_default_is_the_whole_set_and_says_so(self):
        """A client diffs this against what it holds, so absence means delete.

        With no parameters the reply must carry every figure in scope and claim
        ``complete: true``; anything less and a client removes files that are
        still in the dataset and has no way to find out.
        """
        response = self._manifest(self.consumer)
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertIs(body['complete'], True)
        self.assertEqual(response['X-OGA-Manifest-Complete'], 'true')
        self.assertEqual(body['counts'], {
            'files_in_scope': 4, 'files_matched': 4, 'files_returned': 4})
        self.assertEqual({row['url'] for row in body['files']},
                         {_url_for(key) for key in self.all_keys})
        self.assertNotIn('truncated', body['sync'])

    def test_a_capped_reply_says_it_is_a_page(self):
        """The cap must never be silent — this is the sharp version of the
        board-pagination rule, because here a missing URL is read as a deletion
        rather than as a row that scrolled off a screen.
        """
        response = self._manifest(self.consumer, limit=1)
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertIs(body['complete'], False)
        self.assertEqual(response['X-OGA-Manifest-Complete'], 'false')
        self.assertEqual(len(body['files']), 1)
        # The count above the list is still the whole scope, not the page.
        self.assertEqual(body['counts']['files_in_scope'], 4)
        self.assertEqual(body['counts']['files_matched'], 4)
        self.assertEqual(body['counts']['files_returned'], 1)

        truncated = body['sync']['truncated']
        self.assertEqual(truncated['limit'], 1)
        self.assertEqual(truncated['offset'], 0)
        self.assertIn('warning', truncated)

    def test_since_says_what_it_cannot_tell_you(self):
        """An incremental reply is additions only, and it has to admit it.

        ``?since=`` can report what arrived. It cannot report a figure that was
        withdrawn — nothing records a deletion — and it cannot report one that
        was replaced in place, because that does not move the timestamp it
        filters on. A client that treated it as a full sync would drift
        permanently, so ``deletions_tracked`` is False and the warning is in the
        body rather than in documentation somebody may not have read.
        """
        now = timezone.now()
        PublicationImage.objects.filter(pk=self.icc.pk).update(
            created_at=now - timedelta(days=400))
        cutoff = now - timedelta(days=200)

        response = self._manifest(self.consumer, since=cutoff.isoformat())
        self.assertEqual(response.status_code, 200)
        body = response.json()

        sync = body['sync']
        self.assertEqual(sync['mode'], 'incremental')
        self.assertIs(sync['deletions_tracked'], False)
        self.assertIn('warning', sync)

        # Only the figures added after the cutoff, and the old one is absent.
        self.assertEqual(
            {row['url'] for row in body['files']},
            {_url_for(self.WB_KEY), _url_for(self.IP_KEY),
             _url_for(self.FC_KEY)})


class TheManifestFormatsTests(ManifestCase):

    def test_csv_urls_and_a_refusal_that_names_the_alternatives(self):
        """Three formats, and an unknown one must not be answered with a guess.

        CSV goes into a spreadsheet, ``urls`` goes into ``wget -i``. A refusal
        that says only "unknown format" leaves a caller with nothing to try; it
        names the three it takes.
        """
        with self.subTest(fmt='csv'):
            response = self._manifest(self.consumer, format='csv')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
            rows = list(csv.reader(response.content.decode().splitlines()))
            self.assertEqual(rows[0], CSV_COLUMNS)
            self.assertEqual(len(rows) - 1, 4)

        with self.subTest(fmt='urls'):
            response = self._manifest(self.consumer, format='urls')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'],
                             'text/plain; charset=utf-8')
            lines = response.content.decode().strip().split('\n')
            self.assertEqual(set(lines),
                             {_url_for(key) for key in self.all_keys})

        with self.subTest(fmt='xlsx'):
            response = self._manifest(self.consumer, format='xlsx')
            self.assertEqual(response.status_code, 400)
            message = response.json()['error']
            for accepted in ('json', 'csv', 'urls'):
                self.assertIn(accepted, message)


class TheManifestScopeTests(ManifestCase):

    def test_the_manifest_is_not_gated_on_a_tier(self):
        """Tiers were a monetisation gate and there is no monetisation.

        Every one of these figures is already served, unauthenticated, from the
        public gene pages — the manifest lists where they are, so refusing it by
        tier withheld an index of public files. A consumer on any tier gets the
        same answer.
        """
        for tier in ('free', 'data', 'intel', 'full'):
            with self.subTest(tier=tier):
                APIConsumer.objects.filter(pk=self.free_tier.pk).update(tier=tier)
                response = self._manifest(self.free_tier)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['files'])

    def test_a_consumers_filters_decide_what_it_can_see(self):
        """The manifest is a second door to rows ``/v1/antibodies/`` already
        gates, and a leak here is a manufacturer downloading a competitor's
        figures — or a demo account holding the whole dataset.
        """
        with self.subTest(filter='supplier'):
            response = self._manifest(self.abcam_only)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body['scope']['supplier_filter'], 'Abcam')
            self.assertEqual({row['url'] for row in body['files']},
                             {_url_for(self.WB_KEY), _url_for(self.IP_KEY)})
            self.assertEqual({row['supplier'] for row in body['files']},
                             {'Abcam'})
            self.assertEqual(body['counts']['files_in_scope'], 2)

        with self.subTest(filter='gene'):
            response = self._manifest(self.snca_only)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body['scope']['genes'], ['SNCA'])
            # Both suppliers' SNCA figures, and none of MAPT's.
            self.assertEqual(
                {row['url'] for row in body['files']},
                {_url_for(self.WB_KEY), _url_for(self.IP_KEY),
                 _url_for(self.FC_KEY)})
            self.assertEqual({row['gene'] for row in body['files']}, {'SNCA'})

    def test_every_consumer_receives_verdicts(self):
        """Withholding these produced a false statement, not a protected one.

        Verdicts used to go only to ``consumer_type == 'manufacturer'``, so a
        registry account received every row with no verdict — and the portal,
        which reads neither ``consumer_type`` nor ``gene_has_recommendations``,
        drew that as "0 recommended" with every badge grey across all 1,645
        antibodies. The same verdicts are on the public gene pages with no gate
        at all, so the rule protected nothing and broke the only screen that
        showed them.
        """
        for consumer in (self.registry, self.consumer, self.free_tier):
            with self.subTest(consumer=consumer.consumer_type):
                body = self._manifest(consumer).json()
                self.assertIs(body['scope']['includes_recommendations'], True)
                # The published schema described `includes_verdicts`, which
                # nothing has ever emitted (7 Aug 2026) — the same failure as
                # the documented `verdict` CSV column. Every key the manifest
                # writes into `scope` must be one the schema names.
                from core.api_schema import document

                documented = (document()['components']['schemas']['Manifest']
                              ['properties']['scope']['properties'])
                self.assertEqual(set(body['scope']) - set(documented), set())
                self.assertTrue(body['files'])
                for row in body['files']:
                    self.assertIn(row['oga_recommendation'],
                                  {R.RECOMMENDED, R.NOT_RECOMMENDED,
                                   R.NOT_TESTED})


class TheFilenameCollisionTests(ManifestCase):

    def test_a_pattern_that_collides_keeps_both_files_and_says_so(self):
        """The browser zip keeps whichever landed last; this must not.

        ``{gene}_{catalogue}`` names one antibody's WB and IP figures
        identically, so a consumer who set that pattern in the portal loses one
        of the two on every antibody with more than one figure — silently, with
        the file count still looking right until somebody opens the folder. The
        suffix keeps both, and the count is reported so the pattern can be
        fixed.
        """
        default = self._manifest(self.consumer).json()
        self.assertNotIn('filename_collisions', default)

        APIConsumer.objects.filter(pk=self.consumer.pk).update(
            portal_config={'filename_pattern': '{gene}_{catalogue}'})

        body = self._manifest(self.consumer).json()
        names = [row['filename'] for row in body['files']]
        self.assertEqual(len(names), 4)
        self.assertEqual(len(set(names)), 4)

        clashing = sorted(row['filename'] for row in body['files']
                          if row['catalogue_number'] == 'ab12345')
        self.assertEqual(len(clashing), 2)
        self.assertNotEqual(clashing[0], clashing[1])
        for name in clashing:
            self.assertTrue(name.startswith('SNCA_ab12345'), name)

        self.assertEqual(body['filename_collisions'], 1)
        self.assertIn('filename_collisions_note', body)


class TheApiIndexTests(ManifestCase):

    def test_the_catalogue_is_readable_without_a_key(self):
        """An API whose endpoint list lives only in a Python file is one every
        consumer has to be told about by email — and the manifest is the newest
        endpoint, so it is the one nobody knows to ask for. It carries no data,
        so it needs no key.
        """
        response = self.client.get(reverse('api:api_index'))
        self.assertEqual(response.status_code, 200)
        body = response.json()

        entries = {entry['path']: entry for entry in body['endpoints']}
        manifest_url = f'{BASE_URL}{reverse("api:manifest")}'
        self.assertIn(manifest_url, entries)
        self.assertEqual(entries[manifest_url]['side_effects'], 'none')

        # The competitor view is the one endpoint with a restriction, and the
        # catalogue has to say so — a machine consumer that discovers an
        # endpoint here and gets a 403 with no warning reads it as a fault.
        detail_url = f'{BASE_URL}{reverse("api:gene_detail")}'
        self.assertIn('manufacturer', entries[detail_url]['available_to'])


class TheGeneNarrowingTests(ManifestCase):
    """`?gene=` on the manifest, added 7 Aug 2026.

    `/download/?gene=` and `/antibodies/?gene=` both existed; the endpoint
    actually built for syncing did not, so a partner mirroring one gene had a
    one-off zip and no incremental route.

    The whole risk is in `complete`. This endpoint's own comment calls a client
    deleting every file a reply did not mention "the single most damaging thing
    this endpoint could get wrong", and a narrowed reply is exactly that shape:
    complete for what was asked, and a small fraction of a full mirror.
    """

    def test_it_returns_only_that_gene(self):
        body = self._manifest(self.consumer, gene='SNCA').json()
        genes = {row['gene'] for row in body['files']}
        self.assertEqual({'SNCA'}, genes)
        self.assertTrue(body['files'], 'the fixture has SNCA figures')

    def test_it_takes_a_list_and_ignores_case(self):
        body = self._manifest(self.consumer, gene='snca,MAPT').json()
        self.assertEqual({'SNCA', 'MAPT'},
                         {row['gene'] for row in body['files']})

    def test_a_narrowed_reply_says_it_is_narrowed(self):
        """The guard. `complete` is true — it IS every SNCA file — so the
        thing that stops a client deleting its MAPT files is this."""
        body = self._manifest(self.consumer, gene='SNCA').json()
        self.assertTrue(body['complete'])
        self.assertTrue(body['sync']['narrowed_by_request'])
        self.assertIn('not because they were withdrawn',
                      body['sync']['narrowed_note'])
        self.assertEqual(['SNCA'], body['scope']['requested_genes'])

    def test_an_unnarrowed_reply_says_it_is_not(self):
        body = self._manifest(self.consumer).json()
        self.assertFalse(body['sync']['narrowed_by_request'])
        self.assertIsNone(body['sync']['narrowed_note'])
        self.assertIsNone(body['scope']['requested_genes'])

    def test_the_etag_distinguishes_narrowings(self):
        """Without this a client alternating between the full manifest and one
        gene is told 304 and goes on holding the wrong set — the same failure
        the format in the ETag prevents."""
        full = self._manifest(self.consumer)['ETag']
        snca = self._manifest(self.consumer, gene='SNCA')['ETag']
        mapt = self._manifest(self.consumer, gene='MAPT')['ETag']
        self.assertNotEqual(full, snca)
        self.assertNotEqual(snca, mapt)

        # And the full manifest's tag must not satisfy a narrowed request.
        again = self._manifest(self.consumer, if_none_match=full, gene='SNCA')
        self.assertEqual(200, again.status_code)

    def test_a_narrowed_etag_still_answers_304(self):
        """The point of the feature: a cheap reconnect on a small mirror."""
        tag = self._manifest(self.consumer, gene='SNCA')['ETag']
        again = self._manifest(self.consumer, if_none_match=tag, gene='SNCA')
        self.assertEqual(304, again.status_code)

    def test_an_unknown_gene_returns_nothing_rather_than_everything(self):
        """A filter that silently fails open is how a narrowed sync becomes a
        full one — and here it would also make every file look withdrawn."""
        body = self._manifest(self.consumer, gene='NOT_A_GENE').json()
        self.assertEqual([], body['files'])

    def test_it_works_in_the_other_formats(self):
        csv_body = self._manifest(self.consumer, gene='SNCA',
                                  format='csv').content.decode()
        self.assertIn('SNCA', csv_body)
        self.assertNotIn('MAPT', csv_body)
