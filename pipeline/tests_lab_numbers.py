"""A-numbers and C-numbers: issued per bench, never invented, never reused.

What is pinned here is the set of ways this could go wrong **silently** — a
number written to the wrong bench's run, a typed number quietly replaced by an
issued one, a blank record from 2019 minted a number no freezer agrees with.
Each of those is invisible on screen: the board would show a plausible number,
and the only way to find out it was the wrong one is to walk to the freezer.

Deliberately not pinned: that the antibodies board draws the column, or the
wording of a refusal. Those fail loudly the first time anybody looks.
"""
from __future__ import annotations

from django.test import TestCase

from pipeline.models import (Antibody, CellLine, CellLineVial, Company, Member,
                            Site, Target)
from pipeline.services import bulk_antibodies, bulk_cell_lines, lab_numbers
from pipeline.tests_timeouts import DB, _member_client

ANTIBODY = lab_numbers.ANTIBODY
CELL_LINE = lab_numbers.CELL_LINE


class ANumberBelongsToABenchTests(TestCase):
    """McGill's A-1 and Leicester's A-1 are different antibodies, and always
    were: each bench keeps its own freezer and its own run of numbers."""

    databases = {DB}

    @classmethod
    def setUpTestData(cls):
        cls.mcgill = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        cls.leicester = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        cls.target = Target.objects.using(DB).create(gene_name="STMN2")
        cls.company = Company.objects.using(DB).create(name="Proteintech")

    def _ab(self, site, catalogue, **kw):
        return Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number=catalogue, site_id=site.pk, **kw)

    def test_each_site_runs_its_own_numbers(self):
        a = self._ab(self.mcgill, "one")
        b = self._ab(self.leicester, "two")
        self.assertEqual((a.ab_number, b.ab_number), (1, 1))

    def test_a_site_continues_from_what_it_already_has(self):
        """McGill's Access run reaches A-3084, so its next antibody is A-3085 —
        not A-1, and not one past whatever Leicester happens to be on."""
        self._ab(self.mcgill, "old", ab_number=3084)
        self._ab(self.leicester, "theirs", ab_number=9)
        self.assertEqual(self._ab(self.mcgill, "new").ab_number, 3085)

    def test_a_record_with_no_bench_gets_no_number(self):
        """`next` is not a question with an answer until you know whose bench is
        asking, and a number under no site would be a number in no run."""
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, catalogue_number="homeless")
        self.assertIsNone(ab.ab_number)

    def test_a_gap_in_the_middle_of_the_run_is_not_filled(self):
        """The box a deleted record was written on may still be in the freezer,
        so a hole in the run is left as a hole rather than handed to the next
        antibody along. (Deleting the *highest* record does free its number
        again — see `next_number`, which says so.)"""
        self._ab(self.mcgill, "one")
        middle = self._ab(self.mcgill, "two")
        self._ab(self.mcgill, "three")
        middle.delete()
        self.assertEqual(self._ab(self.mcgill, "four").ab_number, 4)


class BlankDataStaysBlankTests(TestCase):
    """The rows with no number are Leicester's — that bench never used the
    convention, so its old records are not missing a number, they simply do not
    have one (owner, 4 Aug 2026). Nothing backfills them, and the way that could
    happen by accident is an ordinary edit."""

    databases = {DB}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        cls.target = Target.objects.using(DB).create(gene_name="STMN2")

    def test_saving_an_old_record_again_does_not_mint_one(self):
        with lab_numbers.suspended():
            ab = Antibody.objects.using(DB).create(
                target_id=self.target.pk, catalogue_number="old",
                site_id=self.site.pk)
        ab.lot_number = "L2"
        ab.save(using=DB)
        ab.refresh_from_db(using=DB)
        self.assertIsNone(ab.ab_number)

    def test_a_historical_import_issues_nothing(self):
        with lab_numbers.suspended():
            line = CellLine.objects.using(DB).create(
                name="HAP1", genotype="WT", site_id=self.site.pk)
        self.assertIsNone(line.c_number)


