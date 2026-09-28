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


# Two names are legitimately absent from a checkout and must not fail this.
#
# `pipeline/tailwind.css` is built by the Build Command and deliberately not in
# git, so that it cannot drift from the templates it is derived from; the app
# already falls back to the CDN and says so with `pipeline.W003`.
#
# `pipeline/ocr/` is a *directory*. The cropper concatenates `worker.min.js`,
# whichever wasm core the browser can run, and `eng.traineddata` onto it at
# runtime, because that is the shape Tesseract's loader wants.
STATIC_MAY_BE_ABSENT = (
    "pipeline/tailwind.css",
    "pipeline/ocr/",
)

_STATIC_CALL = re.compile(r"""\{%\s*static\s+['"]([^'"]+)['"]""")


class EveryStaticReferenceResolvesTests(SimpleTestCase):
    """A `{% static %}` name that resolves to nothing is silent by design.

    The test above pins that a missing file *renders* rather than raising, and
    that is deliberate — a hand-deployed site must not be taken down by an
    absent stylesheet (see `OGA_website/storages.py`). The cost of that trade is
    this: a name nobody ever typed correctly produces a page that looks fine and
    one 404 on an asset, and no screen anywhere says so.

    It is not hypothetical. Seventeen templates asked for
    `academy/css/style.css` — the whole allauth account area (login, signup, all
    four password-reset pages) plus the Academy's own lesson, quiz and account
    pages — against a path that has never existed in this repository's history,
    while the file they meant, `academy/styles.css`, sat referenced by nothing.
    It was found in the origin's access log on 20 Sep 2026, after a real learner
    opened an email-confirmation link, then a lesson, then a quiz, and took a
    404 on each.

    Cheap enough to be worth it for every template at once: a regex sweep and
    one `finders.find` per distinct name.
    """

    def test_every_static_name_in_a_template_points_at_a_real_file(self):
        from django.contrib.staticfiles import finders

        root = Path(settings.BASE_DIR)
        skip = {"staticfiles", ".venv", "node_modules", ".git"}
        missing = []
        for path in sorted(root.rglob("*.html")):
            if skip & set(path.relative_to(root).parts):
                continue
            for name in _STATIC_CALL.findall(path.read_text(errors="replace")):
                if name.startswith(STATIC_MAY_BE_ABSENT):
                    continue
                if finders.find(name) is None:
                    missing.append(f"{path.relative_to(root)} -> {name}")

        self.assertEqual(
            missing, [],
            "These templates name a static file that does not exist. The page "
            "will render and the asset will 404, so nothing else will tell "
            "you:\n  " + "\n  ".join(missing))


class RootIconRequestsAreAnsweredTests(SimpleTestCase):
    """The other half of the sweep above: requests that read no template.

    Forty-five templates carry `<link rel="icon">`, and the origin's access log
    for 19-20 Sep 2026 is still full of 404s for `/favicon.ico` and the two
    apple-touch names — several of them with the home page, which *does* set an
    icon, as the referer. A root request does not parse HTML first, so no tag
    anywhere could have answered it.
    """

    def test_each_root_icon_redirects_to_a_file_that_exists(self):
        from django.contrib.staticfiles import finders
        from core.views import ROOT_ICONS

        for name, target in ROOT_ICONS.items():
            with self.subTest(name=name):
                answer = self.client.get(f"/{name}")
                # 302: the target carries a content hash, so a permanent
                # redirect would be cached against a name that stops existing
                # the next time the image changes.
                self.assertEqual(answer.status_code, 302)
                self.assertIsNotNone(
                    finders.find(target),
                    f"/{name} redirects to {target}, which does not exist.")
                self.assertIn(Path(target).stem, answer["Location"])
