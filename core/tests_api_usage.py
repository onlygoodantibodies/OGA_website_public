"""The API usage counter — that it counts, and that it cannot break a request.

Both halves matter and the second is the one that would be silent. A counter
that stops counting is a gap in a graph nobody is watching yet; a counter that
raises takes down the endpoint it was measuring, in the database that also holds
the site's logins.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from core.models import APIConsumer, ApiUsageDay
from pipeline.models import Antibody, Company, PublicationImage, Target


class TheUsageCounterTests(TestCase):
    databases = {'pipeline_db', 'academy_db'}

    @classmethod
    def setUpTestData(cls):
        cls.consumer = APIConsumer.objects.create(
            name='Test Registry', consumer_type='rrid', is_active=True)
        company = Company.objects.create(name='Abcam', display_name='Abcam')
        target = Target.objects.create(gene_name='SNCA', protein_name='Syn')
        antibody = Antibody.objects.create(
            target=target, company=company, catalogue_number='ab138501',
            wb_recommended=True)
        PublicationImage.objects.create(
            antibody=antibody, application_type='WB', image='pubs/x.png')

    def _call(self, key=None):
        headers = {'HTTP_X_API_KEY': str(key)} if key else {}
        return self.client.get(reverse('api:genes_feed'), **headers)

    def test_a_keyed_request_is_counted_against_its_consumer(self):
        self._call(self.consumer.api_key)
        row = ApiUsageDay.objects.get(consumer=self.consumer)
        self.assertEqual(row.endpoint, 'genes_feed')
        self.assertEqual(row.count, 1)

    def test_counting_increments_one_row_rather_than_adding_rows(self):
        # The row count is bounded by consumers x endpoints x days whatever the
        # traffic does. A row per request would let a crawler set the size of a
        # table in the database that holds the logins.
        for _ in range(5):
            self._call(self.consumer.api_key)
        self.assertEqual(ApiUsageDay.objects.count(), 1)
        self.assertEqual(ApiUsageDay.objects.get().count, 5)

    def test_a_keyless_request_is_counted_without_naming_anybody(self):
        # This was a 401 until 12 Aug 2026, and the counting is what it was:
        # "the same row keeps counting them if keyless requests are ever
        # allowed to succeed" is what the previous version of this comment
        # said, and they now are. The baseline it was collecting is what makes
        # the effect of opening it up measurable rather than guessed at.
        response = self._call()
        self.assertEqual(response.status_code, 200)
        row = ApiUsageDay.objects.get(consumer__isnull=True)
        self.assertEqual(row.endpoint, 'genes_feed')
        self.assertEqual(row.count, 1)

    def test_keyed_and_keyless_counts_do_not_share_a_row(self):
        self._call(self.consumer.api_key)
        self._call()
        self.assertEqual(ApiUsageDay.objects.count(), 2)
        self.assertEqual(
            ApiUsageDay.objects.get(consumer=self.consumer).count, 1)
        self.assertEqual(
            ApiUsageDay.objects.get(consumer__isnull=True).count, 1)

    def test_endpoints_are_counted_apart(self):
        self._call(self.consumer.api_key)
        self.client.get(reverse('api:api_status'),
                        HTTP_X_API_KEY=str(self.consumer.api_key))
        self.assertEqual(
            set(ApiUsageDay.objects.values_list('endpoint', flat=True)),
            {'genes_feed', 'api_status'})

    def test_a_failing_counter_does_not_fail_the_request(self):
        """The half that would be silent, and the reason it is swallowed.

        `academy_db` is SQLite on a Render disk and also holds the logins, so a
        write that contends must not be able to turn telemetry into a refused
        request. Patching the model manager rather than `record` itself, so the
        `except DatabaseError` in `api_usage` is the thing under test — a mock
        on `record` would prove only that the caller was wrapped.
        """
        from django.db import DatabaseError

        with mock.patch('core.api_usage.ApiUsageDay.objects') as manager:
            manager.filter.side_effect = DatabaseError('database is locked')
            response = self._call(self.consumer.api_key)

        self.assertEqual(response.status_code, 200)
        self.assertIn('genes', response.json())
        self.assertEqual(ApiUsageDay.objects.count(), 0)


class AKeylessRowIsARefusalUnlessTheEndpointServesOneTests(TestCase):
    """`api_usage.SERVED_WITHOUT_KEY` asked of the endpoints themselves.

    `_guard` counts a keyless request and then refuses it, so the row a refusal
    leaves is identical to the row a served read leaves. Which is which is only
    knowable from which endpoint it was, and that list is a copy of a decision
    made in the views. This is what keeps the copy true: opening a third
    endpoint to keyless callers fails here, rather than silently turning its
    past refusals into reads on the impact page and in its download.
    """
    databases = {'pipeline_db', 'academy_db'}

    def setUp(self):
        # Fifteen keyless requests from one address is past the anonymous
        # ceiling, which lives in the cache and outlasts the test — every
        # keyless test after this one would be answered 429.
        cache.clear()
        self.addCleanup(cache.clear)

    def _ask_without_a_key(self, name):
        url = reverse(f'api:{name}')
        for method in ('get', 'post', 'put', 'delete'):
            response = getattr(self.client, method)(url)
            if response.status_code != 405:
                return response
        self.fail(f'{name} answered 405 to every method')

    def test_only_the_endpoints_listed_serve_a_request_with_no_key(self):
        from core import api_urls
        from core.api_usage import SERVED_WITHOUT_KEY, refused_without_key_q
        from pipeline.services.consumer_activity import UNCOUNTED

        refused = set()
        for pattern in api_urls.urlpatterns:
            if not pattern.name or pattern.name in UNCOUNTED:
                continue
            status = self._ask_without_a_key(pattern.name).status_code
            if pattern.name in SERVED_WITHOUT_KEY:
                self.assertEqual(status, 200, pattern.name)
            else:
                self.assertEqual(status, 401, pattern.name)
                refused.add(pattern.name)

        # Every refusal was still counted, and the reader picks out exactly
        # those rows — not the served ones beside them.
        self.assertEqual(
            set(ApiUsageDay.objects.filter(refused_without_key_q())
                .values_list('endpoint', flat=True)),
            refused)
        self.assertEqual(
            set(ApiUsageDay.objects.exclude(refused_without_key_q())
                .values_list('endpoint', flat=True)),
            set(SERVED_WITHOUT_KEY))


class ThePortalIsCountedApartFromTheApiTests(TestCase):
    """The portal is an API client, so its clicks are API requests.

    Silent if wrong in either direction: a portal click recorded as a script
    reads as a manufacturer integrating with the API (nine organisations on
    13 Oct 2026, where one had), and a script recorded as the portal hides the
    integration an impact case most wants to quote.
    """
    databases = {'pipeline_db', 'academy_db'}

    @classmethod
    def setUpTestData(cls):
        cls.consumer = APIConsumer.objects.create(
            name='Test Registry', consumer_type='rrid', is_active=True)

    def _status(self, key=True, **headers):
        if key:
            headers['HTTP_X_API_KEY'] = str(self.consumer.api_key)
        return self.client.get(reverse('api:api_status'), **headers)

    def test_the_portals_label_is_recorded(self):
        self._status(HTTP_X_OGA_CLIENT='portal')
        self.assertIs(ApiUsageDay.objects.get().via_portal, True)

    def test_a_request_without_it_is_recorded_as_direct(self):
        self._status()
        self.assertIs(ApiUsageDay.objects.get().via_portal, False)

    def test_a_same_origin_fetch_carrying_a_key_is_the_portal(self):
        """A portal tab opened before the label shipped sends no label."""
        self._status(HTTP_SEC_FETCH_SITE='same-origin')
        self.assertIs(ApiUsageDay.objects.get().via_portal, True)

    def test_a_link_followed_from_our_own_page_is_not_the_portal(self):
        """Same origin, but no key: a link cannot carry one, the portal always
        does. Without this, reading the open feed from our own docs page would
        count as portal use."""
        self.client.get(reverse('api:genes_feed'),
                        HTTP_SEC_FETCH_SITE='same-origin')
        self.assertIs(ApiUsageDay.objects.get().via_portal, False)

    def test_the_portal_and_a_script_on_one_day_are_two_rows(self):
        """`via_portal` is in the unique key, or the second request of the day
        lands on the first's row and takes its label."""
        self._status(HTTP_X_OGA_CLIENT='portal')
        self._status()
        self._status()
        self.assertEqual(
            dict(ApiUsageDay.objects.values_list('via_portal', 'count')),
            {True: 1, False: 2})

    def test_every_keyed_fetch_in_the_portal_sends_the_label(self):
        """The label is set in one helper; a fetch writing its own headers
        would silently go back to being counted as a script."""
        from pathlib import Path

        from django.conf import settings

        page = (Path(settings.BASE_DIR) / 'core' / 'templates' / 'core'
                / 'portal.html').read_text()
        self.assertEqual(page.count("'X-API-Key'"), 1,
                         "A fetch builds its own key header instead of "
                         "calling keyHeaders(), so it sends no portal label.")
        helper = next(line for line in page.splitlines()
                      if line.startswith('function keyHeaders('))
        self.assertIn("'X-API-Key'", helper)
        self.assertIn("'X-OGA-Client': 'portal'", helper)


