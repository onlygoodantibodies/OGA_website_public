"""A C-number lives on two tables, and both are written on tubes.

`CellLine.c_number` is the line's own label; `CellLineVial.c_number` is the
freeze-down batch's. On live data (16 Aug 2026) **219 of the 746 vial numbers —
29% — appear on no `CellLine` row at all**, and they run consecutively with the
line's, so a line is C-23 and its own vial is C-24. Searching the first worked
and the second returned nothing, from a number somebody had just read off a box
in the freezer.

`services/cell_lines.py::by_c_number` had always read both tables. `find.py`
read one. That is the same two-readers split the header of `find.py` is about,
one level further down, and it reaches every Search box in the app because all
four boards use these builders.
"""
from __future__ import annotations

from django.test import TestCase

from pipeline.models import CellLine, CellLineVial, Site
from pipeline.services import find
from pipeline.tests_timeouts import DB


class ACNumberOnAVialIsFindableTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        # The live shape: the line carries one number, its freeze-down batch the
        # next one along, and nothing else on file carries the vial's.
        cls.line = CellLine.objects.using(DB).create(
            name="HCT116", genotype="KO", c_number=23, site_id=cls.site.pk)
        CellLineVial.objects.using(DB).create(cell_line_id=cls.line.pk, c_number=24)

    def _hits(self, term):
        return list(CellLine.objects.using(DB).filter(find.cell_line_q(term)))

    def test_the_lines_own_c_number_is_findable(self):
        for term in ("23", "C-23", "c23"):
            with self.subTest(term=term):
                self.assertEqual([c.pk for c in self._hits(term)], [self.line.pk])

    def test_a_freeze_down_vials_c_number_is_findable(self):
        """The half that was missing. C-24 is on a tube; it was on no screen."""
        for term in ("24", "C-24", "c24"):
            with self.subTest(term=term):
                self.assertEqual([c.pk for c in self._hits(term)], [self.line.pk],
                                 f"{term} reached no cell line")

    def test_a_line_with_several_vials_is_listed_once(self):
        """The clauses reach `vials`, so without `.distinct()` a line with three
        batches is three rows — and the count above the list is the first thing
        a reader checks it against."""
        line = CellLine.objects.using(DB).create(
            name="U2OS", genotype="WT", c_number=90, site_id=self.site.pk)
        for n in (91, 92, 93):
            CellLineVial.objects.using(DB).create(cell_line_id=line.pk, c_number=n)
        rows = list(find._cell_lines("9"))
        self.assertEqual(len(rows), len({r.pk for r in rows}),
                         "a cell line was listed once per vial")

    def test_a_number_matching_nothing_finds_nothing(self):
        self.assertEqual(self._hits("C-4242"), [])

    def test_the_receipt_says_a_vials_number_is_why_it_matched(self):
        """A row surfacing on a number its own C-number column does not hold is
        unexplainable otherwise — the line says C-23 and you searched C-24. The
        rule is that widening the builder and widening `_why` happen together."""
        row = find._row("cell_lines", self.line, "C-24")
        self.assertIn("C-24", row["why"],
                      f"the hit did not explain itself: {row['why']!r}")

    def test_a_hit_on_the_lines_own_number_still_says_so(self):
        row = find._row("cell_lines", self.line, "C-23")
        self.assertIn("C-23", row["why"])
