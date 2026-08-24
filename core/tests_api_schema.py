"""The OpenAPI document is checked against the API, not just published.

A specification is a promise about what the code does, and the failure mode is
always the same: the code moves, the document does not, and a consumer builds
against a contract that stopped being true months ago. That is worse than having
no document, because it is believed.

So the parts that can be checked mechanically are:

  * every path in the document resolves to a real Django view, at the URL the
    document says, under the declared server;
  * every operation's security scheme exists in ``components``;
  * every ``$ref`` points at something that is actually defined;
  * the enums equal the constants they were built from, so a new application or
    a renamed verdict cannot be published in one place and not the other.

What is deliberately *not* pinned is prose. Descriptions are for humans and will
drift; a test that asserted them would fail on every wording change and teach
whoever hits it to stop reading this file.
"""
from __future__ import annotations

import json
import re

from django.test import SimpleTestCase, TestCase
from django.urls import Resolver404, resolve, reverse

from core import api_throttle, recommendations as R
from core.api_manifest import CSV_COLUMNS
from core.api_schema import API_VERSION, document
from core.api_views import BASE_URL

SERVER_PATH = '/api/v1'


def _refs(node):
    """Every ``$ref`` string anywhere in the document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == '$ref' and isinstance(value, str):
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


class TheDocumentIsWellFormedTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = document()

    def test_it_is_openapi_3_1_and_serialises(self):
        self.assertEqual(self.doc['openapi'], '3.1.0')
        self.assertEqual(self.doc['info']['version'], API_VERSION)
        json.dumps(self.doc)          # any tool has to be able to read it

    def test_the_server_is_the_live_api(self):
        self.assertEqual(self.doc['servers'][0]['url'], f'{BASE_URL}{SERVER_PATH}')

    def test_every_ref_resolves(self):
        """A dangling $ref breaks Swagger UI and every code generator."""
        for ref in set(_refs(self.doc)):
            with self.subTest(ref=ref):
                self.assertTrue(ref.startswith('#/'), ref)
                node = self.doc
                for part in ref[2:].split('/'):
                    self.assertIn(part, node, f'{ref} does not resolve')
                    node = node[part]

    def test_every_operation_names_a_declared_security_scheme(self):
        declared = set(self.doc['components']['securitySchemes'])
        for path, operations in self.doc['paths'].items():
            for method, operation in operations.items():
                # An empty list is the documented way to say "no key needed".
                for requirement in operation.get('security', [{'ApiKeyAuth': []}]):
                    for scheme in requirement:
                        with self.subTest(path=path, method=method):
                            self.assertIn(scheme, declared)

    def test_operation_ids_are_unique(self):
        """Code generators name their methods from these."""
        ids = [op['operationId']
               for operations in self.doc['paths'].values()
               for op in operations.values() if 'operationId' in op]
        self.assertEqual(sorted(ids), sorted(set(ids)))


class TheDocumentMatchesTheCodeTests(SimpleTestCase):
    """The values a consumer switches on must be the ones the code emits."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = document()

    def test_the_recommendation_enum_is_the_shared_readers(self):
        schema = self.doc['components']['schemas']['Recommendation']
        self.assertCountEqual(
            schema['enum'], [R.RECOMMENDED, R.NOT_RECOMMENDED, R.NOT_TESTED])
        for value in schema['enum']:
            self.assertIn(R.MEANINGS[value], schema['description'])

    def test_the_applications_are_the_shared_readers(self):
        self.assertCountEqual(
            self.doc['components']['schemas']['OgaRecommendations']['properties'],
            R.APPLICATIONS)

    def test_the_rate_limits_are_the_throttles(self):
        rendered = json.dumps(self.doc)
        self.assertIn(str(api_throttle.BURST_LIMIT), rendered)
        self.assertIn(str(api_throttle.SUSTAINED_LIMIT), rendered)

    def test_the_csv_columns_are_the_manifests(self):
        described = self.doc['components']['schemas']['ManifestFile']['description']
        self.assertIn(', '.join(CSV_COLUMNS), described)

    def test_the_manifest_row_schema_is_the_columns_the_api_writes(self):
        """Three times in one file, so it is asked as a set difference now.

        `ManifestFile` documented a `verdict` property and no
        `oga_recommendation`; the `scope` block documented `includes_verdicts`,
        which nothing emits; and `API.md` listed `verdict` as a CSV column.
        All three were the same rename, applied to the code and not to the
        things describing it (7 Aug 2026). The description of this schema is
        built from `CSV_COLUMNS`, so it was already naming the right columns in
        prose while its own properties named a different set.
        """
        from core.api_manifest import CSV_COLUMNS

        properties = self.doc['components']['schemas']['ManifestFile']['properties']
        self.assertEqual(set(properties), set(CSV_COLUMNS))

    def test_the_recommendation_carries_the_scope_note(self):
        """The one sentence in here that protects a named commercial product.

        Every other description is prose this file deliberately does not pin.
        It used to pin "not a negative result", the paragraph the owner cut on
        7 Aug 2026; what a consumer is owed is which protocols the result came
        from, that performance is protocol and sample dependent, and that the
        result neither validates nor invalidates another assay system. Asserted
        against the constant, so the schema cannot drift from the pages.
        """
        from core import recommendations as R

        schema = self.doc['components']['schemas']['Recommendation']
        self.assertIn(R.SCOPE_NOTE, schema['description'].replace('**', ''))