class AnOldRowIsPlacedByItsDayTests(TestCase):
    """`route_of` for the rows written before the portal labelled itself."""
    databases = {'academy_db'}

    @classmethod
    def setUpTestData(cls):
        cls.consumer = APIConsumer.objects.create(
            name='Abcam', consumer_type='manufacturer', is_active=True)

    def _row(self, endpoint, day, consumer='ours', count=1, via_portal=None):
        from datetime import date

        return ApiUsageDay.objects.create(
            consumer=self.consumer if consumer == 'ours' else consumer,
            date=date(2026, 9, day), endpoint=endpoint, count=count,
            via_portal=via_portal)

    def _routes(self):
        from core import api_usage

        return {(r.date.day, r.endpoint, r.via_portal): r.route
                for r in api_usage.with_route(ApiUsageDay.objects.all())}

    def test_a_day_they_signed_in_is_the_portals(self):
        self._row('api_status', 1)
        self._row('antibodies_feed', 1)
        self._row('gene_detail', 1, count=12)
        self.assertEqual(set(self._routes().values()), {'portal'})

    def test_the_feed_with_no_sign_in_was_not_the_portal(self):
        """The portal fetches the feed only straight after signing in."""
        self._row('antibodies_feed', 2)
        self._row('gene_detail', 2, count=6)
        self.assertEqual(set(self._routes().values()), {'direct'})

    def test_only_what_an_open_tab_does_cannot_be_placed(self):
        """A tab left open overnight and a script look identical here."""
        self._row('api_status', 1)
        self._row('reviewed_antibodies', 3)
        self._row('gene_detail', 3)
        routes = self._routes()
        self.assertEqual(routes[(3, 'reviewed_antibodies', None)], 'unplaced')
        self.assertEqual(routes[(3, 'gene_detail', None)], 'unplaced')

    def test_no_tab_is_open_before_they_have_ever_signed_in(self):
        self._row('reviewed_antibodies', 3)
        self._row('api_status', 4)
        self.assertEqual(self._routes()[(3, 'reviewed_antibodies', None)],
                         'direct')

    def test_a_labelled_row_keeps_its_label_on_a_sign_in_day(self):
        self._row('api_status', 4)
        self._row('antibodies_feed', 4, via_portal=False)
        routes = self._routes()
        self.assertEqual(routes[(4, 'api_status', None)], 'portal')
        self.assertEqual(routes[(4, 'antibodies_feed', False)], 'direct')

    def test_another_organisations_sign_in_does_not_place_this_one(self):
        other = APIConsumer.objects.create(name='Other',
                                           consumer_type='manufacturer')
        self._row('api_status', 1)
        self._row('api_status', 5, consumer=other)
        self._row('reviewed_antibodies', 5)
        self.assertEqual(self._routes()[(5, 'reviewed_antibodies', None)],
                         'unplaced')

    def test_a_keyless_row_has_no_sign_in_day(self):
        """A served feed is direct; a refused call could have been a portal
        sign-in with a mistyped key, or anything."""
        self._row('antibodies_feed', 6, consumer=None)
        self._row('api_status', 6, consumer=None)
        routes = self._routes()
        self.assertEqual(routes[(6, 'antibodies_feed', None)], 'direct')
        self.assertEqual(routes[(6, 'api_status', None)], 'unplaced')