class ATypedNumberWinsTests(TestCase):
    """"Unless the user specifies a different number" — and the way that breaks
    is not a refusal, it is the typed number being accepted and then overtaken
    by an issued one, which nothing on screen would show."""

    databases = {DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).first()
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    HEADER = "name\tgene\tgenotype\tparent\tc number\tsite"

    def test_a_pasted_c_number_is_what_the_line_gets(self):
        """The line's number used to be filled in *after* the save, by the
        freeze-down batch bridging up to it. That only fills a blank — and the
        column is no longer blank at save time, so the tube would have said
        C-42 and the record something else entirely."""
        rows = bulk_cell_lines.parse(
            f"{self.HEADER}\nHAP1\tNA\tWT\t\tC-42\tLeicester")
        bulk_cell_lines.apply(rows, create_targets=True, member=self.member)
        line = CellLine.objects.using(DB).get(name="HAP1")
        self.assertEqual(line.c_number, 42)

    def test_a_pasted_a_number_is_what_the_antibody_gets(self):
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\tab #\nSTMN2\tab1\tAbcam\tA-77")
        bulk_antibodies.apply(rows, create_targets=True, member=self.member)
        ab = Antibody.objects.using(DB).get(catalogue_number="ab1")
        self.assertEqual(ab.ab_number, 77)

    def test_a_bare_number_in_the_ab_column_is_left_alone(self):
        """Every antibodies sheet downloaded before this printed the *record*
        id in that column. Reading one back as an A-number would relabel the row
        with a number nobody typed, so only the `A` makes it a lab number."""
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\tab #\nSTMN2\tab2\tAbcam\t914")
        bulk_antibodies.apply(rows, create_targets=True, member=self.member)
        ab = Antibody.objects.using(DB).get(catalogue_number="ab2")
        self.assertEqual(ab.ab_number, 1, "issued, not read off a record id")

    def test_a_c_number_nobody_could_read_leaves_the_cell_empty(self):
        """The row is still written — an odd batch label is no reason to discard
        a good cell line — and the number stays blank. Issuing one here would
        file the row under a number that contradicts the tube, which is the same
        failure as `C-RUN11-01` becoming C-11, one door along."""
        rows = bulk_cell_lines.parse(
            f"{self.HEADER}\nHAP1\tNA\tWT\t\tC-RUN11-01\tLeicester")
        out = bulk_cell_lines.apply(rows, create_targets=True, member=self.member)
        self.assertEqual(len(out["created"]), 1)
        line = CellLine.objects.using(DB).get(name="HAP1")
        self.assertIsNone(line.c_number)


class OneNumberMeansOneRecordTests(TestCase):
    """Two records answering to one number at one bench cannot be told apart on
    a freezer box, which is the whole job of the number."""

    databases = {DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).first()
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    def test_a_pasted_c_number_already_in_use_blocks_the_row(self):
        CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                          site_id=self.site.pk, c_number=42)
        rows = bulk_cell_lines.parse(
            "name\tgene\tgenotype\tparent\tc number\tsite\n"
            "U2OS\tNA\tWT\t\tC-42\tLeicester")
        out = bulk_cell_lines.apply(rows, create_targets=True, member=self.member)
        self.assertEqual(out["created"], [])
        self.assertFalse(CellLine.objects.using(DB).filter(name="U2OS").exists())

    def test_an_issued_c_number_steps_over_a_freeze_down_batch(self):
        """`services/cell_lines.py::by_c_number` reads the vial table as well as
        the line's own column — 102 of the 145 C-numbered parents on file are a
        vial's number — so a number issued past only the lines would land on top
        of somebody's batch and the two would be indistinguishable."""
        with lab_numbers.suspended():
            line = CellLine.objects.using(DB).create(
                name="HAP1", genotype="WT", site_id=self.site.pk)
        CellLineVial.objects.using(DB).create(cell_line=line, c_number=200)
        fresh = CellLine.objects.using(DB).create(
            name="U2OS", genotype="WT", site_id=self.site.pk)
        self.assertEqual(fresh.c_number, 201)

    def test_the_board_refuses_an_a_number_another_vial_holds(self):
        company = Company.objects.using(DB).create(name="Abcam")
        held = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab1", site_id=self.site.pk)
        other = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab2", site_id=self.site.pk)
        resp = self.client.post("/pipeline/antibodies/board/patch/", {
            "antibody_id": other.pk, "field": "ab_number",
            "value": lab_numbers.label(held.ab_number, kind=ANTIBODY)})
        self.assertEqual(resp.status_code, 400)
        # Named, not "could not save that" — the row in the way, and the way out.
        self.assertIn("ab1", resp.json()["error"])
        other.refresh_from_db(using=DB)
        self.assertEqual(other.ab_number, 2)


