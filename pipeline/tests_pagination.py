"""Boards draw a page, not the dataset.

The owner's review got *"page unresponsive"* — a browser dialog — opening the
antibodies board, which returns 3,225 rows of fifteen cells and paints them in
one `innerHTML`. It is not the extension and not the network: it is building and
drawing the whole dataset when nobody can read more than a screenful.

What is pinned here is mostly the ways this goes quietly wrong rather than
loudly:

* the **count** stays the whole filtered set, or "3,225 antibodies" becomes
  "50 antibodies" and reads as the filters having matched fifty things;
* a **download is never paginated**, which is why `page` is board state and not
  a filter field — `formQuery` feeds the rows fetch, the patch query string and
  every export href from the same form;
* a page **past the end** comes back as the last page, because a board whose
  filters have just narrowed is the usual way to get there and an empty grid is
  the one answer that is never true.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from pipeline.models import Antibody, CellLine, Company, Site, Target
from pipeline.services import antibody_board, board_page, cell_line_board
from pipeline.tests_timeouts import DB, _member_client


class OnePageAtATimeTests(TestCase):
    databases = {DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        self.company = Company.objects.using(DB).create(name="Abcam")
        for i in range(120):
            Antibody.objects.using(DB).create(
                target_id=self.target.pk, company_id=self.company.pk,
                catalogue_number=f"ab{1000 + i}", site_id=self.site.pk)

    def _rows(self, **params):
        resp = self.client.get("/pipeline/antibodies/board/rows/", params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_a_page_is_a_page(self):
        data = self._rows(page=1, per_page=50)
        self.assertEqual(len(data["rows"]), 50)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["pages"], 3)

    def test_the_count_is_the_whole_filtered_set(self):
        # The number beside the filters. If it became the page size, a reader
        # would believe their filters had matched fifty antibodies.
        self.assertEqual(self._rows(page=2, per_page=50)["count"], 120)

    def test_the_second_page_is_the_second_page(self):
        first = [r["catalogue"] for r in self._rows(page=1, per_page=10)["rows"]]
        second = [r["catalogue"] for r in self._rows(page=2, per_page=10)["rows"]]
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        self.assertFalse(set(first) & set(second))

    def test_a_page_past_the_end_is_the_last_page_not_an_empty_grid(self):
        data = self._rows(page=99, per_page=50)
        self.assertEqual(data["page"], 3)
        self.assertEqual(len(data["rows"]), 20)

    def test_nonsense_in_the_url_is_clamped_rather_than_fatal(self):
        for params in ({"page": "banana"}, {"page": "-4"}, {"per_page": "0"},
                       {"per_page": "999999"}, {"page": ""}):
            with self.subTest(params=params):
                data = self._rows(**params)
                self.assertGreaterEqual(data["page"], 1)
                self.assertLessEqual(data["per_page"], board_page.MAX_PER_PAGE)

    def test_filters_still_narrow_the_count(self):
        Target.objects.using(DB).create(gene_name="ELP3")
        self.assertEqual(self._rows(gene="SNCA")["count"], 120)
        self.assertEqual(self._rows(gene="ELP3")["count"], 0)

    def test_the_rows_built_are_bounded_by_the_page_not_the_dataset(self):
        """Paginating the built rows would still build all of them, which is
        most of the cost and all of the queries."""
        built = []
        real = antibody_board.row_for
        try:
            antibody_board.row_for = lambda a: (built.append(a.pk), real(a))[1]
            antibody_board.board_page(page=1, per_page=25)
        finally:
            antibody_board.row_for = real
        self.assertEqual(len(built), 25)

    def test_every_board_answers_the_same_shape(self):
        """All five, including the people board.

        `board.js` is shared, so a board whose server does not answer `pages`
        gets no pager — and the first version of this fell back to
        `ceil(count / perPage)`, which *invents* pagination: every row drawn,
        with a pager underneath saying "1–50 of 74" and a Next that changes
        nothing. Two things on one screen disagreeing about how much you are
        looking at. The fallback is gone and all five paginate instead.
        """
        from django.test import Client
        from pipeline.tests_user_board import _person

        # The people board is superuser-only, so it needs its own client — but
        # it is a board on the shared file and belongs in this sweep.
        _person("boss", self.site, role="admin", superuser=True)
        boss = Client()
        self.assertTrue(boss.login(username="boss", password="pw"))

        for url, client in (
                ("/pipeline/antibodies/board/rows/", self.client),
                ("/pipeline/cell-lines/board/rows/", self.client),
                ("/pipeline/sessions/board/rows/", self.client),
                ("/pipeline/targets/board/rows/", self.client),
                ("/pipeline/users/board/rows/", boss)):
            with self.subTest(url=url):
                resp = client.get(url, {"page": 1, "per_page": 10})
                self.assertEqual(resp.status_code, 200, url)
                data = resp.json()
                for key in ("rows", "count", "page", "pages", "per_page"):
                    self.assertIn(key, data, url)

    def test_the_shared_file_never_invents_a_pager(self):
        js = (Path(settings.BASE_DIR) / "pipeline/static/pipeline/board.js").read_text()
        self.assertNotIn("Math.ceil((data.count", js)
        self.assertIn("pages = data.pages || 1;", js)

    def test_the_page_honours_a_per_page_in_the_url(self):
        """The rows endpoint honours `?per_page=`; the page did not read it, so
        a URL asking for ten drew fifty and said nothing.

        Milder than the `?site=` case — nothing looks filtered that is not — but
        the same silence, and resolving a parameter server-side is only half of
        it. Clamped to the server's own ceiling so the two cannot disagree about
        what is too big.
        """
        js = (Path(settings.BASE_DIR) / "pipeline/static/pipeline/board.js").read_text()
        self.assertIn("get('per_page')", js)
        self.assertIn(f"Math.min(asked, {board_page.MAX_PER_PAGE})", js)
        # Still not a form field: `formQuery` feeds every download href, so a
        # page size living there would scope an export to a screenful.
        for name in ("target_board", "antibody_board", "cell_line_board",
                     "session_board"):
            html = (Path(settings.BASE_DIR) /
                    "pipeline/templates/pipeline" / f"{name}.html").read_text()
            body = html.split('id="filters"', 1)[1].split("</form>", 1)[0]
            self.assertNotRegex(body, r'name=["\']per_page["\']', name)

    def test_a_delete_refetches_the_page_rather_than_shortening_it(self):
        """All six delete handlers removed the `<tr>` and decremented the count.

        That is right for a grid holding everything and wrong for a *page*: the
        row that should have moved up stays on the next page, so the page
        shrinks, and deleting the last row of the last page leaves an empty grid
        — which after a successful delete reads as data loss. One helper now, in
        the shared file, because it was six copies of the same mistake.
        """
        js = (Path(settings.BASE_DIR) / "pipeline/static/pipeline/board.js").read_text()
        self.assertIn("function rowDeleted(", js)
        block = js[js.index("function rowDeleted("):]
        block = block[:block.index("\n  /*")]
        self.assertIn("await board.load()", block)
        # The banner is posted after the await: `load` clears messages so that a
        # stale error cannot outlive a reload.
        self.assertLess(block.index("await board.load()"), block.index("errors.show"))
        # The three child boards. A gene is deleted from its own page now, not
        # from the targets list, so that board has no delete handler at all.
        for name in ("antibody_board", "cell_line_board", "session_board"):
            html = (Path(settings.BASE_DIR) /
                    "pipeline/templates/pipeline" / f"{name}.html").read_text()
            self.assertIn("OGABoard.rowDeleted(", html, name)
            self.assertNotIn("board.count - 1", html,
                             f"{name} still hand-decrements after a delete")


class ALinkToARowStillFindsItTests(TestCase):
    """Pagination breaks every link that points at a row.

    The sessions board is opened by id from two places — `?open=<id>`, which is
    where `pipeline:session_detail` redirects a bookmark, and "Record its
    results here" after a save — and both looked the row up in the *drawn* grid.
    Draw fifty rows instead of all of them and anything further down comes back
    missing, under a message reading "That session is outside the current
    filters — clear them to see it." That is false, and the advice makes it
    worse: clearing the filters lengthens the list and pushes the row further
    back.
    """

    databases = {DB, "academy_db"}

    def setUp(self):
        from pipeline.models import ExperimentSession, Member
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        self.sessions = [
            ExperimentSession.objects.using(DB).create(
                target_id=self.target.pk, procedure_type="WB",
                date=f"2026-0{1 + i // 28}-{1 + i % 28:02d}",
                site_id=self.site.pk, experimenter_id=self.member.pk)
            for i in range(80)]

    def _rows(self, **params):
        return self.client.get("/pipeline/sessions/board/rows/", params).json()

    def test_a_row_on_a_later_page_is_found_and_its_page_returned(self):
        # Ordered by -date, so the oldest session is on the last page.
        oldest = min(self.sessions, key=lambda s: s.date)
        data = self._rows(per_page=25, locate=oldest.pk)
        self.assertIs(data["located"], True)
        self.assertEqual(data["page"], 4)
        self.assertIn(oldest.pk, [r["id"] for r in data["rows"]])

    def test_locate_beats_an_explicit_page(self):
        oldest = min(self.sessions, key=lambda s: s.date)
        data = self._rows(per_page=25, page=1, locate=oldest.pk)
        self.assertEqual(data["page"], 4)

    def test_a_row_that_really_is_outside_the_filters_says_so(self):
        other = Target.objects.using(DB).create(gene_name="ELP3")
        data = self._rows(per_page=25, gene="ELP3", locate=self.sessions[0].pk)
        self.assertIs(data["located"], False)
        self.assertEqual(data["count"], 0)
        self.assertTrue(other.pk)

    def test_asking_for_nothing_reports_nothing(self):
        self.assertIsNone(self._rows(per_page=25)["located"])

    def test_a_nonsense_locate_is_not_fatal(self):
        for value in ("banana", "-1", "999999"):
            with self.subTest(value=value):
                data = self._rows(per_page=25, locate=value)
                self.assertEqual(data["count"], 80)
                self.assertIs(data["located"], False)

    def test_the_board_asks_the_server_rather_than_the_drawn_grid(self):
        js = (Path(settings.BASE_DIR) / "pipeline/static/pipeline/board.js").read_text()
        self.assertIn("async function locate(", js)
        self.assertIn("locate=", js)
        html = (Path(settings.BASE_DIR)
                / "pipeline/templates/pipeline/session_board.html").read_text()
        self.assertIn("board.locate(", html)
        # Every claim that a row is "outside the current filters" must be
        # guarded by what the *server* said about it. A row simply being on
        # another page must never produce that sentence — which is exactly what
        # a `querySelector` on the drawn grid would do.
        #
        # Comments are stripped first: this is about what the code does, and the
        # note explaining the change quotes the very sentence being looked for.
        # (It caught itself the first time this ran.)
        code = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        parts = code.split("outside the current filters")
        self.assertGreaterEqual(len(parts), 2, "the message has gone entirely")
        for i, before in enumerate(parts[:-1]):
            with self.subTest(occurrence=i):
                window = before[-700:]
                self.assertRegex(
                    window, r"board\.locate\(|found === false",
                    "this refusal is not guarded by the server's answer")


class ADownloadIsNeverPaginatedTests(TestCase):
    """`page` is board state, not a filter field — `formQuery` builds the rows
    fetch, the patch query *and* every export href from the same form."""

    databases = {DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        for i in range(60):
            CellLine.objects.using(DB).create(
                name=f"HAP1-{i}", genotype="KO", target_id=self.target.pk,
                site_id=self.site.pk)

    def test_the_export_carries_every_filtered_row(self):
        import openpyxl
        resp = self.client.get("/pipeline/cell-lines/export/",
                               {"gene": "SNCA", "page": "2", "per_page": "10"})
        self.assertEqual(resp.status_code, 200)
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        self.assertEqual(ws.max_row, 61, "header + every matching row")

    def test_no_board_puts_the_page_in_its_filter_form(self):
        for name in ("target_board", "antibody_board", "cell_line_board",
                     "session_board"):
            html = (Path(settings.BASE_DIR) /
                    "pipeline/templates/pipeline" / f"{name}.html").read_text()
            form = html.split('id="filters"', 1)
            self.assertEqual(len(form), 2, f"{name} has no filters form")
            body = form[1].split("</form>", 1)[0]
            self.assertNotRegex(body, r'name=["\']page["\']',
                                f"{name}'s filter form carries the page number")

    def test_the_pager_is_wired_on_every_board(self):
        for name in ("target_board", "antibody_board", "cell_line_board",
                     "session_board"):
            html = (Path(settings.BASE_DIR) /
                    "pipeline/templates/pipeline" / f"{name}.html").read_text()
            self.assertIn('id="row-pager"', html, name)
            self.assertIn("pagerEl:", html, name)

    def test_the_rows_fetch_is_the_only_thing_that_carries_a_page(self):
        js = (Path(settings.BASE_DIR) / "pipeline/static/pipeline/board.js").read_text()
        # `pagedQuery` for the grid; `query` — without a page — for the patch
        # endpoint and for anything a board hangs a download on.
        self.assertIn("pagedQuery()", js)
        patch_block = js[js.index("async function patch("):]
        patch_block = patch_block[:patch_block.index("\n    }")]
        self.assertIn("${query()}", patch_block)
        self.assertNotIn("pagedQuery", patch_block)
