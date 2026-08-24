"""What would be *silently* wrong about the August 2026 Access delta import.

Each test here stands for a defect that writes plausible-looking rows and
contradicts nothing on screen. The loud failures — a missing CSV, a bad date —
are not pinned: they announce themselves the first time anybody runs the
command.

The fixtures are hand-written CSVs rather than the real delta, so a test says
what it is about in the rows above the assertion.
"""

import csv
import os
import tempfile
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pipeline.models import (
    Site, Member, Company, Target, Report, CellLine, CellLineVial,
    Antibody, ExperimentSession, WbResult,
)

DB = "pipeline_db"

ANTIBODY_COLUMNS = [
    "ID", "ProteinsID", "AbNumber", "BoxNumber", "Fridge4C", "Neg80C",
    "CompaniesID", "CatNumber", "Lot", "RRID", "Clonality", "Clone",
    "ExpressionSystem", "ConcentrationInUgUl", "PurchasedORinKind",
    "ReceivedDate", "Comments", "RRIDlink",
]
CELL_LINE_COLUMNS = [
    "ID", "CellLine", "WTorKO", "ProteinsID", "LabLabel", "Origin",
    "OriginComments", "CatNumber", "Lot", "ParentalLine", "Species",
    "ReceivedDate", "PurchasedOrInKind", "KOconfirmed", "KOInfo", "RRID",
]
WB_COLUMNS = [
    "ID", "AntibodiesID", "MembersID", "SpecificSignal", "SelectiveSignal",
    "When", "1AbDilution", "lane1CellLineID", "lane2CellLineID",
    "lane3CellLineID", "lane4CellLineID", "Comments",
]
CHANGED = ["ID", "Column", "OldValue", "NewValue"]


class AccessDeltaTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.site = Site.objects.using(DB).create(
            name="Montreal", short_code="MTL", is_active=True)
        self.company = Company.objects.using(DB).create(name="abcam")
        self.target = Target.objects.using(DB).create(
            protein_name="Tripartite motif-containing protein 2",
            gene_name="TRIM2", access_id=468, site_id=self.site.pk)

        user = User.objects.using(DB).create(username="access_sara_gonzalez_bolivar")
        self.member = Member.objects.using(DB).create(
            user_id=user.pk, site_id=self.site.pk, role="experimenter",
            display_name="Sara Gonzalez Bolivar")

        self._write("Companies.csv", ["ID", "Company"], [
            {"ID": "1", "Company": "abcam"}])
        self._write("Members.csv", ["ID", "Name"], [
            {"ID": "6", "Name": "Sara Gonzalez Bolivar"}])
        for name, columns in (("Antibodies_new.csv", ANTIBODY_COLUMNS),
                              ("CellLines_new.csv", CELL_LINE_COLUMNS),
                              ("Wb_new.csv", WB_COLUMNS),
                              ("IF_new.csv", ["ID"]), ("IP_new.csv", ["ID"])):
            self._write(name, columns, [])
        for name in ("Antibodies_changed.csv", "CellLines_changed.csv",
                     "Wb_changed.csv", "IF_changed.csv", "Proteins_changed.csv"):
            self._write(name, CHANGED, [])

    def _write(self, name, columns, rows):
        with open(os.path.join(self.dir, name), "w", newline="",
                  encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in columns})

    def _run(self, command="import_access_update", **kwargs):
        out = StringIO()
        call_command(command, csv_dir=self.dir, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    # ------------------------------------------------------------------
    # The RRID lives in the hyperlink on every new row
    # ------------------------------------------------------------------
    def test_rrid_is_read_from_the_hyperlink_when_the_rrid_column_is_empty(self):
        """All 41 new antibodies have a blank `RRID`; 30 carry one in `RRIDlink`.

        Reading `RRID` alone is the right rule for the *existing* rows, whose
        `RRIDlink` the export corrupted — and applying it to the new ones drops
        30 identifiers with nothing on any screen to say so.
        """
        self._write("Antibodies_new.csv", ANTIBODY_COLUMNS, [
            {"ID": "4103", "ProteinsID": "468", "AbNumber": "3087",
             "CompaniesID": "1", "CatNumber": "ab12345", "RRID": "",
             "RRIDlink": "AB_3741488#https://www.antibodyregistry.org/AB_3741488#"},
        ])
        self._run(apply=True)
        antibody = Antibody.objects.using(DB).get(ab_number=3087)
        self.assertEqual(antibody.rrid, "AB_3741488")
        self.assertEqual(antibody.rrid_link,
                         "https://www.antibodyregistry.org/AB_3741488")

    def test_a_placeholder_hyperlink_is_not_stored_as_an_rrid(self):
        """Two rows hold `NA#http://NA#`, which is a URL and is not an RRID.

        The enriched CSV in the update pack turns it into `http://NA` and calls
        it clean; stored, it is a registry link that resolves to nothing.
        """
        self._write("Antibodies_new.csv", ANTIBODY_COLUMNS, [
            {"ID": "4107", "ProteinsID": "468", "AbNumber": "3091",
             "CompaniesID": "1", "CatNumber": "ab999", "RRID": "",
             "RRIDlink": "NA#http://NA#"},
        ])
        output = self._run(apply=True)
        antibody = Antibody.objects.using(DB).get(ab_number=3091)
        self.assertEqual(antibody.rrid, "")
        self.assertEqual(antibody.rrid_link, "")
        self.assertIn("antibodies with no RRID", output)

    # ------------------------------------------------------------------
    # One Access cell-line row is a freeze-down batch, not a cell line
    # ------------------------------------------------------------------
    def test_repeated_freeze_downs_of_one_line_stay_one_line(self):
        """Nine of the nineteen new rows are one SK-N-AS knockout, frozen nine
        times. A row-per-line import would split one line's freezer across nine
        records, each looking complete, and `session_options` would offer nine
        identical knockouts to pick between.
        """
        self._write("CellLines_new.csv", CELL_LINE_COLUMNS, [
            {"ID": str(892 + i), "CellLine": "SK-N-AS", "WTorKO": "KO",
             "ProteinsID": "468", "LabLabel": str(755 + i), "Species": "Human"}
            for i in range(9)
        ])
        self._run(apply=True)
        lines = CellLine.objects.using(DB).filter(name="SK-N-AS", genotype="KO")
        self.assertEqual(lines.count(), 1)
        vials = CellLineVial.objects.using(DB).filter(cell_line_id=lines.first().pk)
        self.assertEqual(vials.count(), 9)
        self.assertEqual(sorted(v.c_number for v in vials), list(range(755, 764)))

    def test_a_c_number_the_lab_wrote_is_kept_exactly(self):
        """`lab_numbers` issues a number on `pre_save`. A batch that arrived
        carrying C-745 must keep it: a number minted here is one no tube in the
        freezer agrees with, and the run would then skip 745.
        """
        self._write("CellLines_new.csv", CELL_LINE_COLUMNS, [
            {"ID": "870", "CellLine": "HAP1", "WTorKO": "KO",
             "ProteinsID": "468", "LabLabel": "745", "Species": "Human"},
        ])
        self._run(apply=True)
        vial = CellLineVial.objects.using(DB).get(access_id=870)
        self.assertEqual(vial.c_number, 745)

    # ------------------------------------------------------------------
    # Corrections
    # ------------------------------------------------------------------
    def test_a_blank_in_the_export_never_clears_a_stored_value(self):
        """C9orf72's Zenodo DOI is blank in the new export and is a real eLife
        DOI on the site. Propagating the blank removes a published paper's
        address from the gene page, and nothing says it went.
        """
        target = Target.objects.using(DB).create(
            protein_name="Guanine nucleotide exchange C9orf72",
            gene_name="C9orf72", access_id=363, site_id=self.site.pk)
        Report.objects.using(DB).create(
            target_id=target.pk, status="published",
            zenodo_doi="https://doi.org/10.7554/eLife.48363")
        self._write("Proteins_changed.csv", CHANGED, [
            {"ID": "363", "Column": "ZenodoDOI",
             "OldValue": "https://doi.org/10.7554/eLife.48363", "NewValue": ""},
        ])
        output = self._run(apply=True)
        report = Report.objects.using(DB).get(target_id=target.pk)
        self.assertEqual(report.zenodo_doi, "https://doi.org/10.7554/eLife.48363")
        self.assertIn("the export blanked", output)

    def test_a_correction_is_refused_when_the_site_has_moved_on(self):
        """The export says what it believed the site held. Where the site holds
        something else somebody edited it on a board since March, and a diff
        generated in August is not the authority on a change made in July.
        """
        antibody = Antibody.objects.using(DB).create(
            access_id=127, ab_number=100, target_id=self.target.pk,
            company_id=self.company.pk, catalogue_number="ab1",
            comments="corrected by hand in July", site_id=self.site.pk)
        self._write("Antibodies_changed.csv", CHANGED, [
            {"ID": "127", "Column": "Comments",
             "OldValue": "what March held", "NewValue": "what August says"},
        ])
        output = self._run(apply=True)
        antibody.refresh_from_db(using=DB)
        self.assertEqual(antibody.comments, "corrected by hand in July")
        self.assertIn("the site has moved on", output)

    def test_an_identity_change_is_named_rather_than_applied(self):
        """Lot is part of `unique_antibody_per_site_lot`. Retyping it in place
        makes the row a different vial while every reading stays attached.
        """
        Antibody.objects.using(DB).create(
            access_id=3929, ab_number=3073, target_id=self.target.pk,
            company_id=self.company.pk, catalogue_number="ab2",
            lot_number="", site_id=self.site.pk)
        self._write("Antibodies_changed.csv", CHANGED, [
            {"ID": "3929", "Column": "Lot", "OldValue": "",
             "NewValue": "1141107-1"},
        ])
        output = self._run(apply=True)
        self.assertEqual(
            Antibody.objects.using(DB).get(access_id=3929).lot_number, "")
        self.assertIn("identity changes", output)

    # ------------------------------------------------------------------
    # Running it twice
    # ------------------------------------------------------------------
    def test_running_it_twice_writes_nothing_the_second_time(self):
        """The delta will be run dry, read, and run again for real, and very
        likely re-run after a correction. `import_access_data` cannot be, which
        is the whole reason this command exists: a second pass that re-`create`s
        its readings doubles the science and no count on any page contradicts it.
        """
        self._write("Antibodies_new.csv", ANTIBODY_COLUMNS, [
            {"ID": "4101", "ProteinsID": "468", "AbNumber": "3085",
             "CompaniesID": "1", "CatNumber": "ab3", "RRID": ""},
        ])
        self._write("CellLines_new.csv", CELL_LINE_COLUMNS, [
            {"ID": "870", "CellLine": "HAP1", "WTorKO": "KO",
             "ProteinsID": "468", "LabLabel": "745", "Species": "Human"},
        ])
        self._write("Wb_new.csv", WB_COLUMNS, [
            {"ID": "2813", "AntibodiesID": "4101", "MembersID": "6",
             "SpecificSignal": "NO", "When": "2026-03-13 00:00:00"},
        ])
        self._run(apply=True)
        counts = (Antibody.objects.using(DB).count(),
                  CellLine.objects.using(DB).count(),
                  CellLineVial.objects.using(DB).count(),
                  ExperimentSession.objects.using(DB).count(),
                  WbResult.objects.using(DB).count())
        self._run(apply=True)
        self.assertEqual(counts, (
            Antibody.objects.using(DB).count(),
            CellLine.objects.using(DB).count(),
            CellLineVial.objects.using(DB).count(),
            ExperimentSession.objects.using(DB).count(),
            WbResult.objects.using(DB).count()))

    def test_a_dry_run_writes_nothing(self):
        """The owner reads the dry run before deciding. If it has already
        written, the decision was never theirs to make.
        """
        self._write("Antibodies_new.csv", ANTIBODY_COLUMNS, [
            {"ID": "4101", "ProteinsID": "468", "AbNumber": "3085",
             "CompaniesID": "1", "CatNumber": "ab4", "RRID": ""},
        ])
        output = self._run()
        self.assertEqual(Antibody.objects.using(DB).count(), 0)
        self.assertIn("DRY RUN", output)

    # ------------------------------------------------------------------
    # A reading belongs to the run it was done in
    # ------------------------------------------------------------------
    def test_readings_from_different_days_are_different_sessions(self):
        """A session is one gene, one day, one person — 392 readings fall into
        43 such runs. Grouping by target alone (what the historical import did,
        having no better handle on rows from 2019) files eight months of
        separate experiments as one and hangs them all on one arbitrary date.
        """
        for i, (access, ab) in enumerate(((4101, "ab5"), (4102, "ab6"))):
            Antibody.objects.using(DB).create(
                access_id=access, ab_number=3085 + i, target_id=self.target.pk,
                company_id=self.company.pk, catalogue_number=ab,
                site_id=self.site.pk)
        self._write("Wb_new.csv", WB_COLUMNS, [
            {"ID": "2813", "AntibodiesID": "4101", "MembersID": "6",
             "When": "2026-03-13 00:00:00"},
            {"ID": "2814", "AntibodiesID": "4102", "MembersID": "6",
             "When": "2026-03-13 00:00:00"},
            {"ID": "2815", "AntibodiesID": "4101", "MembersID": "6",
             "When": "2026-08-06 00:00:00"},
        ])
        self._run(apply=True)
        sessions = ExperimentSession.objects.using(DB).filter(procedure_type="WB")
        self.assertEqual(sessions.count(), 2)
        self.assertEqual(
            sorted(s.date.isoformat() for s in sessions),
            ["2026-03-13", "2026-08-06"])


class CellLineIdentityTests(TestCase):
    """The rename half — where a name is already taken, this is a merge."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.site = Site.objects.using(DB).create(
            name="Montreal", short_code="MTL", is_active=True)

    def _write_changes(self, rows):
        with open(os.path.join(self.dir, "CellLines_changed.csv"), "w",
                  newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CHANGED)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in CHANGED})

    def _run(self, **kwargs):
        out = StringIO()
        call_command("fix_cell_line_identity", csv_dir=self.dir,
                     stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def _line(self, name, access_id, c_number):
        line = CellLine.objects.using(DB).create(
            name=name, genotype="WT", site_id=self.site.pk, access_id=access_id)
        CellLineVial.objects.using(DB).create(
            cell_line_id=line.pk, c_number=c_number, site_id=self.site.pk,
            access_id=access_id)
        return line

    def test_a_free_name_is_normalised(self):
        """The 84 changes are a normalisation to Cellosaurus spelling, not a
        rename — `SKNFI` → `SK-N-FI` — and worth having, since `find_cell_line`
        matches on Cellosaurus first.
        """
        line = self._line("SKNFI", 388, 300)
        self._write_changes([
            {"ID": "388", "Column": "CellLine",
             "OldValue": "SKNFI", "NewValue": "SK-N-FI"}])
        self._run(apply=True)
        line.refresh_from_db(using=DB)
        self.assertEqual(line.name, "SK-N-FI")

    def test_a_taken_name_is_refused_as_a_merge(self):
        """Live cell lines were deduplicated *by name*, so renaming onto a name
        already in use leaves two rows for one line with the freezer split
        between them — and every screen shows both as complete. The real delta
        has exactly one of these: RPEI (C-100) against an existing RPE-1.
        """
        renaming = self._line("RPEI", 157, 100)
        self._line("RPE-1", 690, 690)
        self._write_changes([
            {"ID": "157", "Column": "CellLine",
             "OldValue": "RPEI", "NewValue": "RPE-1"}])
        output = self._run(apply=True)
        renaming.refresh_from_db(using=DB)
        self.assertEqual(renaming.name, "RPEI")
        self.assertIn("merge, not a rename", output)
        # The refusal has to name both rows, or it says only "no" and leaves
        # the reader to find out which line it collided with.
        self.assertIn("C-100", output)
        self.assertIn("C-690", output)

    def test_a_parent_c_number_is_linked_not_just_stored(self):
        """A parent is written as a C-number on 145 of the 169 rows that have
        one, and the number may be a *batch's* rather than the line's own —
        which is why only 2 of 564 rows had the FK linked at all.
        """
        parent = self._line("HAP1", 18, 439)
        child = CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", site_id=self.site.pk, access_id=560,
            parental_line_name="C-48")
        CellLineVial.objects.using(DB).create(
            cell_line_id=child.pk, c_number=560, site_id=self.site.pk,
            access_id=560)
        self._write_changes([
            {"ID": "560", "Column": "ParentalLine",
             "OldValue": "C-48", "NewValue": "C-439"}])
        self._run(apply=True)
        child.refresh_from_db(using=DB)
        self.assertEqual(child.parental_line_name, "C-439")
        self.assertEqual(child.parent_line_id, parent.pk)


class BenchResolutionTests(TestCase):
    """Which bench the delta writes to.

    This began as `short_code="MTL"`, copied from `import_access_data`, and
    refused on live — which files the same bench under another code. The loud
    failure is not the dangerous one: picking the *wrong* site would file
    McGill's antibodies on Leicester's bench, where their A-numbers collide with
    a run that is already there and nothing on any screen says so.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mcgill = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.leicester = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI")
        # Leicester is the bigger bench here, and has nothing from Access.
        for i in range(5):
            Target.objects.using(DB).create(
                protein_name=f"Local protein {i}", gene_name=f"LOC{i}",
                site_id=self.leicester.pk)
        Target.objects.using(DB).create(
            protein_name="Imported protein", gene_name="TRIM2",
            access_id=468, site_id=self.mcgill.pk)
        for name, columns in (("Antibodies_new.csv", ANTIBODY_COLUMNS),
                              ("CellLines_new.csv", CELL_LINE_COLUMNS),
                              ("Wb_new.csv", WB_COLUMNS),
                              ("IF_new.csv", ["ID"]), ("IP_new.csv", ["ID"])):
            with open(os.path.join(self.dir, name), "w", newline="",
                      encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=columns).writeheader()
        for name in ("Antibodies_changed.csv", "CellLines_changed.csv",
                     "Wb_changed.csv", "IF_changed.csv", "Proteins_changed.csv"):
            with open(os.path.join(self.dir, name), "w", newline="",
                      encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=CHANGED).writeheader()
        for name, columns, rows in (
                ("Companies.csv", ["ID", "Company"], []),
                ("Members.csv", ["ID", "Name"], [])):
            with open(os.path.join(self.dir, name), "w", newline="",
                      encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=columns).writeheader()

    def _run(self, **kwargs):
        out = StringIO()
        call_command("import_access_update", csv_dir=self.dir,
                     stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_the_bench_is_the_one_the_access_import_used(self):
        """Not the biggest site, and not a hardcoded short code — the site
        carrying the rows this delta is about to join.
        """
        output = self._run()
        self.assertIn("McGill", output)
        self.assertNotIn("Bench: Leicester", output)

    def test_a_named_site_that_is_not_on_file_names_the_alternatives(self):
        """"There is no site called 'Leicster'" says it is wrong and not what is
        right, which is half a message on a command with one chance to be run.
        """
        with self.assertRaises(CommandError) as caught:
            self._run(site="Leicster")
        self.assertIn("Leicester", str(caught.exception))
        self.assertIn("McGill", str(caught.exception))


class ExperimenterLookupTests(TestCase):
    """Finding the person who ran the experiment.

    On live this dropped **97 of 392 readings** — a quarter of the new science —
    because the lookup asked only for the `access_<name>` login the historical
    import minted. Nine of the lab's twenty-four people have since been given
    real accounts, and those are the ones actually at the bench. A rebuilt copy
    of the Access import cannot show this: every member in it is synthetic.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.company = Company.objects.using(DB).create(name="abcam")
        self.target = Target.objects.using(DB).create(
            protein_name="Kinesin heavy chain", gene_name="KIF5A",
            access_id=8, site_id=self.site.pk)
        self.antibody = Antibody.objects.using(DB).create(
            access_id=4101, ab_number=3085, target_id=self.target.pk,
            company_id=self.company.pk, catalogue_number="ab1",
            site_id=self.site.pk)

        # A real scientist, with her own login — not `access_riham_ayoubi`.
        user = User.objects.using(DB).create(
            username="rayoubi", first_name="Riham", last_name="Ayoubi")
        self.member = Member.objects.using(DB).create(
            user_id=user.pk, site_id=self.site.pk, role="experimenter",
            display_name="Riham Ayoubi")

        for name, columns, rows in (
                ("Companies.csv", ["ID", "Company"], []),
                ("Members.csv", ["ID", "Name"], [{"ID": "1", "Name": "Riham Ayoubi"}])):
            with open(os.path.join(self.dir, name), "w", newline="",
                      encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
        for name, columns in (("Antibodies_new.csv", ANTIBODY_COLUMNS),
                              ("CellLines_new.csv", CELL_LINE_COLUMNS),
                              ("IF_new.csv", ["ID"]), ("IP_new.csv", ["ID"])):
            with open(os.path.join(self.dir, name), "w", newline="",
                      encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=columns).writeheader()
        for name in ("Antibodies_changed.csv", "CellLines_changed.csv",
                     "Wb_changed.csv", "IF_changed.csv", "Proteins_changed.csv"):
            with open(os.path.join(self.dir, name), "w", newline="",
                      encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=CHANGED).writeheader()
        with open(os.path.join(self.dir, "Wb_new.csv"), "w", newline="",
                  encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=WB_COLUMNS)
            writer.writeheader()
            writer.writerow({k: "" for k in WB_COLUMNS} | {
                "ID": "2813", "AntibodiesID": "4101", "MembersID": "1",
                "SpecificSignal": "YES", "When": "2026-06-19 00:00:00"})

    def _run(self, **kwargs):
        out = StringIO()
        call_command("import_access_update", csv_dir=self.dir,
                     stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_a_scientist_with_a_real_login_is_found_by_display_name(self):
        """Not `access_riham_ayoubi` — her own account. The reading must land,
        attributed to her, rather than be skipped for want of a username
        spelling nobody uses.
        """
        output = self._run(apply=True)
        self.assertEqual(WbResult.objects.using(DB).count(), 1)
        session = ExperimentSession.objects.using(DB).get()
        self.assertEqual(session.experimenter_id, self.member.pk)
        self.assertNotIn("readings skipped", output)

    def test_a_retired_duplicate_does_not_keep_the_name_ambiguous(self):
        """`merge_members` deactivates the duplicate rather than deleting it,
        because sessions point at these rows. Counting a retired row as a rival
        left Carl Laflamme ambiguous *after* his two rows had been merged, and
        went on refusing the very reading the merge was run to rescue.
        """
        stale = User.objects.using(DB).create(username="access_riham_ayoubi")
        Member.objects.using(DB).create(
            user_id=stale.pk, site_id=self.site.pk, role="experimenter",
            display_name="Riham Ayoubi", is_active=False)
        self._run(apply=True)
        self.assertEqual(WbResult.objects.using(DB).count(), 1)
        self.assertEqual(
            ExperimentSession.objects.using(DB).get().experimenter_id,
            self.member.pk)

    def test_two_people_of_one_name_is_a_question_not_a_coin_toss(self):
        """Attributing a day's work to the wrong scientist is not something
        anybody can spot by looking at it afterwards.
        """
        other = User.objects.using(DB).create(username="rayoubi2")
        Member.objects.using(DB).create(
            user_id=other.pk, site_id=self.site.pk, role="experimenter",
            display_name="Riham Ayoubi")
        output = self._run(apply=True)
        self.assertEqual(WbResult.objects.using(DB).count(), 0)
        self.assertIn("more than one person", output)
