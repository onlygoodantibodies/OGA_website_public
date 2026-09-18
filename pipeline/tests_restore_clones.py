"""What `restore_cell_line_clones` must get right, and what it must refuse.

Built on the real HCT116 ACSL5 case rather than an invented one: seven clones
(2.3, 2.5, 2.6, 2.8, 4.2, 4.5, 4.11) that `restructure_cell_lines` merged into
one row labelled clone 2.3, with C-472…C-478 left underneath it as freeze-down
batches. Clone 2.8 is the one the export marks `Confirmed`, which is the case
worth having a fixture for: the verdict must land on the clone that earned it
and nowhere else.

Only the silent failures are pinned. A command that refuses out loud, or writes
a row somebody can see, will say so the first time it is run — what cannot be
seen is a clone restored under the wrong number, a verdict copied onto a
sibling, or a second run quietly duplicating the lot.
"""
import csv
import datetime
import os
import tempfile
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from pipeline.models import (Antibody, CellLine, CellLineVial,
                             ExperimentSession, Member, Site, Target, WbResult)
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import identity

DB = "pipeline_db"

_CELL_LINE_COLUMNS = ["ID", "CellLine", "WTorKO", "ProteinsID", "LabLabel",
                      "Origin", "OriginComments", "CatNumber", "Lot",
                      "ParentalLine", "Species", "Clone", "GrowthProperties",
                      "Medium", "KOconfirmed", "KOInfo", "Thawed", "ReceivedDate"]

# access id, C-number, clone, verdict — the live group, in its own order.
_CLONES = [
    (487, 472, "2.3", ""),
    (488, 473, "2.5", ""),
    (489, 474, "2.6", ""),
    (490, 475, "2.8", "Confirmed"),
    (491, 476, "4.2", ""),
    (492, 477, "4.5", ""),
    (493, 478, "4.11", ""),
]