class TheAppSaysWhenItGivesOutANumberTests(TestCase):
    """The first field test: *"the new antibody was auto-assigned A-1 even
    though I left ab # blank — probably deliberate, but nothing in the preview
    or the save message mentions that a lab reference number has been minted."*

    A number the app invents goes on a freezer box, so it is not something to
    find out about later by looking at the board."""

    databases = {DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).first()
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    def test_the_check_says_a_number_is_coming(self):
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\nSTMN2\tab1\tAbcam")
        items = bulk_antibodies.plan(rows, False, member=self.member)
        self.assertEqual(bulk_antibodies.summarize(items)["will_be_numbered"], 1)

    def test_the_check_does_not_promise_one_for_a_row_that_typed_its_own(self):
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\tab #\nSTMN2\tab1\tAbcam\tA-77")
        items = bulk_antibodies.plan(rows, False, member=self.member)
        self.assertEqual(bulk_antibodies.summarize(items)["will_be_numbered"], 0)

    def test_the_save_names_the_numbers_it_gave_out(self):
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\nSTMN2\tab1\tAbcam")
        out = bulk_antibodies.apply(rows, create_targets=True, member=self.member)
        self.assertEqual([d["number"] for d in out["numbers_issued"]], ["A-1"])
        self.assertEqual(out["numbers_issued"][0]["name"], "ab1")

    def test_a_cell_line_save_says_so_too(self):
        rows = bulk_cell_lines.parse(
            "name\tgene\tgenotype\tsite\nHAP1\tNA\tWT\tLeicester")
        out = bulk_cell_lines.apply(rows, create_targets=True, member=self.member)
        self.assertEqual([d["number"] for d in out["numbers_issued"]], ["C-1"])

    def test_a_number_the_reader_typed_is_not_reported_as_given_out(self):
        """It is not news: they wrote it. The message is for numbers the app
        invented."""
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\tab #\nSTMN2\tab1\tAbcam\tA-77")
        out = bulk_antibodies.apply(rows, create_targets=True, member=self.member)
        self.assertEqual(out["numbers_issued"], [])


class TheSupplierClaimIsWiderThanOGAsVerdictTests(TestCase):
    """The first field test, third finding: *"I wrote WB, IHC in supplier
    recommendations; it stored WB and dropped IHC without a word."*

    `Antibody.supplier_validated_ihc` has been a column since the Access
    import — every list in the chain simply stopped at four, so the claim was
    lost on a column whose only job is to record the claim."""

    databases = {DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).first()
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    def test_ihc_is_kept(self):
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\tsupplier recommendations\n"
            "STMN2\tab1\tAbcam\tWB, IHC")
        bulk_antibodies.apply(rows, create_targets=True, member=self.member)
        ab = Antibody.objects.using(DB).get(catalogue_number="ab1")
        self.assertTrue(ab.supplier_validated_wb)
        self.assertTrue(ab.supplier_validated_ihc)

    def test_the_sheet_and_the_board_both_show_it(self):
        from pipeline.services import antibody_board, board_columns
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, catalogue_number="ab2",
            site_id=self.site.pk, supplier_validated_wb=True,
            supplier_validated_ihc=True)
        col = next(c for c in board_columns.ANTIBODIES
                   if c.key == "supplier_validated")
        self.assertEqual(col.value(ab), "WB, IHC")
        self.assertTrue(antibody_board.row_for(ab)["supplier_validated"]["ihc"])

    def test_oga_s_own_verdict_stays_the_four_it_tests(self):
        """The column beside it is a different question. OGA characterises four
        applications, so a fifth in that list would be a verdict about work
        nobody did."""
        from pipeline.services import antibody_board
        self.assertEqual(antibody_board.APPLICATIONS, ["wb", "ip", "if", "fc"])

    def test_the_check_says_what_it_could_not_read(self):
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\tsupplier recommendations\n"
            "STMN2\tab3\tAbcam\tWB, dot blot")
        items = bulk_antibodies.plan(rows, False, member=self.member)
        self.assertIn("stored as WB", items[0]["note"])


