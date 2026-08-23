"""The sync client in `API.md` is executed, not just printed.

`API.md` is what a consumer builds against, and its centrepiece is a working
Python client that downloads new figures and **deletes local files that have
left the dataset**. Documentation that has drifted from the API is worse than
none — a reader who follows it and gets nothing concludes the feature is broken
— and this particular script can destroy somebody's copy of the data if its
diff-and-delete logic is wrong about what "missing" means.

So the script is extracted from the Markdown and run against a live server. If
the API changes shape underneath it, this fails, and the fix is to correct the
document rather than to loosen the test.

Image bytes are stubbed. What is under test is the sync logic — which files are
fetched, which are removed, and whether an unchanged dataset costs a `304` —
not urllib's ability to retrieve a PNG from a CDN.
"""
from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import LiveServerTestCase

from core.models import APIConsumer
from pipeline.models import Antibody, Company, PublicationImage, Target

DOC = Path(settings.BASE_DIR) / "API.md"


def _documented_client():
    """The first ```python block in API.md — the sync client."""
    blocks = re.findall(r"```python\n(.*?)```", DOC.read_text(), re.S)
    if not blocks:
        raise AssertionError("API.md no longer contains a Python client.")
    return blocks[0]


class TheDocumentedSyncClientWorksTests(LiveServerTestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

        self.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", tier="data")
        company = Company.objects.create(name="Abcam")
        target = Target.objects.create(
            protein_name="Alpha-synuclein", gene_name="SNCA")
        self.antibody = Antibody.objects.create(
            target=target, company=company, catalogue_number="ab138501",
            rrid="AB_2687467", wb_recommended=True)
        self.wb = PublicationImage.objects.create(
            antibody=self.antibody, application_type="WB",
            image="publication_images/2026/ab138501_WB.png")
        self.icc = PublicationImage.objects.create(
            antibody=self.antibody, application_type="ICC-IF",
            image="publication_images/2026/ab138501_ICC.png")

        self.dest = Path(self.mkdtemp())

    def mkdtemp(self):
        import tempfile
        directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        return directory

    # --- running the documented script ---------------------------------

    def _run_client(self):
        """Execute the script from API.md; returns (exit code, printed lines).

        Only the three constants a reader is told to set are overridden — the
        key, the base URL and the destination. Everything else is the document's
        own code.
        """
        namespace = {"__name__": "oga_sync_doc"}
        exec(compile(_documented_client(), "API.md", "exec"), namespace)

        namespace["API_KEY"] = str(self.consumer.api_key)
        namespace["BASE"] = f"{self.live_server_url}/api/v1"
        namespace["DEST"] = self.dest
        namespace["STATE"] = self.dest / ".sync_state.json"

        real_urlopen = urllib.request.urlopen

        def fake_urlopen(request, *args, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            # Figure URLs point at the public CDN, which is not this test
            # server. Everything else is a genuine request to the live API.
            if "/media/" in url:
                # Bytes derived from the object key, not a constant. A constant
                # makes "did the client re-download a replaced figure?"
                # unanswerable — the old and new content compare equal, so the
                # assertion passes on a client that fetched nothing.
                return _StubResponse(f"PNG-BYTES:{url}".encode())
            return real_urlopen(request, *args, **kwargs)

        printed = []
        with mock.patch.object(urllib.request, "urlopen", fake_urlopen), \
                mock.patch("builtins.print", lambda *a: printed.append(" ".join(map(str, a)))):
            code = namespace["main"]()
        return code, printed

    def _files(self):
        return {p.name for p in self.dest.iterdir() if not p.name.startswith(".")}

    def _icc_filename(self):
        """The name the manifest gives the ICC figure.

        Asked of the manifest rather than spelled here, so this test cannot
        quietly stop referring to a real file if the default pattern changes.
        """
        response = self.client.get(
            "/api/v1/manifest/", HTTP_X_API_KEY=str(self.consumer.api_key))
        rows = response.json()["files"]
        return next(r["filename"] for r in rows if r["application"] == "ICC-IF")

    # --- the three things a consumer depends on ------------------------

    def test_a_first_run_downloads_everything_in_scope(self):
        code, printed = self._run_client()
        self.assertEqual(code, 0, printed)
        self.assertEqual(
            self._files(),
            {"SNCA_ab138501_WB.png", "SNCA_ab138501_ICC-IF.png"})
        self.assertIn("2 added", " ".join(printed))

    def test_a_second_run_costs_a_304_and_changes_nothing(self):
        """The claim the whole design rests on: reconnecting is nearly free."""
        self._run_client()
        before = self._files()

        code, printed = self._run_client()
        self.assertEqual(code, 0)
        self.assertIn("Nothing has changed.", printed)
        self.assertEqual(self._files(), before)

    def test_a_withdrawn_figure_is_deleted_locally(self):
        """The reason the manifest is complete by default.

        Deletions are recorded nowhere, so this only works because the client
        diffs a full manifest. If it ever silently stops working, a consumer
        goes on publishing a figure OGA has withdrawn.
        """
        self._run_client()
        self.assertIn("SNCA_ab138501_ICC-IF.png", self._files())

        self.icc.delete()

        code, printed = self._run_client()
        self.assertEqual(code, 0)
        self.assertEqual(self._files(), {"SNCA_ab138501_WB.png"})
        self.assertIn("1 removed", " ".join(printed))

    def test_a_replaced_figure_is_re_fetched(self):
        """The bytes on disk must change, not merely the client's output.

        This test used to assert only that the run did not print
        "Nothing has changed." — which passed while the client did nothing at
        all. `created_at` does not move when a figure is re-cropped, so the ETag
        shifts (the fingerprint hashes the stored key), the client dutifully
        pulls the whole manifest, and then skips the file because one of that
        name already exists locally. The `filename` is built from gene,
        catalogue number and application, so it is **unchanged** by a
        replacement; only `url` moves.

        So the assertion has to reach the file. Anything weaker passes on a
        client that notices the change and ignores it, which is exactly what was
        documented for a week.
        """
        self._run_client()

        target = self.dest / self._icc_filename()
        self.assertTrue(target.exists())
        before = target.read_bytes()

        self.icc.image = "publication_images/2026/ab138501_ICC_v2.png"
        self.icc.save()

        code, printed = self._run_client()
        self.assertEqual(code, 0)
        self.assertNotIn("Nothing has changed.", printed,
                         "A replaced figure left the dataset looking unchanged.")
        self.assertIn("1 replaced", " ".join(printed))
        self.assertNotEqual(
            target.read_bytes(), before,
            "The client saw the dataset change and kept the old image. It is "
            "comparing filenames rather than urls.")

    def test_an_unchanged_figure_is_not_re_fetched(self):
        """The control: without this, 're-fetch everything' passes the test above."""
        self._run_client()
        code, printed = self._run_client()
        self.assertEqual(code, 0)
        self.assertIn("Nothing has changed.", printed)

    def test_it_refuses_to_delete_from_an_incomplete_manifest(self):
        """The guard that stops the client destroying a consumer's copy.

        A capped or filtered manifest cannot be diffed — a URL missing from it
        may still be in the dataset. The document says so and the script checks
        it; this is what makes that more than a comment.
        """
        self._run_client()

        namespace = {"__name__": "oga_sync_doc"}
        exec(compile(_documented_client(), "API.md", "exec"), namespace)
        namespace["API_KEY"] = str(self.consumer.api_key)
        # A limited manifest, which is what `complete: false` reports.
        namespace["BASE"] = f"{self.live_server_url}/api/v1"
        namespace["DEST"] = self.dest
        namespace["STATE"] = self.dest / ".missing_state.json"

        original = urllib.request.Request

        def limited(url, headers=None, **kwargs):
            if "/manifest/" in url:
                url = url + "?limit=1"
            return original(url, headers=headers or {}, **kwargs)

        printed = []
        with mock.patch.object(urllib.request, "Request", limited), \
                mock.patch("builtins.print", lambda *a: printed.append(" ".join(map(str, a)))):
            code = namespace["main"]()

        self.assertEqual(code, 1)
        self.assertIn("refusing to sync deletions", " ".join(printed))
        # And nothing was removed.
        self.assertEqual(
            self._files(),
            {"SNCA_ab138501_WB.png", "SNCA_ab138501_ICC-IF.png"})


class _StubResponse(io.BytesIO):
    """Enough of an HTTP response for the client's `with urlopen(...)` calls."""

    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
