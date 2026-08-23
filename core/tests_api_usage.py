"""The API usage counter — that it counts, and that it cannot break a request.

Both halves matter and the second is the one that would be silent. A counter
that stops counting is a gap in a graph nobody is watching yet; a counter that
raises takes down the endpoint it was measuring, in the database that also holds
the site's logins.
"""
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import APIConsumer, ApiUsageDay
from pipeline.models import Antibody, Company, PublicationImage, Target


class TheUsageCounterTests(TestCase):
    databases = {'default', 'pipeline_db', 'academy_db'}

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
