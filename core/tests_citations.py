"""The citation snapshot: the shared normaliser, and what a missing file means.

Two failure modes are pinned here and both are silent ones.

**The normaliser drifting from the extension's copy.** `core/citations.py` hashes
a title when the artefact is built and `browser-extension/src/paper.js` hashes it
again in the reader's browser. If those two ever disagree, papers simply stop
being recognised — no error, no empty result, a feature that quietly does nothing
on whatever subset of titles diverged. Both suites read the same vector file, so
neither side can move alone.

**A missing artefact reading as an empty one.** An empty citation table is
indistinguishable on screen from a table in which nothing matched, and the second
tells readers no paper cites anything. The file is optional by design; its absence
has to stay legible as absence.
"""
import json
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core import citations


VECTORS = Path(citations.DATA_DIR) / "title_normalisation_vectors.json"


class TitleNormalisationTests(TestCase):
    databases = {"academy_db", "pipeline_db"}

    def test_every_shared_vector_still_holds(self):
        """The same file browser-extension/test/paper.test.mjs reads."""
        data = json.loads(VECTORS.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["vectors"]), 50,
                                "the vector corpus should be real, not a token few")
        wrong = [v for v in data["vectors"]
                 if citations.normalise_title(v["title"]) != v["normalised"]
                 or citations.title_key(v["title"]) != v["key"]]
        self.assertEqual(wrong, [], f"{len(wrong)} vectors no longer match")

    def test_a_greek_letter_and_its_name_reach_one_key(self):
        """9.1% of real titles carry one, and stripping them loses the paper."""
        self.assertEqual(citations.title_key("Loss of β-catenin in stem cells"),
                         citations.title_key("Loss of beta-catenin in stem cells"))
        self.assertEqual(citations.title_key("NF-κB signalling"),
                         citations.title_key("NF-kappaB signalling"))

    def test_the_key_is_eight_hex_digits_and_stays_in_32_bits(self):
        for title in ("a", "an ordinary title about kinases", "ζ" * 200):
            self.assertRegex(citations.title_key(title), r"^[0-9a-f]{8}$")

    def test_case_and_punctuation_do_not_change_the_key(self):
        self.assertEqual(citations.title_key("TDP-43: aggregation, in vivo"),
                         citations.title_key("tdp 43 aggregation in vivo"))


