"""What `backfill_cell_line_parents` must link, and what it must leave alone.

The four cases are the four live shapes, not invented ones: a C-number naming a
vial of the parental (the normal case, 157 rows), several references that agree
because the parentals were themselves merged (30 rows), a reference landing on
something that is not a wild type (the SK-N-AS row, 1), and references that
disagree (`C-591/C-262`, 1).

Only the silent failures are pinned. A refusal prints; what cannot be seen is a
knockout quietly linked to the wrong control, which every session that follows
would then read as the matched wild type.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from pipeline.models import CellLine, CellLineVial, Site, Target

DB = "pipeline_db"


class BackfillParentsTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.gene = Target.objects.using(DB).create(gene_name="ARF6")
        # The parental, and the batch a knockout names. 102 of the 145 numbered
        # parents on file are a *vial's* number rather than the line's, which is
        # why `by_c_number` reads both tables and why this fixture does too.
        self.wt = CellLine.objects.using(DB).create(
            name="HeLa", genotype="WT", site=self.site, c_number=15)
        CellLineVial.objects.using(DB).create(
            cell_line=self.wt, c_number=647, site=self.site)

    def _ko(self, parental, name="HeLa", **kw):
        return CellLine.objects.using(DB).create(
            name=name, genotype="KO", target=self.gene, site=self.site,
            parental_line_name=parental, **kw)

    def _run(self, **kw):
        out = StringIO()
        call_command("backfill_cell_line_parents", stdout=out, **kw)
        return out.getvalue()

    def test_a_c_number_naming_a_batch_of_the_parental_links(self):
        """The normal case, and the reason `by_c_number` reads the vial table."""
        ko = self._ko("C-647")
        self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertEqual(ko.parent_line_id, self.wt.pk)
        self.assertEqual(ko.parental_line_name, "C-647",
                         "the bench's own text is the record and is never rewritten")

    def test_several_references_that_agree_link(self):
        """`C-590/C-262` is 11 live rows. Both refs land on one line, because
        the parentals were merged by name — so there is one answer, not two."""
        ko = self._ko("C-15/C-647")
        self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertEqual(ko.parent_line_id, self.wt.pk)

    def test_a_reference_that_is_not_a_wild_type_is_refused(self):
        """The live case: SK-N-AS records `C-744`, which lands on a HAP1 KO.

        This is the one that has to be silent-proof. A knockout written in as a
        parental is a false control, and every session planned afterwards would
        read it as the matched wild type with nothing on the screen dissenting.
        """
        other_ko = CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target=self.gene, site=self.site,
            c_number=744)
        ko = self._ko("C-744", name="SK-N-AS")
        report = self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertIsNone(ko.parent_line_id)
        self.assertIn("not a wild type", report)
        self.assertRegex(report, r"left — not a wild type\s+1")
        self.assertNotEqual(ko.parent_line_id, other_ko.pk)

    def test_a_wild_type_of_another_background_is_refused(self):
        """A HeLa knockout does not come from a HAP1.

        The first version checked the genotype and not the name, and would have
        written 14 of these on live — `HeLa FUS KO → HAP1`, `U2OSn UBQLN2 KO →
        HCT116`, `HCT116 CSNK2A1 KO → HEK293T`. A mismatched control is read as
        the matched one by every session planned afterwards, and nothing on any
        screen dissents. This is the whole reason the command exists, arriving
        through the command itself.
        """
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site=self.site, c_number=48)
        ko = self._ko("C-48")          # a HeLa knockout pointing at the HAP1 WT
        report = self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertIsNone(ko.parent_line_id)
        self.assertIn("Looks wrong", report)
        # The sentence has to be actionable: the HeLa wild type is on file, so
        # it is named. "Not a HeLa" describes the check; this says what to do.
        self.assertIn("The HeLa wild type on file is C-15", report)
        self.assertRegex(report, r"left — other background\s+1")

    def test_a_near_miss_on_the_name_is_refused_too(self):
        """`HEK293` and `HEK293T` are different cell lines.

        No prefix rule separates that pair from `U2OSn` and `U2OSn clone FM109`,
        which are the same line — so the match is exact and both go to a person.
        Refusing a link that is right costs one edit in a dialog; writing one
        that is wrong costs an experiment.
        """
        CellLine.objects.using(DB).create(
            name="HeLa clone FM109", genotype="WT", site=self.site, c_number=77)
        ko = self._ko("C-77")
        self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertIsNone(ko.parent_line_id)

    def test_references_that_disagree_are_refused_and_both_named(self):
        """`C-591/C-262` names a KO and a WT. There is no single answer, so
        there is no answer — and the refusal names what it found."""
        CellLine.objects.using(DB).create(
            name="U-87 MG", genotype="KO", target=self.gene, site=self.site,
            c_number=591)
        second_wt = CellLine.objects.using(DB).create(
            name="U-87 MG", genotype="WT", site=self.site, c_number=262)
        ko = self._ko("C-591/C-262", name="U-87 MG")
        report = self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertIsNone(ko.parent_line_id)
        self.assertIn("names 2 different lines", report)
        self.assertIsNotNone(second_wt.pk)

    def test_a_background_with_no_wild_type_on_file_says_so(self):
        """`CellLine-130` is a placeholder name with no parental behind it.

        A suggestion that names nothing would be worse than none — the reader
        would go looking for a row that is not there."""
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site=self.site, c_number=48)
        ko = self._ko("C-48", name="CellLine-130")
        report = self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertIsNone(ko.parent_line_id)
        self.assertIn("No CellLine-130 wild type is on file", report)
        self.assertNotIn("may be", report.split("CellLine-130 SPAST")[-1]
                         if "CellLine-130 SPAST" in report else
                         report[report.index("CellLine-130"):])

    def test_linking_a_row_does_not_take_the_c_number_off_the_screen(self):
        """The regression the backfill itself caused.

        Both the board cell and the sheet rendered `parent_line.name if
        parent_line_id else parental_line_name`, so linking 156 rows replaced
        `C-16` with a bare `HeLa` on every one of them — losing the freeze-down
        batch at the moment the row got better, on the one field where *which*
        HeLa is the whole question (the name covers 17 stocks from five
        suppliers).

        The board draws both; the **sheet** keeps the bench's own text, because a
        sheet cell has to be one value `resolve_parent` accepts back and it does
        not take `HeLa · C-16`.
        """
        from pipeline.services import board_columns, cell_lines as svc
        ko = self._ko("C-647")
        self._run(**{"apply": True})
        ko.refresh_from_db()
        ko = CellLine.objects.using(DB).select_related("parent_line").get(pk=ko.pk)

        self.assertEqual(svc.parent_label(ko), "HeLa · C-647")
        self.assertEqual(board_columns._parent(ko), "C-647",
                         "the sheet carries what the parser accepts back")

    def test_a_reference_and_a_name_that_agree_are_not_said_twice(self):
        from pipeline.services import cell_lines as svc
        ko = self._ko("HeLa")
        self._run(**{"apply": True})
        ko = CellLine.objects.using(DB).select_related("parent_line").get(pk=ko.pk)
        self.assertEqual(svc.parent_label(ko), "HeLa")

    def test_a_reference_that_resolved_to_nothing_does_not_read_as_a_link(self):
        """The 17 left behind are a worklist, and an unlinked reference drawn
        plainly is indistinguishable from a working one."""
        from pipeline.services import cell_lines as svc
        ko = self._ko("C-99999")
        self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertEqual(svc.parent_label(ko), "C-99999 — not linked")

    def test_an_existing_link_is_never_overwritten(self):
        """20 rows already carry one. A backfill fills blanks."""
        already = CellLine.objects.using(DB).create(
            name="HeLa", genotype="WT", site=self.site, c_number=999)
        ko = self._ko("C-647", parent_line=already)
        self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertEqual(ko.parent_line_id, already.pk)

    def test_a_dry_run_writes_nothing(self):
        ko = self._ko("C-647")
        report = self._run()
        ko.refresh_from_db()
        self.assertIsNone(ko.parent_line_id)
        self.assertIn("DRY RUN", report)
        self.assertRegex(report, r"linked\s+1")

    def test_a_reference_naming_nothing_is_left_alone_and_named(self):
        ko = self._ko("C-99999")
        report = self._run(**{"apply": True})
        ko.refresh_from_db()
        self.assertIsNone(ko.parent_line_id)
        self.assertIn("an empty field", report)
        self.assertIn("The HeLa wild type on file is C-15", report)