class TheNumberOnScreenGoesBackInTests(TestCase):
    """The cell shows `A-118` and an edit types that string back, so the reader
    of the value and the writer of it have to agree about the prefix. They did
    not for the supplier column, and it took two field tests."""

    databases = {DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    def test_an_a_number_round_trips_through_the_cell(self):
        from pipeline.services import antibody_board
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, catalogue_number="ab1",
            site_id=self.site.pk)
        shown = antibody_board.row_for(ab)["ab_number"]
        self.assertEqual(shown, "A-1")
        resp = self.client.post("/pipeline/antibodies/board/patch/", {
            "antibody_id": ab.pk, "field": "ab_number", "value": shown})
        self.assertEqual(resp.status_code, 200, resp.content)
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.ab_number, 1)

    def test_a_c_number_round_trips_through_the_cell(self):
        from pipeline.services import cell_line_board
        line = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk)
        shown = cell_line_board.row_for(line)["c_number"]
        self.assertEqual(shown, "C-1")
        resp = self.client.post("/pipeline/cell-lines/board/patch/", {
            "cell_line_id": line.pk, "field": "c_number", "value": shown})
        self.assertEqual(resp.status_code, 200, resp.content)
        line.refresh_from_db(using=DB)
        self.assertEqual(line.c_number, 1)

    def test_emptying_the_cell_clears_the_number(self):
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, catalogue_number="ab1",
            site_id=self.site.pk)
        resp = self.client.post("/pipeline/antibodies/board/patch/", {
            "antibody_id": ab.pk, "field": "ab_number", "value": ""})
        self.assertEqual(resp.status_code, 200, resp.content)
        ab.refresh_from_db(using=DB)
        self.assertIsNone(ab.ab_number, "an emptied cell means 'not written down'")


class TheSheetsAgreeAboutWhatANumberIsTests(TestCase):
    """A bench sheet's `Ab#` column has always held a *record* id, and now also
    holds A-numbers. Only the prefix separates them, so the writer and the
    reader have to use the same one."""

    databases = {DB}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        cls.target = Target.objects.using(DB).create(gene_name="STMN2")

    def test_an_unnumbered_row_still_finds_its_way_home(self):
        """Taking the record id out of the ``Ab#`` cell took away the key the
        bench-sheet upload used to match on, for exactly the rows that have no
        A-number. Supplier and catalogue are on every row of every one of these
        sheets, and the session's own antibodies are the set to choose from —
        which is narrower than the record id ever was."""
        from pipeline.services.bench_results import _resolve_ab
        from pipeline.models import Company
        company = Company.objects.using(DB).create(name="Proteintech")
        with lab_numbers.suspended():
            ab = Antibody.objects.using(DB).create(
                target_id=self.target.pk, company_id=company.pk,
                catalogue_number="NB110-40763", site_id=self.site.pk)
        self.assertEqual(lab_numbers.sheet_number(ab), "")
        self.assertEqual(
            _resolve_ab(self.target, "", "NB110-40763", "Proteintech"), ab)

    def test_a_prefixed_cell_resolves_by_lab_number_not_by_record_id(self):
        from pipeline.services.bench_results import _resolve_ab
        decoy = Antibody.objects.using(DB).create(
            target_id=self.target.pk, catalogue_number="decoy",
            site_id=self.site.pk)
        wanted = Antibody.objects.using(DB).create(
            target_id=self.target.pk, catalogue_number="wanted",
            site_id=self.site.pk)
        # `wanted` is A-2; `decoy` is record id 1 and A-1. A sheet saying `A-2`
        # means the antibody the lab calls A-2, whatever its record id is.
        self.assertEqual(wanted.ab_number, 2)
        self.assertEqual(
            _resolve_ab(self.target, lab_numbers.label(2, kind=ANTIBODY), "", ""),
            wanted)
        self.assertEqual(_resolve_ab(self.target, str(decoy.pk), "", ""), decoy)

    def test_a_row_with_no_number_prints_nothing_rather_than_its_record_id(self):
        """The first field test's finding, at the source.

        `ab #` fell back to the record id so no cell was ever blank, and that
        put `4550` in front of a reader for a vial the board called *not
        numbered* — a value they would write on a tube, that is not the vial's
        identifier, and that the upload ignores on the way back. Reading a bare
        integer as a record id stays, because sheets printed before this are on
        people's disks; nothing writes one."""
        with lab_numbers.suspended():
            old = Antibody.objects.using(DB).create(
                target_id=self.target.pk, catalogue_number="old",
                site_id=self.site.pk)
        new = Antibody.objects.using(DB).create(
            target_id=self.target.pk, catalogue_number="new",
            site_id=self.site.pk)
        self.assertEqual(lab_numbers.sheet_number(old), "")
        self.assertEqual(lab_numbers.sheet_number(new), "A-1")
        self.assertEqual(lab_numbers.read_reference("A-1"), ("number", 1))
        self.assertEqual(lab_numbers.read_reference(str(old.pk)),
                         ("record", old.pk))