class EveryDocumentedPathExistsTests(TestCase):
    """A documented endpoint that 404s is worse than an undocumented one."""

    databases = {'pipeline_db', 'academy_db'}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = document()

    def test_every_path_resolves_to_a_view(self):
        for path in self.doc['paths']:
            url = f'{SERVER_PATH}{path}' if path != '/' else f'{SERVER_PATH}/'
            with self.subTest(path=path):
                try:
                    resolve(url)
                except Resolver404:
                    self.fail(f'{url} is documented and routed nowhere.')

    def test_the_methods_documented_are_the_methods_allowed(self):
        """A GET documented on a POST-only endpoint is a 405 for the reader."""
        for path, operations in self.doc['paths'].items():
            url = f'{SERVER_PATH}{path}' if path != '/' else f'{SERVER_PATH}/'
            for method in operations:
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertNotEqual(
                        response.status_code, 405,
                        f'{method.upper()} {url} is documented and not allowed.')

    def test_it_is_served_without_a_key(self):
        response = self.client.get(reverse('api:openapi'))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['openapi'], '3.1.0')
        # Open to any origin: a generator or a docs viewer runs in a browser.
        self.assertEqual(response['Access-Control-Allow-Origin'], '*')

    def test_the_catalogue_points_at_it(self):
        """An endpoint nothing links to is one nobody finds."""
        catalogue = self.client.get(reverse('api:api_index')).json()
        self.assertTrue(catalogue['openapi'].endswith('/api/v1/openapi.json'))
        self.assertIn('openapi.json',
                      ' '.join(e['path'] for e in catalogue['endpoints']))


class TheReadmeAndTheSpecAgreeTests(SimpleTestCase):
    """API.md is the narrative; the spec is the contract. Both are published."""

    def test_the_markdown_points_at_the_spec(self):
        from pathlib import Path

        from django.conf import settings

        text = Path(settings.BASE_DIR, 'API.md').read_text()
        self.assertIn('openapi.json', text,
                      'API.md does not mention the machine-readable spec, so a '
                      'reader has no way to find it.')

    def test_the_markdown_uses_the_key_placeholder_throughout(self):
        from pathlib import Path

        from django.conf import settings

        text = Path(settings.BASE_DIR, 'API.md').read_text()
        self.assertIn('YOUR_API_KEY_HERE', text)
        # No real UUID-shaped key left in an example by accident.
        leaked = re.findall(
            r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
            text)
        self.assertEqual(leaked, [], f'A real-looking key is in API.md: {leaked}')
