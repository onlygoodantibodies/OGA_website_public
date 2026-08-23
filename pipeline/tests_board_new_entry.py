"""Every board can create, on the board, from one column source.

The pop-out's value depends on two things that are easy to break silently:

  * the table's headings being the *same list* the Excel template and the paste
    parser use — if they drift, a column lands in the wrong field and nobody
    notices until the data is wrong;
  * the parsers accepting what the table sends. bulk_cell_lines.parse requires a
    header row and returns zero rows without one, so "the table always sends its
    headings" is load-bearing, not tidiness.
"""
from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from pipeline.models import Member, Site
from pipeline.views.imports import _KINDS, columns_and_example

DB = "pipeline_db"

# board url → the import kind whose columns its pop-out must use
BOARDS = {
    "/pipeline/antibodies/board/": "antibodies",
    "/pipeline/cell-lines/board/": "cell-lines",
    "/pipeline/sessions/board/": "sessions",
}


class NewEntryPanelTests(TestCase):
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

    def _columns_on_page(self, url):
        html = self.client.get(url).content.decode()
        marker = '<script id="new-columns" type="application/json">'
        self.assertIn(marker, html, f"{url} does not render its column list")
        start = html.index(marker) + len(marker)
        return json.loads(html[start:html.index("</script>", start)])

    def test_every_board_offers_the_pop_out(self):
        for url in list(BOARDS) + ["/pipeline/targets/board/"]:
            html = self.client.get(url).content.decode()
            self.assertIn('id="new-btn"', html, f"{url} has no add button")
            self.assertIn('id="new-panel"', html, f"{url} has no pop-out mount")
            self.assertIn("OGABoard.newEntry(", html, f"{url} does not wire the pop-out")

    def test_table_headings_are_the_import_column_list(self):
        """One source, so the table, the Excel template and the parser agree."""
        for url, kind in BOARDS.items():
            expected, _example = columns_and_example(kind)
            self.assertEqual(self._columns_on_page(url), expected, url)

    def test_the_targets_table_is_just_a_gene(self):
        self.assertEqual(self._columns_on_page("/pipeline/targets/board/"), ["gene"])

    def test_columns_and_example_line_up_for_every_kind(self):
        """A shorter example than column list silently mislabels the placeholders."""
        for kind in _KINDS:
            cols, example = columns_and_example(kind)
            self.assertTrue(cols, kind)
            self.assertEqual(len(cols), len(example),
                             f"{kind}: {len(cols)} columns but {len(example)} example values")

    def test_an_unknown_kind_returns_empty_rather_than_raising(self):
        self.assertEqual(columns_and_example("nonsense"), ([], []))

    def test_the_parsers_accept_what_the_table_sends(self):
        """The table sends a header row then tab-separated data. Feed each parser
        exactly that and check it produces rows — bulk_cell_lines returns nothing
        without the header, which is why the header is always sent."""
        from pipeline.services import (bulk_antibodies, bulk_cell_lines,
                                       bulk_sessions)
        cases = [
            (bulk_antibodies, "antibodies", ["SOD1", "12A8", "Proteintech"]),
            (bulk_cell_lines, "cell-lines", ["HAP1-WT", "", "WT"]),
            (bulk_sessions, "sessions", ["12A8", "Proteintech"]),
        ]
        for service, kind, values in cases:
            cols, _ = columns_and_example(kind)
            row = values + [""] * (len(cols) - len(values))
            tsv = "\t".join(cols) + "\n" + "\t".join(row)
            rows = service.parse(tsv)
            self.assertEqual(len(rows), 1,
                             f"{kind}: parser returned {len(rows)} rows for one data row")

    def test_a_header_only_sheet_creates_nothing(self):
        """An empty table must not post a phantom row built from the headings."""
        from pipeline.services import bulk_antibodies
        cols, _ = columns_and_example("antibodies")
        self.assertEqual(bulk_antibodies.parse("\t".join(cols)), [])
