"""Every board has a guide, and every column has a tip that resolves.

Two failure modes worth guarding:

  * A guide that 500s, or silently shows nothing, because its Markdown file was
    renamed or left out of a deploy. A guide is never worth a 500 — a missing
    file must say so in a sentence.
  * A column tooltip that renders empty because its key was renamed in the tips
    registry. An empty ``title=""`` looks exactly like a column nobody wrote
    help for, so it goes unnoticed indefinitely.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase

from pipeline.models import Member, Site

DB = "pipeline_db"

# board page → (guide url, a phrase the guide must actually contain)
GUIDES = {
    "/pipeline/targets/board/": ("/pipeline/targets/guide/", "target board"),
    "/pipeline/sessions/board/": ("/pipeline/sessions/guide/", "session"),
    "/pipeline/antibodies/board/": ("/pipeline/antibodies/guide/", "antibod"),
    "/pipeline/cell-lines/board/": ("/pipeline/cell-lines/guide/", "cell line"),
}


class BoardGuideTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        site = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        Member.objects.create(user_id=pu.pk, site_id=site.pk, role="admin",
                              is_active=True, display_name="Vera")
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def test_every_guide_renders(self):
        for board_url, (guide_url, phrase) in GUIDES.items():
            response = self.client.get(guide_url)
            self.assertEqual(response.status_code, 200, guide_url)
            body = response.content.decode().lower()
            self.assertIn(phrase.lower(), body, guide_url)
            self.assertIn("back to the board", body, guide_url)

    def test_every_board_links_to_its_guide(self):
        for board_url, (guide_url, _phrase) in GUIDES.items():
            html = self.client.get(board_url).content.decode()
            self.assertIn(guide_url, html,
                          f"{board_url} does not link to {guide_url}")

    def test_every_guide_downloads_as_markdown(self):
        for _board_url, (guide_url, phrase) in GUIDES.items():
            response = self.client.get(guide_url + "?download=1")
            self.assertEqual(response.status_code, 200, guide_url)
            self.assertIn("text/markdown", response["Content-Type"])
            self.assertIn(".md", response["Content-Disposition"])
            self.assertIn(phrase.lower(), response.content.decode().lower())

    def test_every_guide_file_exists_in_the_repo(self):
        """The pages read these off disk, so a rename must fail here first."""
        from pipeline.views.guides import _GUIDES
        for which, (filename, *_rest) in _GUIDES.items():
            path = Path(settings.BASE_DIR) / filename
            self.assertTrue(path.exists(), f"{which}: {filename} is missing")
            self.assertGreater(len(path.read_text(encoding="utf-8")), 500,
                               f"{which}: {filename} is suspiciously short")

    def test_a_missing_guide_file_says_so_rather_than_500ing(self):
        with mock.patch.object(Path, "read_text", side_effect=OSError("gone")):
            response = self.client.get("/pipeline/sessions/guide/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("missing", response.content.decode().lower())

    def test_a_guide_renders_raw_when_markdown_is_unavailable(self):
        """A guide is never worth a 500, even with the renderer gone."""
        import builtins
        real_import = builtins.__import__

        def no_markdown(name, *args, **kwargs):
            if name == "markdown":
                raise ImportError("no markdown")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", no_markdown):
            response = self.client.get("/pipeline/antibodies/guide/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("antibod", response.content.decode().lower())


class ColumnTipTests(TestCase):
    """Every board column's tip must resolve to real text.

    The target board pins this already; these extend it to the other three, and
    to the property that matters most for sessions — the page tooltip and the
    workbook's header comment are the same string, so they cannot drift.
    """
    databases = {"default", "pipeline_db", "academy_db"}

    def test_session_board_tips_come_from_the_workbook_registry(self):
        from pipeline.services import session_io as sio
        from pipeline.views.session_board import _HEADER_TIP_KEYS
        for column, key in _HEADER_TIP_KEYS.items():
            self.assertIn(key, sio.COLUMN_TIPS, f"no tip for column '{column}'")
            self.assertTrue(sio.COLUMN_TIPS[key].strip(),
                            f"empty tip for column '{column}'")

    def test_target_board_tips_still_resolve(self):
        from pipeline.services import target_list_io as tio
        from pipeline.views.target_board import _HEADER_TIP_KEYS
        for column, key in _HEADER_TIP_KEYS.items():
            self.assertIn(key, tio.COLUMN_TIPS, f"no tip for column '{column}'")
            self.assertTrue(tio.COLUMN_TIPS[key].strip(), column)

    def test_antibody_and_cell_line_tips_are_non_empty(self):
        from pipeline.services import antibody_board, cell_line_board
        for service in (antibody_board, cell_line_board):
            self.assertTrue(service.COLUMN_TIPS, service.__name__)
            for column, tip in service.COLUMN_TIPS.items():
                self.assertTrue(tip.strip(),
                                f"{service.__name__}: empty tip for '{column}'")

    def test_no_board_header_renders_an_empty_tooltip(self):
        """An empty title="" is invisible — it reads as a column nobody
        documented, which is how a renamed tip key goes unnoticed."""
        site = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="carl")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.create(user_id=pu.pk, site_id=site.pk, role="admin",
                              is_active=True, display_name="Carl")
        client = Client()
        self.assertTrue(client.login(username="carl", password="pw"))

        for url in ("/pipeline/targets/board/", "/pipeline/sessions/board/",
                    "/pipeline/antibodies/board/", "/pipeline/cell-lines/board/"):
            html = client.get(url).content.decode()
            # Report the count, never the page: asserting against the whole
            # document prints the entire HTML on failure, which buries the
            # one fact you need.
            empty = html.count('class="help" title=""')
            self.assertEqual(
                empty, 0,
                f"{url} renders {empty} column header(s) with an empty tooltip — "
                f"a tip key was probably renamed in that board's COLUMN_TIPS")
