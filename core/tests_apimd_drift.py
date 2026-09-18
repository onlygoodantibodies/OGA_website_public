"""`API.md`'s examples, checked against what the API actually returns.

The reference is prose written by hand beside an API generated from code, so it
drifts, and it drifted three times in one afternoon (7 Aug 2026): a `verdict`
CSV column, an `includes_verdicts` scope flag and a `verdict` property on the
manifest row, none of which anything emitted. Each would have had a partner
write a parser for a field that never arrives.

**Only the phantom direction fails.** A key in an example that the API does not
send is a defect — somebody builds on it. A key the API sends that the example
omits is an incomplete illustration, which is what an example is; failing on
that would make every response field mandatory in the document and the file
would stop being readable. Both directions are reported, one is asserted.

`tests_api_schema.py` does the same job for the OpenAPI document, which is
generated and therefore only drifts in prose. This file is for the one written
by hand.
"""
import json
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from core.models import APIConsumer
from pipeline.models import Antibody, Company, PublicationImage, Target
from pipeline.services import review

#: Objects whose keys are data, not contract — a supplier name, a gene. An
#: example naming one our fixture does not have is not a phantom field.
#:
#: ``oga_qualifiers`` is the same shape for a different reason: it carries only
#: the applications whose capability axis the bench actually judged, so which
#: keys appear is a fact about the data rather than about the contract. The
#: example documents a qualified IP because that is what the field is FOR, and a
#: fixture with no judged axis must not make documenting it an error.
DATA_KEYED = {'supplier_summary', 'recommendations_by_application',
              'oga_qualifiers'}


def documented_examples():
    """Every ```json block in API.md, keyed by the heading above it."""
    md = Path(settings.BASE_DIR, 'API.md').read_text(encoding='utf-8')
    out, heading = {}, ''
    for chunk in re.split(r'\n(?=#{2,3} )', md):
        first = chunk.splitlines()[0]
        if first.startswith('#'):
            heading = first.strip('# ').strip()
        for block in re.findall(r'```json\n(.*?)```', chunk, re.S):
            # An example nobody can paste is not an example, so an unparseable
            # block raises here rather than being skipped.
            out.setdefault(heading, []).append(json.loads(block))
    return out


