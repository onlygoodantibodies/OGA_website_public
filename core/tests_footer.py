"""The site footer is drawn from one file, and it names the pages the nav names.

The bug this exists for: the footer had been hand-copied into 28 templates in
fourteen variants. Two changes the team review asked for in August 2026 --
LinkedIn and BlueSky under "Follow Us" rather than "Contact Us", and Quick
Links carrying the newer tabs -- could not be made true of the site by editing
any one of them, and the drift had already produced its own defects: an
`alt="LinkedIn"` on the BlueSky icon, a page whose Quick Links held three
entries, and two entries ("Publications" and "News") pointing at one page.

Both halves are the silent kind. A page that quietly keeps an older footer
still returns 200 and still looks like a page; what a reader loses is a route
to half the site, and nothing on the screen says the footer they are looking
at is out of date.

So: no template draws footer markup of its own, and every destination in the
header nav appears in the footer. The second is the rule written at the top of
core/templates/core/footer.html -- add a page to the nav and add it to the
footer -- which is only a rule if something asks.

What is checked is the *site* footer -- the three-column one, marked by
`footer-column`. Two templates draw a deliberately different footer holding a
copyright line and nothing else: `templates/404.html`, which CLAUDE.md keeps to
named URLs and no ORM because a 404 template that raises is silently replaced
by the one it exists to avoid, and `allauth/layouts/base.html`, the account
area's chrome. Neither is a copy of the site footer and neither should become
one.

Not asserted here: that every page *has* a footer. Four do not (the Tools hub,
Connect your AI, the Portal, the Planning Framework), which predates this and
is a real gap, but pinning it would need an exemption list holding four pages
that ought to change rather than be excused.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

REPO_ROOT = Path(__file__).resolve().parent.parent
FOOTER_TEMPLATE = REPO_ROOT / "core" / "templates" / "core" / "footer.html"


def tracked_templates() -> list[Path]:
    """Tracked templates, minus the build artifact and the extension's own."""
    listed = subprocess.run(
        ["git", "ls-files", "*.html"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [
        REPO_ROOT / name for name in listed
        if not name.startswith(("staticfiles/", "browser-extension/"))
    ]


class OneFooterTests(SimpleTestCase):
    def test_the_sweep_finds_the_templates_it_is_meant_to_cover(self):
        """A sweep that silently matched nothing would pass forever."""
        found = {p.name for p in tracked_templates()}
        self.assertGreater(len(found), 40)
        for expected in ("home.html", "about.html", "footer.html"):
            self.assertIn(expected, found)

    def test_only_the_footer_template_draws_the_site_footer(self):
        offenders = []
        for path in tracked_templates():
            if path == FOOTER_TEMPLATE:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "footer-column" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(
            offenders, [],
            "these draw their own copy of the site footer instead of "
            "including core/footer.html:\n" + "\n".join(offenders),
        )

    def test_the_marker_it_searches_for_is_in_the_footer_template(self):
        """Or the sweep above is looking for a string nothing ever contains."""
        self.assertIn(
            "footer-column",
            FOOTER_TEMPLATE.read_text(encoding="utf-8"),
        )


class FooterNamesEveryNavDestinationTests(TestCase):
    # Named, not "__all__": `default` is deliberately {} and Django gives it the
    # dummy backend, so a teardown flush of it raises ImproperlyConfigured.
    databases = {"pipeline_db", "academy_db"}

    def _urls_in(self, template_name: str) -> set[str]:
        """The resolved hrefs a template's {% url %} tags produce."""
        text = (REPO_ROOT / "core" / "templates" / "core" / template_name).read_text(
            encoding="utf-8"
        )
        found = set()
        for name in re.findall(r"{%\s*url\s*'([a-z_]+:?[a-z_]*)'\s*%}", text):
            try:
                found.add(reverse(name))
            except Exception:
                continue
        return found

    def test_the_footer_reaches_everywhere_the_nav_does(self):
        nav = self._urls_in("header.html")
        footer = self._urls_in("footer.html")
        self.assertTrue(nav, "read no destinations out of the nav")
        missing = sorted(nav - footer - {reverse("academy:login"),
                                         reverse("academy:academy_home")})
        self.assertEqual(
            missing, [],
            "the nav reaches these and the footer does not — add them to "
            "core/templates/core/footer.html:\n" + "\n".join(missing),
        )

    def test_the_rendered_footer_puts_the_networks_under_follow_us(self):
        """The team review's actual ask, on a real page rather than in source."""
        body = self.client.get(reverse("about")).content.decode()
        follow = body.index("Follow Us")
        contact = body.index("Contact Us")
        self.assertLess(contact, follow, "columns are in an unexpected order")
        between = body[contact:follow]
        self.assertNotIn("linkedin.com", between)
        self.assertNotIn("bsky.app", between)
        self.assertIn("linkedin.com", body[follow:])
        self.assertIn("bsky.app", body[follow:])