class MissingArtefactTests(TestCase):
    """The artefact is optional. Absent must not read as empty."""
    databases = {"academy_db", "pipeline_db"}

    def setUp(self):
        citations.load.cache_clear()
        citations.raw_bytes.cache_clear()
        self.addCleanup(citations.load.cache_clear)
        self.addCleanup(citations.raw_bytes.cache_clear)

    def test_a_missing_file_is_none_not_an_empty_table(self):
        with mock.patch.object(citations, "ARTEFACT", Path("/nonexistent/x.json")):
            self.assertIsNone(citations.load())
            self.assertIsNone(citations.raw_bytes())
            self.assertIsNone(citations.summary())

    def test_a_truncated_file_is_none_rather_than_an_exception(self):
        """A corrupt artefact must not take the whole index build down with it.

        Written against a REAL truncated file rather than a patched reader: what
        is in doubt is whether json.load's exception is caught, and a mock that
        raises the exception we chose would prove only that we chose it.
        """
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"schema": 1, "papers": [truncat')
            broken = Path(handle.name)
        self.addCleanup(broken.unlink)
        with mock.patch.object(citations, "ARTEFACT", broken):
            self.assertIsNone(citations.load())

    def test_a_schema_we_do_not_understand_is_refused(self):
        with mock.patch.object(citations, "load", return_value=None):
            self.assertIsNone(citations.summary())

    def test_the_endpoint_404s_rather_than_serving_an_empty_table(self):
        with mock.patch.object(citations, "raw_bytes", return_value=None):
            response = self.client.get(reverse("extension_citations"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")


class DeployedArtefactTests(TestCase):
    """What is actually committed, so a bad build cannot ship quietly."""
    databases = {"academy_db", "pipeline_db"}

    def test_the_snapshot_says_which_snapshot_it_is(self):
        data = citations.load()
        if data is None:
            self.skipTest("no citation artefact in this checkout")
        self.assertTrue(data["source"]["citeab_data_through"],
                        "a snapshot that cannot date itself is not publishable")
        self.assertEqual(data["schema"], citations.SCHEMA)

    def test_every_lookup_key_points_at_a_real_paper(self):
        data = citations.load()
        if data is None:
            self.skipTest("no citation artefact in this checkout")
        top = len(data["papers"])
        for name in ("by_title", "by_pmid", "by_doi"):
            bad = [k for k, slot in data[name].items() if not 0 <= slot < top]
            self.assertEqual(bad[:5], [], f"{name} has {len(bad)} out-of-range slots")
        self.assertEqual(len(data["years"]), top,
                         "every paper needs a year, or the title check cannot run")

    def test_the_endpoint_serves_the_file_verbatim(self):
        if citations.raw_bytes() is None:
            self.skipTest("no citation artefact in this checkout")
        response = self.client.get(reverse("extension_citations"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, citations.raw_bytes())
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")


class DoiResolutionTests(TestCase):
    """The DOI pass: parsing Europe PMC, and what a clash does.

    The network half cannot be tested here and is not what would go wrong
    quietly. What would is the parsing — keyed on the id we ASKED with — and the
    collision rule, where serving one paper's citations under another's DOI has
    no symptom on screen.

    Imported from ``core.citations_doi``, which is the module that actually runs:
    the management command is a wrapper, and testing the wrapper would pin the
    packaging rather than the behaviour.
    """
    databases = {"academy_db", "pipeline_db"}

    def test_two_papers_claiming_one_doi_lose_the_key(self):
        """An arbitrary winner is indistinguishable from a correct answer."""
        from core.citations_doi import index_from
        artefact = {"by_pmid": {"1": 0, "2": 1}}
        by_doi, clashes = index_from(artefact, {"1": "10.1/x", "2": "10.1/x"})
        self.assertEqual(by_doi, {})
        self.assertEqual(clashes, 1)

    def test_two_ids_for_the_SAME_paper_keep_the_key(self):
        """A preprint and its published record are one paper, not a clash."""
        from core.citations_doi import index_from
        artefact = {"by_pmid": {"1": 0, "PPR9": 0}}
        by_doi, clashes = index_from(artefact, {"1": "10.1/x", "PPR9": "10.1/x"})
        self.assertEqual(by_doi, {"10.1/x": 0})
        self.assertEqual(clashes, 0)

    def test_the_module_runs_without_django(self):
        """The whole reason it is not in the management command.

        A scientist with a laptop and a checkout should not need a virtualenv to
        fill in some DOIs.
        """
        import subprocess, sys as _sys
        from pathlib import Path as _Path
        root = _Path(citations.DATA_DIR).parent.parent
        result = subprocess.run(
            [_sys.executable, str(root / "core" / "citations_doi.py"), "--help"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin"})          # no DJANGO_SETTINGS_MODULE
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check", result.stdout)

    def test_a_pubmed_row_is_keyed_on_the_pmid_we_asked_with(self):
        from core.citations_doi import dois_from as _dois_from
        found = _dois_from({"resultList": {"result": [
            {"id": "21994327", "source": "MED", "pmid": "21994327",
             "doi": "10.1038/S41586-020-1234-5"},
        ]}})
        # Lower-cased, because the extension looks up a lower-cased DOI.
        self.assertEqual(found, {"21994327": "10.1038/s41586-020-1234-5"})

    def test_a_preprint_row_is_keyed_on_its_ppr_id(self):
        from core.citations_doi import dois_from as _dois_from
        found = _dois_from({"resultList": {"result": [
            {"id": "PPR383438", "source": "PPR", "doi": "10.1101/2020.05.06.081414"},
        ]}})
        self.assertEqual(found, {"PPR383438": "10.1101/2020.05.06.081414"})

    def test_a_row_with_no_doi_is_skipped_rather_than_stored_empty(self):
        from core.citations_doi import dois_from as _dois_from
        self.assertEqual(_dois_from({"resultList": {"result": [
            {"id": "1", "source": "MED", "pmid": "1", "doi": ""},
            {"id": "2", "source": "MED", "pmid": "2"},
        ]}}), {})

    def test_an_unrecognised_shape_returns_nothing_rather_than_raising(self):
        """--check exists because this shape is Europe PMC's to change."""
        from core.citations_doi import dois_from as _dois_from
        self.assertEqual(_dois_from({}), {})
        self.assertEqual(_dois_from({"resultList": None}), {})