class TheExamplesMatchTheApiTests(TestCase):
    databases = {'pipeline_db', 'academy_db'}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name='Abcam', display_name='Abcam')
        target = Target.objects.create(gene_name='SNCA', protein_name='Syn')
        antibody = Antibody.objects.create(
            target=target, company=company, catalogue_number='ab138501',
            rrid='AB_2687467', wb_recommended=True)
        PublicationImage.objects.create(
            antibody=antibody, application_type='WB', image='pubs/x.png')
        cls.key = str(APIConsumer.objects.create(
            name='doc-audit', consumer_type='manufacturer',
            is_active=True).api_key)

        # The pre-release endpoints (§10b) need two things this fixture would
        # not otherwise have: a key that carries a **supplier scope**, since an
        # unscoped one is refused by design, and a staged figure, since `_walk`
        # skips an empty list and would then check none of the row's fields —
        # which is the shape of a test that silently checks nothing.
        cls.staged = review.stage(
            antibody=antibody, application_type='IP', content=b'not-a-png',
            filename='SNCA_ab138501_IP.png', recommended=True)
        cls.scoped_key = str(APIConsumer.objects.create(
            name='doc-audit-scoped', consumer_type='manufacturer',
            supplier_filter='Abcam', is_active=True).api_key)

    def _get(self, path, key=None, **params):
        return self.client.get(path, params,
                               HTTP_X_API_KEY=key or self.key).json()

    def _walk(self, doc, real, path, phantom, undocumented):
        if isinstance(doc, dict) and isinstance(real, dict):
            here = path.split('.')[-1].split('[')[0] if path else ''
            if here not in DATA_KEYED:
                for key in sorted(set(doc) - set(real)):
                    phantom.append(f'{path}.{key}'.lstrip('.'))
                for key in sorted(set(real) - set(doc)):
                    undocumented.append(f'{path}.{key}'.lstrip('.'))
            for key in set(doc) & set(real):
                self._walk(doc[key], real[key],
                           f'{path}.{key}'.lstrip('.'), phantom, undocumented)
        elif isinstance(doc, list) and isinstance(real, list) and doc and real:
            self._walk(doc[0], real[0], path + '[0]', phantom, undocumented)

    def test_no_example_names_a_field_the_api_does_not_send(self):
        examples = documented_examples()
        cases = [
            ('The whole manifest', '/api/v1/manifest/', {}, None),
            ('6. `GET /antibodies/` — antibody records',
             '/api/v1/antibodies/', {'preview': 'true'}, None),
            ('7. `GET /genes/` — the gene catalogue', '/api/v1/genes/', {}, None),
            ('8. `GET /gene-detail/` — the competitor view',
             '/api/v1/gene-detail/', {'gene': 'SNCA'}, None),
            ('9. `GET /status/` — your account', '/api/v1/status/', {}, None),
            ('`GET /pipeline-data/` — your figures awaiting release',
             '/api/v1/pipeline-data/', {}, 'scoped'),
            ('`GET /gene-progress/` — where your genes have got to',
             '/api/v1/gene-progress/', {}, 'scoped'),
        ]
        for heading, path, params, scope in cases:
            with self.subTest(endpoint=path):
                self.assertIn(
                    heading, examples,
                    f'No JSON example under "{heading}" — has the heading been '
                    f'renamed? This test then silently checks nothing.')
                phantom, undocumented = [], []
                key = self.scoped_key if scope == 'scoped' else None
                self._walk(examples[heading][0], self._get(path, key=key, **params),
                           '', phantom, undocumented)
                if undocumented:  # reported, never failed — see the docstring
                    print(f'\n  note: {path} sends fields the example omits: '
                          f'{undocumented}')
                self.assertEqual(
                    [], phantom,
                    f'API.md documents {phantom} for {path}, and the API does '
                    f'not send it. A reader will build against it.')

    def test_the_sync_client_reads_headers_case_insensitively(self):
        """The reference implementation partners are told to copy.

        It did `dict(response.headers)` and then `.get("ETag")`. HTTP/2
        lowercases every header name, so behind a CDN the lookup returns None,
        the state file stores no etag, `If-None-Match` is never sent, and the
        client refetches the whole manifest for ever — silently, and while
        looking like it is syncing incrementally. The same line did it to
        `Retry-After`, so a rate-limited client printed `Retry in Nones`.

        Found 7 Aug 2026 after making the identical mistake in
        `bin/check_api.py`, where a live run exposed it. Nothing here can run
        the sample against a proxy, so this pins the shape instead: the object
        `urlopen` returns has a case-insensitive `.get()`, and a `dict` of it
        does not.
        """
        import ast

        md = Path(settings.BASE_DIR, 'API.md').read_text(encoding='utf-8')
        section = md.split('## 11. A complete sync client')[1].split('\n## ')[0]
        blocks = re.findall(r'```python\n(.*?)```', section, re.S)
        self.assertTrue(blocks, 'the sync client has moved out of section 11')
        code = blocks[0]

        ast.parse(code)  # an example nobody can run is not an example

        offenders = [line.strip() for line in code.splitlines()
                     if re.search(r'dict\(\s*\w+\.headers', line)]
        self.assertEqual(
            [], offenders,
            'The sync client converts headers to a dict, which loses '
            'case-insensitive lookup and silently breaks conditional '
            f'requests behind any HTTP/2 proxy: {offenders}')

    def test_the_sync_client_repairs_a_mirror_that_lost_files(self):
        """A 304 says the dataset is unchanged, not that the mirror is intact.

        The client returned on 304 without looking at what it held, so deleting
        a file from a synced directory and running again printed "Nothing has
        changed" — for ever. A partner losing files to a half-finished copy or
        a cleared directory would have had a sync reporting success while the
        images their pages point at were gone. Found 7 Aug 2026 by deleting two
        files and re-running, which is how it was meant to be demonstrated.

        This runs the real extracted client with the network stubbed, because
        the bug was in its control flow: no assertion about the source text
        would have caught it, and the two shape tests beside this one did not.
        """
        import importlib.util
        import io
        import json
        import tempfile
        import urllib.request
        from unittest import mock

        md = Path(settings.BASE_DIR, 'API.md').read_text(encoding='utf-8')
        code = re.findall(r'```python\n(.*?)```', md, re.S)[0]

        files = {f'ACE_ab{n}_WB.png': f'https://cdn.example/{n}.png'
                 for n in (1, 2, 3)}
        manifest = json.dumps({
            'complete': True,
            'files': [{'filename': k, 'url': v} for k, v in files.items()],
        }).encode()

        class Headers(dict):
            """Lowercased, the way HTTP/2 delivers them."""

            def get(self, key, default=None):
                return dict.get(self, key.lower(), default)

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'sync.py')
            path.write_text(code)
            spec = importlib.util.spec_from_file_location('oga_sync', path)
            client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(client)
            client.DEST = Path(tmp, 'images')
            client.STATE = client.DEST / '.sync_state.json'

            status = {'code': 200}
            client.get = lambda url, headers: (
                (304, b'', Headers({'etag': '"t"'})) if status['code'] == 304
                else (200, manifest, Headers({'etag': '"t"'})))

            with mock.patch.object(urllib.request, 'urlopen',
                                   lambda *a, **k: Response(b'PNG')):
                self.assertEqual(0, client.main())
                self.assertEqual(3, len(list(client.DEST.glob('*.png'))))

                status['code'] = 304
                self.assertEqual(0, client.main())

                lost = client.DEST / 'ACE_ab2_WB.png'
                lost.unlink()
                self.assertEqual(0, client.main())
                self.assertTrue(
                    lost.exists(),
                    'a 304 left a deleted file missing — the client is treating '
                    '"the dataset is unchanged" as "my copy of it is fine"')

    def test_every_documented_endpoint_is_routed(self):
        """A path in the reference that 404s sends a partner nowhere."""
        md = Path(settings.BASE_DIR, 'API.md').read_text(encoding='utf-8')
        paths = set(re.findall(
            r'onlygoodantibodies\.co\.uk(/api/v1/[\w\-/.]*)', md))
        self.assertTrue(paths, 'no endpoints found — has the base URL changed?')
        for path in sorted(paths):
            with self.subTest(path=path):
                response = self.client.get(path, {'gene': 'SNCA'},
                                           HTTP_X_API_KEY=self.key)
                self.assertNotEqual(
                    404, response.status_code,
                    f'{path} is documented and answers 404.')
