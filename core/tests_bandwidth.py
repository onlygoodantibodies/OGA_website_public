"""What leaves this server, and to whom.

Both rules here fail *silently* if they regress, which is the only reason they
are worth a test. Drop `GZipMiddleware` out of the list in a merge and every
page still renders perfectly, four times heavier, with nothing on any screen to
say so. Mistype a name in the crawler list and the site keeps working while
quietly asking Google to leave.

Context, Aug 2026: bandwidth on the Hobby plan ran to ~70% of 5 GB by the 20th,
and sampling the request log showed roughly nine in ten requests were crawlers
walking 40-100 KB gene pages that nothing was compressing.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase


class PublicHtmlIsCompressedTests(TestCase):
    """A page a crawler can reach must not go out uncompressed."""

    def test_a_public_page_is_compressed_for_a_client_that_asks(self):
        page = self.client.get('/privacy-policy/')
        self.assertEqual(page.status_code, 200)

        zipped = self.client.get(
            '/privacy-policy/', headers={'accept-encoding': 'gzip'})
        self.assertEqual(zipped['Content-Encoding'], 'gzip')
        # Markup of this shape compresses several times over. Asserted loosely:
        # the number that matters is "much smaller", not a particular ratio.
        self.assertLess(len(zipped.content), len(page.content) / 2)

    def test_a_client_that_does_not_ask_still_gets_a_readable_page(self):
        """The old behaviour has to survive, or a plain client gets bytes it
        cannot read. Django decides this on `Accept-Encoding`; the test exists
        because the failure would be invisible from a browser, which always
        asks."""
        page = self.client.get('/privacy-policy/', headers={'accept-encoding': ''})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('Content-Encoding', page.headers)


class RobotsTxtTests(TestCase):
    """Who is turned away, and — the half that would hurt — who is not."""

    def setUp(self):
        self.body = self.client.get('/robots.txt').content.decode()

    def _blocked(self, agent):
        """True if `agent` has its own group, and that group disallows the site."""
        groups = self.body.split('User-agent: ')
        for group in groups[1:]:
            if group.splitlines()[0].strip() == agent:
                return 'Disallow: /\n' in group or group.rstrip().endswith('Disallow: /')
        return False

    def test_the_crawlers_that_bring_readers_are_not_blocked(self):
        """The expensive mistake. A site nobody can find costs more than the
        bandwidth ever did, and the AI crawlers are how this data reaches a
        model — which is the same job `mcp_servers/` exists to do."""
        for agent in ('Googlebot', 'Bingbot', 'DuckDuckBot',
                      'ClaudeBot', 'GPTBot', 'ChatGPT-User', 'PerplexityBot'):
            with self.subTest(agent=agent):
                self.assertFalse(
                    self._blocked(agent),
                    f'{agent} is asked not to crawl; it should be welcome.')

    def test_the_freeloading_crawlers_are_turned_away(self):
        from core.views import FREELOADING_CRAWLERS
        for agent in FREELOADING_CRAWLERS:
            with self.subTest(agent=agent):
                self.assertTrue(self._blocked(agent))

    def test_the_lab_tool_and_its_assets_are_not_offered_to_anyone(self):
        """`/pipeline/` answers an anonymous request with a redirect to sign in,
        and `/static/pipeline/` is 18 MB of Tesseract nobody crawling this site
        has any use for."""
        for path in ('/pipeline/', '/static/pipeline/'):
            self.assertIn(f'Disallow: {path}', self.body)

    def test_the_sitemap_is_still_announced(self):
        """Everything above is subtraction; this is the line that gets the gene
        pages read in the first place."""
        self.assertIn('Sitemap: https://onlygoodantibodies.co.uk/sitemap.xml',
                      self.body)


class PublicCacheHeadersTests(TestCase):
    """Which replies are offered to a shared cache, and — the half that matters
    — which are refused. See OGA_website/cache_headers.py for the reasoning."""

    # Signing in reads the login, which lives in academy_db, not `default`.
    databases = {'default', 'academy_db'}

    def test_an_anonymous_public_page_may_be_cached(self):
        cc = self.client.get('/champions/')['Cache-Control']
        self.assertIn('public', cc)
        self.assertIn('s-maxage=', cc)

    def test_a_page_that_sets_a_cookie_is_refused(self):
        """`/contact/` carries a CSRF form, so it sets `csrftoken`. This is the
        refusal that means the safe set needs no hand-maintained list — a new
        page with a form drops out on its own."""
        page = self.client.get('/contact/')
        self.assertTrue(page.cookies, 'expected /contact/ to set a cookie')
        self.assertNotIn('Cache-Control', page.headers)

    def test_a_signed_in_reader_is_never_the_cached_copy(self):
        User = get_user_model()
        User.objects.create_user(username='member', password='pw')
        self.client.login(username='member', password='pw')
        self.assertNotIn('Cache-Control', self.client.get('/champions/').headers)

    def test_a_member_area_is_refused_by_name(self):
        """Belt and braces over the cookie rule: a page under these prefixes must
        not become cacheable just because it happened not to set a cookie."""
        from OGA_website.cache_headers import NEVER_CACHED_PREFIXES
        for prefix in ('/pipeline/', '/academy/', '/accounts/', '/api/'):
            self.assertTrue(prefix.startswith(NEVER_CACHED_PREFIXES) or
                            prefix in NEVER_CACHED_PREFIXES, prefix)

    def test_a_reply_that_already_decided_is_left_alone(self):
        """The extension snapshot sets its own day-long TTL and the API decides
        public-vs-private per key. Overriding either would be this module
        second-guessing the one reader that knows."""
        cc = self.client.get('/extension/citations.json')['Cache-Control']
        self.assertNotIn('s-maxage', cc)

    def test_a_missing_page_is_not_cached(self):
        """A 404 held for an hour is a page that stays missing after it is fixed."""
        self.assertNotIn('Cache-Control',
                         self.client.get('/no-such-page-here/').headers)

    def test_a_cacheable_page_does_not_vary_on_cookie(self):
        """The header that silently cancelled this whole module.

        Every public page renders the Academy link, which reads `request.user`,
        so Django marks the response `Vary: Cookie`. Cloudflare declines to store
        anything varying on more than `Accept-Encoding` — so the page went out
        asking to be cached (`s-maxage=3600`) and came back
        `cf-cache-status: DYNAMIC`, every time, with nothing saying so. Measured
        on live 23 Aug 2026; adding a Cloudflare Cache Rule changed nothing until
        this came off.
        """
        page = self.client.get('/champions/')
        self.assertIn('s-maxage=', page['Cache-Control'])
        vary = page.get('Vary', '')
        self.assertNotIn('cookie', vary.lower(), f'Vary is {vary!r}')

    def test_the_encoding_half_of_vary_survives(self):
        """`Accept-Encoding` is the one value Cloudflare does honour, and GZip
        needs it — dropping it would hand a gzipped body to a client that cannot
        read one."""
        page = self.client.get('/champions/', headers={'accept-encoding': 'gzip'})
        self.assertIn('accept-encoding', page.get('Vary', '').lower())

    def test_a_refused_page_keeps_its_vary_untouched(self):
        """The cookie is only dropped where `_may_be_cached` already cleared the
        reply. A page that was refused must keep varying, or it would be shared
        by a cache that was never told it could be."""
        page = self.client.get('/contact/')
        self.assertNotIn('Cache-Control', page.headers)
        self.assertIn('cookie', page.get('Vary', '').lower())


class RevalidationTests(TestCase):
    """"You already have this one" — the reply that costs nothing.

    `ConditionalGetMiddleware` fingerprints every reply so a cache holding a
    copy can be told it is still good, in a few bytes, instead of being handed
    the page again. Django sets no ETag on its own, so before this there was
    nothing on the site that could answer that question.
    """

    def test_a_cache_that_already_has_the_page_is_told_so(self):
        first = self.client.get('/privacy-policy/')
        self.assertEqual(first.status_code, 200)
        self.assertIn('ETag', first.headers)

        again = self.client.get(
            '/privacy-policy/', headers={'if-none-match': first['ETag']})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b'')

    def test_the_304_still_says_how_long_it_may_be_kept(self):
        """Cloudflare re-derives freshness from the revalidation's own reply, so
        a 304 stripped of `Cache-Control` leaves the edge nothing to go on. This
        is the whole reason ConditionalGetMiddleware sits ABOVE the header
        middleware in the list rather than below it."""
        etag = self.client.get('/privacy-policy/')['ETag']
        again = self.client.get(
            '/privacy-policy/', headers={'if-none-match': etag})
        self.assertIn('s-maxage', again['Cache-Control'])


class ExtensionSnapshotRevalidationTests(TestCase):
    """The extension's daily download, against data that changes monthly.

    Every install fetches `index.json` once a day whether or not anything has
    moved. Two things make that nearly free, and BOTH fail silently: the file
    has to be byte-identical while the data is unchanged, or the fingerprint
    moves and every cache in the chain re-fetches a megabyte to be told what it
    already knew; and a revalidation has to be answered 304.

    The first is the one that shipped broken: `generated` was `timezone.now()`,
    so the bytes differed on every rebuild — hourly — on a dataset nobody had
    touched. Nothing on any screen would have said so.
    """
    databases = {'default', 'pipeline_db', 'academy_db'}

    def setUp(self):
        import datetime

        from django.core.cache import cache
        from pipeline.models import Antibody, Company, PublicationImage, Target

        cache.clear()
        target = Target.objects.create(protein_name='Synuclein', gene_name='SNCA')
        antibody = Antibody.objects.create(
            target=target,
            company=Company.objects.create(name='Proteintech'),
            catalogue_number='10842-1-AP',
            rrid='AB_2178511')
        figure = PublicationImage.objects.create(
            antibody=antibody, application_type='WB', image='pubs/snca_wb.png')
        # auto_now_add refuses an assignment; UPDATE is how a fixture dates a row.
        self.published_at = datetime.datetime(
            2026, 7, 15, 9, 30, tzinfo=datetime.timezone.utc)
        PublicationImage.objects.filter(pk=figure.pk).update(
            created_at=self.published_at)

    def test_the_stamp_is_the_data_s_date_not_the_clock(self):
        from core.extension_index import build_index
        self.assertEqual(build_index()['generated'], '2026-07-15T09:30:00Z')

    def test_rebuilding_an_unchanged_dataset_gives_the_same_fingerprint(self):
        from django.core.cache import cache

        first = self.client.get('/extension/index.json')
        self.assertEqual(first.status_code, 200)
        # Drop the server-side copy, which is what an expiry does an hour later.
        # The rebuild reads the same rows, so it must produce the same bytes.
        cache.clear()
        second = self.client.get('/extension/index.json')
        self.assertEqual(
            second['ETag'], first['ETag'],
            'the snapshot changed while the data did not, so every install '
            'downloads it again')

    def test_an_install_that_already_has_the_snapshot_is_told_so(self):
        etag = self.client.get('/extension/index.json')['ETag']
        again = self.client.get(
            '/extension/index.json', headers={'if-none-match': etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b'')
