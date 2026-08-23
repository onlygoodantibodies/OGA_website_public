"""A full-page template ends where it says it ends.

The bug this exists for: on 21 Aug 2026 seven live templates were found cut off
mid-write — `news_publications.html` stopped inside a scroll handler,
`about.html` inside a `{% static %}` tag, `contact.html` inside the characters
of `</footer`. Every one of them ended with no trailing newline, which is the
signature of a writer that stopped early rather than an edit anybody made.

It is the silent kind. A browser closes the tags it is missing, so the page
still returns 200 and still looks like a page; what it loses is whatever came
after the cut — a footer, a closing `</script>` that made the whole block a
syntax error and killed the script that was in it. Nothing on the screen says
so, no view raises, and a response test asserting status 200 passes.

Deliberately narrow: this asks only whether a page-level template *finishes*,
which is exactly the truncation signature and nothing else. It is not an HTML
validator — templates legitimately open a tag in one `{% if %}` branch and
close it in another, so balance cannot be checked this way without a wall of
false positives.

No exemption list, on purpose. The two files that would have wanted one were
unreferenced backups of `lesson_detail.html`, truncated the same way; they were
deleted instead. A page-level template nothing renders is the thing to remove,
not the thing to excuse.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent


def page_templates() -> list[Path]:
    """Every tracked template that declares itself a whole HTML document.

    Read from git rather than the filesystem, for the same reason
    `_all_test_ids` does: an untracked file is not part of the project yet.
    """
    listed = subprocess.run(
        ["git", "ls-files", "*.html"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    pages = []
    for name in listed:
        if name.startswith(("staticfiles/", "browser-extension/")):
            continue
        path = REPO_ROOT / name
        text = path.read_text(encoding="utf-8", errors="replace")
        if "<!DOCTYPE html>" in text[:400] or "<html" in text[:400]:
            pages.append(path)
    return pages


class TemplatesAreNotTruncatedTests(SimpleTestCase):
    def test_the_sweep_finds_the_pages_it_is_meant_to_cover(self):
        """A test that silently matched nothing would pass forever."""
        found = page_templates()
        self.assertGreater(len(found), 40, "expected the full-page templates to be found")
        names = {p.name for p in found}
        for expected in ("news_publications.html", "about.html", "roadmap.html"):
            self.assertIn(expected, names)

    def test_each_page_closes_its_document(self):
        cut_off = []
        for path in page_templates():
            tail = path.read_text(encoding="utf-8", errors="replace").rstrip()
            if not tail.endswith("</html>"):
                cut_off.append(f"{path.relative_to(REPO_ROOT)} ends with {tail[-60:]!r}")
        self.assertEqual(cut_off, [], "templates that stop before </html>:\n" + "\n".join(cut_off))
