"""How this site's static files are named, and why the name carries a hash.

`collectstatic` runs in Render's Build Command, so every deploy rebuilds
`staticfiles/` — but rebuilding a file does not change what it is *called*.
`board.js` is `/static/pipeline/board.js` before a deploy and after it, and a
cache is keyed on the address, so a browser (or Cloudflare in front of the
origin) holding that URL goes on serving the copy it already has. WhiteNoise
sends `max-age=60, public` for an unhashed file, which is short at the origin
and nothing at all inside the window: no revalidation happens until it lapses.

That is not theoretical. On 5 Aug 2026 the owner could not delete a test gene
from its own page: the *page* was current — the Delete link was drawn, and so
was the panel it mounts into — while `board.js` was not, so
`OGABoard.deleteDialog` did not exist and the line wiring the button threw. The
symptom is the worst shape this repo keeps meeting: the page renders perfectly,
the control is there, and pressing it does nothing, with nothing on any screen
to say why. A hard refresh fixed it, which is the whole diagnosis — the browser
was told to ignore what it held.

A content hash in the filename ends the class of bug rather than this instance
of it: the URL changes when the bytes change, so nothing can be stale, and a
file that did *not* change keeps its address and stays cached. It is the same
reasoning as the built stylesheet not being in git — a thing derived on every
deploy cannot drift from what it was derived from.

**A name this cannot hash falls back to the plain one, and that is the whole
safety of the change.** Hashing moves a missing static file from a 404 on one
asset to a `ValueError` while *rendering*, which is a 500 on the whole page —
so a stylesheet nobody noticed was absent would take the page down with it.
This site is deployed by hand by a scientist, and the standing rule here is
that a storage problem must never be able to do that (`pipeline.W002` and
`W003` are warnings for the same reason). `stored_name` therefore returns the
unhashed name rather than raising: the asset is served exactly as it is today,
uncache-busted, and the page renders.

`manifest_strict = False` alone does **not** buy that, which is worth knowing
before anybody simplifies this away. Non-strict only means "do not insist on a
manifest entry" — Django then hashes the file live, and hashing a file that is
not there raises the same error. Both halves are needed.

Two live cases depend on it. `pipeline/tailwind.css` is built by the Build
Command and is *not* in git, so it is legitimately absent on a fresh clone —
the app already falls back to the CDN and says so at deploy, and it must not
start 500ing instead. And the cropper does `{% static 'pipeline/ocr/' %}` — a
*directory* — concatenating `worker.min.js`, whichever of the two wasm cores
the browser can run, and `eng.traineddata` onto it at runtime, because that is
the shape Tesseract's loader wants. Those assets are fetched unhashed and so
are not cache-busted, which is the right trade for a vendored bundle that never
changes, and `collectstatic` writes the original beside the hashed copy so the
URLs still serve.

One thing this forbids, and it fails loudly if forgotten: a file collected here
may not reference a file that is not collected. `collectstatic` rewrites
`url(...)` in CSS and `//# sourceMappingURL=` in JS to the hashed name, and a
reference it cannot resolve raises — which fails the build and aborts the
deploy, leaving the running version alone. That is the safe direction, but it
is a build somebody has to debug: the vendored bundles here
(`tesseract.min.js`, `worker.min.js`) each shipped a `sourceMappingURL` line
pointing at a `.map` that is not in the repo — dangling already, and a 404 in
anybody's dev tools — and those lines were removed rather than the maps added.
Re-vendoring one of those libraries brings the line back. `html2pdf.bundle.min.js`
was a third until the Manuscript Validation Checker was retired on 21 Aug 2026.
"""
from django.core.exceptions import SuspiciousOperation
from whitenoise.storage import CompressedManifestStaticFilesStorage


class HashedStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Content-hashed names, compressed, and forgiving of a name it has never
    seen. See the module docstring for why each of those three is here."""

    manifest_strict = False

    def stored_name(self, name):
        """The hashed name, or the plain one if there is nothing to hash.

        The only path that reaches a template. A reference this cannot resolve
        is a missing asset, and a missing asset was a 404 on one file before
        this storage existed — it must not become a 500 on the page that
        mentions it. Deliberately not applied to `post_process`, where a
        dangling reference *inside* a collected file still fails the build:
        that one is found at deploy with the running site untouched, which is
        the safe direction, and it is how the three vendored `sourceMappingURL`
        lines above were found in the first place.

        **`SuspiciousOperation` is the half that is easy to miss**, and it is
        not hypothetical: `{% static '/core/Not_Available.png' %}` — a leading
        slash, on the public antibody table — makes `FileSystemStorage` join a
        path outside `STATIC_ROOT` and raise `SuspiciousFileOperation`, which
        Django turns into a **400 on the whole page**. The old storage built a
        URL by string join and never touched the filesystem, so that typo had
        only ever cost a broken image. Catching `ValueError` alone left five
        public pages answering 400.
        """
        try:
            return super().stored_name(name)
        except (ValueError, SuspiciousOperation):
            return self.clean_name(name)