def _write_csvs(directory, *, wb_rows=()):
    with open(os.path.join(directory, "Proteins.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ID", "Gene"])
        w.writeheader()
        w.writerow({"ID": "427", "Gene": "ACSL5"})
    with open(os.path.join(directory, "CellLines.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_CELL_LINE_COLUMNS)
        w.writeheader()
        for access_id, c_number, clone, verdict in _CLONES:
            w.writerow({"ID": access_id, "CellLine": "HCT116", "WTorKO": "KO",
                        "ProteinsID": "427", "LabLabel": c_number,
                        "Clone": clone, "KOInfo": verdict, "Medium": "McCoy's",
                        "Thawed": "1", "ReceivedDate": "06/01/23 00:00:00"})
    if wb_rows:
        with open(os.path.join(directory, "Wb.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "ID", "lane1CellLineID", "lane2CellLineID",
                "lane3CellLineID", "lane4CellLineID"])
            w.writeheader()
            for row in wb_rows:
                w.writerow(row)
    return os.path.join(directory, "CellLines.csv")


class RestoreClonesTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.target = Target.objects.using(DB).create(gene_name="ACSL5")
        self.parent = CellLine.objects.using(DB).create(
            name="HCT116", genotype="WT", site=self.site)
        # The merged row, exactly as live holds it: one line, clone 2.3, seven
        # vials underneath carrying the numbers that are on the tubes.
        self.survivor = CellLine.objects.using(DB).create(
            name="HCT116", genotype="KO", target=self.target, site=self.site,
            parent_line=self.parent, clone="2.3", c_number=472, access_id=487,
            # Live's own state for this row: the export says `Thawed=1` for all
            # seven and carries no `received` column at all. The fixture has to
            # mirror that, or the sibling-agreement test below is asking the
            # restored rows to match a survivor that is itself the odd one out.
            thawed=True, received=False)
        for access_id, c_number, _clone, _verdict in _CLONES:
            CellLineVial.objects.using(DB).create(
                cell_line=self.survivor, c_number=c_number, site=self.site,
                access_id=access_id)

    def _member(self):
        """A Member needs its login row in the *other* database — auth spans two,
        and a pipeline user is matched by username (see services/members.py)."""
        for alias in ("academy_db", DB):
            user = User(username="sara")
            user.set_password("pw")
            user.save(using=alias)
        return Member.objects.using(DB).create(
            user_id=User.objects.using(DB).get(username="sara").pk,
            site=self.site, role="admin", is_active=True)

    def _run(self, directory, **kwargs):
        out = StringIO()
        call_command("restore_cell_line_clones", csv=_write_csvs(directory, **kwargs),
                     stdout=out, **{"apply": True})
        return out.getvalue()

    def test_each_clone_comes_back_with_its_own_number(self):
        """The tube says C-475 and the record has to agree.

        The silent failure this guards is a clone restored under a *fresh*
        C-number: `lab_numbers` issues one on `pre_save` whenever the column is
        blank, so a restored row that forgot to carry its own would be filed
        under the next number free at that bench — correct-looking, and pointing
        at a box that holds something else.
        """
        with tempfile.TemporaryDirectory() as directory:
            self._run(directory)
        lines = {cl.clone: cl for cl in CellLine.objects.using(DB)
                 .filter(genotype="KO", target=self.target)}
        self.assertEqual(sorted(lines), sorted(c[2] for c in _CLONES))
        for _access_id, c_number, clone, _verdict in _CLONES:
            self.assertEqual(lines[clone].c_number, c_number, clone)
            vial = CellLineVial.objects.using(DB).get(c_number=c_number)
            self.assertEqual(vial.cell_line_id, lines[clone].pk, clone)

    def test_the_confirmation_lands_only_on_the_clone_that_earned_it(self):
        """Clone 2.8 is the one the export marks Confirmed — and only 2.8.

        A verdict copied across the siblings is the exact failure the merge
        already committed in reverse, and nothing on any screen would contradict
        it: six knockouts drawn as confirmed on the strength of a seventh's blot.
        """
        with tempfile.TemporaryDirectory() as directory:
            self._run(directory)
        confirmed = list(CellLine.objects.using(DB)
                         .filter(genotype="KO", target=self.target, ko_validated=True))
        self.assertEqual([cl.clone for cl in confirmed], ["2.8"])

    def test_a_restored_clone_does_not_contradict_its_own_siblings(self):
        """Seven tubes from one freeze-down run must not disagree in one column.

        The first live run put six restored ACSL5 clones on screen reading
        *received / not thawed* beside their own survivor reading *not received /
        thawed*, because this asserted `received=True` instead of reading the
        export and the row it was split from. Nothing is wrong with the science
        and everything is wrong with the screen: a column where siblings
        disagree is the shape of every two-readers-one-fact defect here.

        `Thawed` is a real column and is read. `received` has none — the export
        carries only `ReceivedDate` — so it follows the survivor. And
        `ReceivedDate` itself is *not* read: `06/01/23` is two different days
        and the cell does not say which.
        """
        with tempfile.TemporaryDirectory() as directory:
            self._run(directory)
        rows = list(CellLine.objects.using(DB)
                    .filter(genotype="KO", target=self.target))
        self.assertEqual(len(rows), 7)
        self.assertEqual({cl.received for cl in rows}, {self.survivor.received})
        self.assertEqual({cl.thawed for cl in rows}, {True})
        # An ambiguous date is left alone rather than guessed at.
        self.assertEqual({cl.received_date for cl in rows}, {None})

    def test_running_it_twice_writes_nothing_the_second_time(self):
        """Idempotent on `access_id`, or a re-run doubles the freezer."""
        with tempfile.TemporaryDirectory() as directory:
            self._run(directory)
            before = CellLine.objects.using(DB).count()
            second = self._run(directory)
        self.assertEqual(CellLine.objects.using(DB).count(), before)
        self.assertIn("clones restored   0", second)

    def test_a_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_csvs(directory)
            out = StringIO()
            call_command("restore_cell_line_clones", csv=path, stdout=out)
        self.assertEqual(
            CellLine.objects.using(DB).filter(genotype="KO").count(), 1)
        self.assertIn("DRY RUN", out.getvalue())

    def test_a_session_moves_to_the_clone_its_own_readings_name(self):
        """The blot named lane 2 as Access cell line 490, so the session is 2.8's.

        And the reverse half in the same test: a session whose readings name two
        clones is left where it is. There is no honest single answer for it — the
        historical import grouped sessions by (procedure, target), not by day —
        and choosing one would be the merge's own mistake made again, quietly.
        """
        member = self._member()
        settled = ExperimentSession.objects.using(DB).create(
            target=self.target, site=self.site, experimenter=member,
            procedure_type="WB", cell_line_ko=self.survivor,
            cell_line_wt=self.parent, date=datetime.date(2026, 1, 5))
        straddling = ExperimentSession.objects.using(DB).create(
            target=self.target, site=self.site, experimenter=member,
            procedure_type="WB", cell_line_ko=self.survivor,
            cell_line_wt=self.parent, date=datetime.date(2026, 1, 6))
        antibody = Antibody.objects.using(DB).create(
            catalogue_number="AB-1", target=self.target, site=self.site)
        WbResult.objects.using(DB).create(session=settled, antibody=antibody,
                                          access_id=9001)
        WbResult.objects.using(DB).create(session=straddling, antibody=antibody,
                                          access_id=9002)
        WbResult.objects.using(DB).create(session=straddling, antibody=antibody,
                                          access_id=9003)
        wb_rows = [
            {"ID": 9001, "lane1CellLineID": "", "lane2CellLineID": 490,
             "lane3CellLineID": "", "lane4CellLineID": ""},
            {"ID": 9002, "lane1CellLineID": "", "lane2CellLineID": 490,
             "lane3CellLineID": "", "lane4CellLineID": ""},
            {"ID": 9003, "lane1CellLineID": "", "lane2CellLineID": 491,
             "lane3CellLineID": "", "lane4CellLineID": ""},
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = self._run(directory, wb_rows=wb_rows)

        clone_28 = CellLine.objects.using(DB).get(clone="2.8", genotype="KO")
        settled.refresh_from_db()
        straddling.refresh_from_db()
        self.assertEqual(settled.cell_line_ko_id, clone_28.pk)
        self.assertEqual(straddling.cell_line_ko_id, self.survivor.pk)
        self.assertIn(f"session #{straddling.pk}", report)

    def test_a_dry_run_reaches_the_same_verdict_as_the_write(self):
        """The preview must not report a clean move where the write refuses one.

        Found on the live dry run of ACSL5 (4 Sep 2026), which printed
        `session #102 → cell line None` and counted two clean repoints. Nothing
        is saved in a dry run, so every restored clone had `pk = None`, and a
        session whose readings named *two* of them collapsed to the single value
        `{None}` — read as agreement. The write, where the rows do have keys,
        would have found the ambiguity and left the session alone.

        Under-reporting ambiguity is the direction that matters: it is a preview
        promising a move the write will not make, on the one field that says
        which knockout an experiment was run against.
        """
        member = self._member()
        session = ExperimentSession.objects.using(DB).create(
            target=self.target, site=self.site, experimenter=member,
            procedure_type="WB", cell_line_ko=self.survivor,
            cell_line_wt=self.parent, date=datetime.date(2026, 1, 7))
        antibody = Antibody.objects.using(DB).create(
            catalogue_number="AB-2", target=self.target, site=self.site)
        for access_id in (9101, 9102):
            WbResult.objects.using(DB).create(session=session, antibody=antibody,
                                              access_id=access_id)
        # Two readings, two different clones — 2.8 and 4.2.
        wb_rows = [
            {"ID": 9101, "lane1CellLineID": "", "lane2CellLineID": 490,
             "lane3CellLineID": "", "lane4CellLineID": ""},
            {"ID": 9102, "lane1CellLineID": "", "lane2CellLineID": 491,
             "lane3CellLineID": "", "lane4CellLineID": ""},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = _write_csvs(directory, wb_rows=wb_rows)
            dry = StringIO()
            call_command("restore_cell_line_clones", csv=path, stdout=dry)
            wet = StringIO()
            call_command("restore_cell_line_clones", csv=path, stdout=wet,
                         **{"apply": True})

        for report in (dry.getvalue(), wet.getvalue()):
            self.assertIn("sessions repointed   0", report)
            self.assertIn("sessions left     1", report)
            self.assertIn(f"session #{session.pk}", report)
            # The clones are named, and no primary key stands in for one — in a
            # dry run there is not one to print.
            self.assertIn("2.8", report)
            self.assertNotIn("cell line None", report)
        session.refresh_from_db()
        self.assertEqual(session.cell_line_ko_id, self.survivor.pk)

    def test_a_clone_frozen_twice_stays_one_line_with_two_batches(self):
        """HeLa NR4A2 is five Access rows and three clones, not five clones.

        D15 sits at C-605 and C-636, E6 at C-606 and C-637, M20 at C-638. **A
        C-number is a freeze-down batch of a clone**, so the repeats are batches.
        Giving each its own row would put two lines under one clone with its
        vials and its readings split between them and both drawn as complete —
        the shape `identity.py::_clone_taken` refuses on the dialog, and the
        shape this whole command exists to undo. Recreating it here would be the
        Access merge's mistake made backwards.
        """
        target = Target.objects.using(DB).create(gene_name="NR4A2")
        survivor = CellLine.objects.using(DB).create(
            name="HeLa", genotype="KO", target=target, site=self.site,
            clone="D15", c_number=605, access_id=700, thawed=True)
        batches = [(700, 605, "D15"), (701, 606, "E6"), (702, 636, "D15"),
                   (703, 637, "E6"), (704, 638, "M20")]
        for access_id, c_number, _clone in batches:
            CellLineVial.objects.using(DB).create(
                cell_line=survivor, c_number=c_number, site=self.site,
                access_id=access_id)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "CellLines.csv")
            with open(os.path.join(directory, "Proteins.csv"), "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["ID", "Gene"])
                w.writeheader()
                w.writerow({"ID": "500", "Gene": "NR4A2"})
            with open(path, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=_CELL_LINE_COLUMNS)
                w.writeheader()
                for access_id, c_number, clone in batches:
                    w.writerow({"ID": access_id, "CellLine": "HeLa",
                                "WTorKO": "KO", "ProteinsID": "500",
                                "LabLabel": c_number, "Clone": clone,
                                "Thawed": "1"})
            out = StringIO()
            call_command("restore_cell_line_clones", csv=path, stdout=out,
                         **{"apply": True})

        rows = {cl.clone: cl for cl in CellLine.objects.using(DB)
                .filter(genotype="KO", target=target)}
        self.assertEqual(sorted(rows), ["D15", "E6", "M20"])
        self.assertEqual(
            {clone: CellLineVial.objects.using(DB)
                    .filter(cell_line=line).count()
             for clone, line in rows.items()},
            {"D15": 2, "E6": 2, "M20": 1})
        # Each vial is on the row for the clone that was frozen, not wherever it
        # started — C-636 is D15's second batch and must be on D15.
        for access_id, c_number, clone in batches:
            vial = CellLineVial.objects.using(DB).get(c_number=c_number)
            self.assertEqual(vial.cell_line_id, rows[clone].pk,
                             f"C-{c_number} should be on clone {clone}")
        self.assertIn("clones restored   2", out.getvalue())

    def test_a_group_recording_one_clone_is_left_alone(self):
        """Repeated freeze-downs of one line are what the vial table already says.

        Splitting those would invent six cell lines out of one, which is the
        opposite defect and just as invisible.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "CellLines.csv")
            with open(path, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=_CELL_LINE_COLUMNS)
                w.writeheader()
                for access_id, c_number, _clone, _verdict in _CLONES:
                    w.writerow({"ID": access_id, "CellLine": "HCT116",
                                "WTorKO": "KO", "ProteinsID": "427",
                                "LabLabel": c_number, "Clone": "2.3"})
            out = StringIO()
            call_command("restore_cell_line_clones", csv=path, stdout=out,
                         **{"apply": True})
        self.assertEqual(
            CellLine.objects.using(DB).filter(genotype="KO").count(), 1)
        self.assertIn("No split knockout groups", out.getvalue())


class ClonesAreEditableOnTheRowsThatHaveThemTests(TestCase):
    """The dialog is the one surface that changes a clone, so it has to open.

    385 of the 399 knockouts on file have no `parent_line` — the lab records a
    parental as a C-number, which this app could not read until
    `cell_lines.by_c_number` existed. The dialog refused any save on a KO whose
    parent box was blank, so putting the clone *in* that dialog made a
    pre-existing gap block the very edit the dialog was extended for.
    """
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.target = Target.objects.using(DB).create(gene_name="ARF1")
        self.parent = CellLine.objects.using(DB).create(
            name="HeLa", genotype="WT", site=self.site)

    def _line(self, **kwargs):
        return CellLine.objects.using(DB).create(
            name="HeLa", genotype="KO", target=self.target, site=self.site,
            **kwargs)

    def _data(self, line, **over):
        data = {"name": line.name, "gene": "ARF1", "genotype": "KO",
                "clone": line.clone, "parent": "", "company": ""}
        data.update(over)
        return data

    def test_a_clone_can_be_set_on_a_knockout_that_never_had_a_parent(self):
        """HeLa ARF1's survivor, which is the case this was found on."""
        line = self._line(clone="", c_number=644)
        identity.change_cell_line_identity(line, self._data(line, clone="0"))
        line.refresh_from_db()
        self.assertEqual(line.clone, "0")
        self.assertIsNone(line.parent_line_id)

    def test_a_blank_parent_never_clears_one_that_is_recorded(self):
        """The half the guard is actually for: a KO stripped of its parental
        stops meaning anything, and the dialog pre-fills the box — so a blank
        there is a deletion, not an absence."""
        line = self._line(clone="1", parent_line=self.parent)
        with self.assertRaises(identity.Refused) as caught:
            identity.change_cell_line_identity(line, self._data(line, clone="2"))
        self.assertIn("parental line", str(caught.exception))
        line.refresh_from_db()
        self.assertEqual(line.parent_line_id, self.parent.pk)
        self.assertEqual(line.clone, "1", "the refused save wrote nothing")

    def test_two_rows_of_one_knockout_cannot_be_given_the_same_clone(self):
        """Which is the whole reason a clone is identity rather than a cell."""
        self._line(clone="1")
        second = self._line(clone="3")
        with self.assertRaises(identity.Refused) as caught:
            identity.change_cell_line_identity(second, self._data(second, clone="1"))
        self.assertIn("clone 1", str(caught.exception))


class RestoredClonesAreTellableApartTests(TestCase):
    """The recovery is only worth anything if the rows can then be told apart.

    Restoring seven rows that all render `HCT116 ACSL5 KO — McGill` would move
    the problem rather than fix it: the resolver would refuse every paste naming
    one, and the session picker would offer seven identical options. So the
    label carries the clone, and it round-trips back to the row it names.
    """
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.target = Target.objects.using(DB).create(gene_name="ACSL5")
        self.lines = {
            clone: CellLine.objects.using(DB).create(
                name="HCT116", genotype="KO", target=self.target,
                site=self.site, clone=clone)
            for clone in ("2.3", "2.8", "4.11")
        }

    def test_the_label_names_the_clone_and_reads_back(self):
        for clone, line in self.lines.items():
            text = cell_line_svc.label(line)
            self.assertIn(clone, text)
            self.assertEqual(cell_line_svc.candidates(text), [line])

    def test_the_bare_knockout_name_is_a_question_not_a_coin_toss(self):
        """`HCT116 ACSL5 KO` now means three rows, and must say so.

        Answering with one of them is how a session ends up controlled against a
        clone nobody chose — the same rule this module holds for two sites'
        HAP1, arriving one level down.
        """
        line, err = cell_line_svc.resolve("HCT116 ACSL5 KO — McGill",
                                          genotype="KO", site_id=self.site.pk)
        self.assertIsNone(line)
        self.assertIn("3 cell lines", err)
        for clone in self.lines:
            self.assertIn(clone, err)

    def test_a_stored_name_containing_the_word_clone_is_not_taken_apart(self):
        """`Jurkat, clone E6-1` is a real parental — ATCC's own designation.

        The most literal reading wins, so the name matches its own row before
        anything tries to read `clone E6-1` as a clone suffix and go looking for
        a line called `Jurkat,`.
        """
        jurkat = CellLine.objects.using(DB).create(
            name="Jurkat, clone E6-1", genotype="WT", site=self.site)
        self.assertEqual(
            cell_line_svc.candidates("Jurkat, clone E6-1 — McGill"), [jurkat])

    def test_a_name_containing_clone_round_trips_when_the_row_has_one_too(self):
        """`Jurkat, clone E6-1` is a real parental — and McGill's SLIT1 knockout
        of it is clone D16.

        So `label()` renders `Jurkat, clone E6-1 SLIT1 KO clone D16`: two
        `clone`s in one string. Splitting on the first asks for a line called
        `Jurkat,` and finds nothing, which is a label the app composed itself and
        then could not read back — the exact failure `split_label` already
        records for the site half. Every separator is tried, rightmost first,
        because the rightmost is the one `label()` appended.
        """
        slit1 = Target.objects.using(DB).create(gene_name="SLIT1")
        jurkat_ko = CellLine.objects.using(DB).create(
            name="Jurkat, clone E6-1", genotype="KO", target=slit1,
            site=self.site, clone="D16")
        text = cell_line_svc.label(jurkat_ko)
        self.assertEqual(text, "Jurkat, clone E6-1 SLIT1 KO clone D16 — McGill")
        self.assertEqual(cell_line_svc.candidates(text), [jurkat_ko])

        # And its sibling, so the two are told apart rather than both answering.
        k2 = CellLine.objects.using(DB).create(
            name="Jurkat, clone E6-1", genotype="KO", target=slit1,
            site=self.site, clone="K2")
        self.assertEqual(cell_line_svc.candidates(cell_line_svc.label(k2)), [k2])
        self.assertEqual(cell_line_svc.candidates(text), [jurkat_ko])

    def test_a_clone_value_that_itself_contains_the_word_still_resolves(self):
        """Live records one clone as `Guide B9, 1X, Clone D` — guide and clone in
        one cell. The rightmost separator is inside the *value* there, so the
        reading that works is one further left, and both stay in the list."""
        gcg = Target.objects.using(DB).create(gene_name="GCG")
        line = CellLine.objects.using(DB).create(
            name="SK-N-AS", genotype="KO", target=gcg, site=self.site,
            clone="Guide B9, 1X, Clone D")
        text = cell_line_svc.label(line)
        self.assertEqual(cell_line_svc.candidates(text), [line])

    def test_a_knockout_with_no_clone_renders_exactly_as_it_did(self):
        """338 of the 399 knockouts on file record no clone, and every stored
        label, bookmark and sheet on somebody's disk has to go on working."""
        plain = CellLine.objects.using(DB).create(
            name="U2OS", genotype="KO", target=self.target, site=self.site)
        self.assertEqual(cell_line_svc.label(plain), "U2OS ACSL5 KO — McGill")
