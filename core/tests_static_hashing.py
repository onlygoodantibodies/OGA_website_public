"""Static files carry a content hash, and a missing one still renders.

The bug this exists for: on 5 Aug 2026 the gene page's Delete button was drawn
and dead, because the page was current and the cached `board.js` was not, so
`OGABoard.deleteDialog` did not exist and the line wiring the button threw. A
deploy rebuilds the file and does not change its URL, and a cache is keyed on
the URL — so nothing about `collectstatic` running could have prevented it.

Both tests here guard a *silent* failure, which is the bar for pinning
something at all. The first is the fix being wired up rather than merely
written; the second is the trap it introduces, which bites at deploy time in a
file nobody was editing.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from OGA_website.storages import HashedStaticFilesStorage

# The vendored bundles. Each shipped a `//# sourceMappingURL=` line pointing at
# a `.map` that is not in this repo — dangling already, a 404 in anybody's dev
# tools, and a hard `collectstatic` failure once names are hashed.
#
# `core/static/core/html2pdf.bundle.min.js` was a fourth until 21 Aug 2026. It
# and `xlsx.full.min.js` were loaded by the Manuscript Validation Checker and by
# nothing else, so both went with it.
VENDORED = (
    "pipeline/static/pipeline/ocr/tesseract.min.js",
    "pipeline/static/pipeline/ocr/worker.min.js",
)


class StaticFilesAreHashedTests(SimpleTestCase):
    """A class nothing points at is a fix nobody gets — the same rule as a
    routed endpoint that no page links to."""

    def test_the_project_actually_serves_static_files_through_it(self):
        backend = settings.STORAGES["staticfiles"]["BACKEND"]
        self.assertEqual(backend, "OGA_website.storages.HashedStaticFilesStorage",
                         "static files are being served without a content hash, "
                         "so a deploy can be shadowed by a cached copy")

    @override_settings(DEBUG=False)
    def test_a_file_that_exists_is_served_under_its_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "thing.js").write_text("console.log(1)\n")
            storage = HashedStaticFilesStorage(location=tmp)
            self.assertRegex(storage.stored_name("thing.js"),
                             r"^thing\.[0-9a-f]{12}\.js$")

    @override_settings(DEBUG=False)
    def test_a_file_that_is_missing_renders_rather_than_raising(self):
        """The whole safety of hashing here.

        Hashing turns an absent asset from a 404 on one file into a `ValueError`
        while *rendering*, i.e. a 500 on the page that mentions it. This site is
        deployed by hand by a scientist and a storage problem must never be able
        to do that. Two real cases: `pipeline/tailwind.css` is built by the
        Build Command and is legitimately absent on a fresh clone, and the
        cropper resolves `pipeline/ocr/` — a directory, which can never have a
        manifest entry — then concatenates Tesseract's worker and cores onto it.

        `manifest_strict = False` alone does not give this: non-strict falls
        back to hashing the file live, and hashing a file that is not there
        raises the same error.
        """
        with tempfile.TemporaryDirectory() as tmp:
            storage = HashedStaticFilesStorage(location=tmp)
            self.assertEqual(storage.stored_name("pipeline/tailwind.css"),
                             "pipeline/tailwind.css")
            self.assertEqual(storage.stored_name("pipeline/ocr/"),
                             "pipeline/ocr/")


class NoVendoredBundlePointsAtAMapWeDoNotShipTests(SimpleTestCase):
    """The trap hashing introduces, and the cheapest thing here that can fail.

    `collectstatic` rewrites `//# sourceMappingURL=` to the hashed name, and a
    reference it cannot resolve raises — failing the build and aborting the
    deploy. That is the safe direction (the running site is untouched) but it is
    a build somebody has to debug, in a file they did not edit: re-vendoring any
    of these libraries brings the line straight back.
    """

    def test_no_bundle_references_a_source_map(self):
        root = Path(settings.BASE_DIR)
        for rel in VENDORED:
            with self.subTest(bundle=rel):
                path = root / rel
                self.assertTrue(path.exists(), f"{rel} has moved — update this list")
                tail = path.read_bytes()[-4096:].decode("utf-8", "replace")
                self.assertIsNone(
                    re.search(r"sourceMappingURL=(\S+)", tail),
                    f"{rel} references a source map that is not in this repo, "
                    f"which fails collectstatic once static names are hashed. "
                    f"Drop the trailing sourceMappingURL comment as before.")
