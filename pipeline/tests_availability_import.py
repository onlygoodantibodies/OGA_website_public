"""``import_antibody_availability`` writes to the row it was told to, or not at all.

The command keys on ``oga_id``, which is a primary key — meaningful only against
the database the check was run on. Regenerate the file against a restored dump
and the same integers name different reagents, so a pk-only write would silently
mark the wrong products discontinued across the public site with nothing on any
screen to catch it by. The catalogue number is what confirms the row, and these
pin that it is actually consulted.

The other silent shape is ``unclear``. 26 rows are genuinely indeterminate —
registry records that were never catalogue products, and two suppliers' own
"temporarily unavailable" hedges — and writing either value there would be
asserting something nobody established. Folding them into "unchanged" would hide
that from whoever reads the run.
"""
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pipeline.models import Antibody, Company, Target

DB = "pipeline_db"

HEADER = "oga_id,catalogue_number,availability_checked,evidence_quote\n"


def _csv(tmp_path, rows):
    path = tmp_path / "availability.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return str(path)


class AvailabilityImportTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        import tempfile
        import pathlib
        self.tmp = pathlib.Path(tempfile.mkdtemp())

        company = Company.objects.create(name="Proteintech")
        target = Target.objects.create(protein_name="Synuclein", gene_name="SNCA")
        self.live = Antibody.objects.create(
            target=target, company=company,
            catalogue_number="10842-1-AP", out_of_market=False)
        self.flagged = Antibody.objects.create(
            target=target, company=company,
            catalogue_number="60009-1-Ig", out_of_market=True)

    def _run(self, rows, *, apply=False):
        out = StringIO()
        args = [_csv(self.tmp, rows)]
        if apply:
            args.append("--apply")
        call_command("import_antibody_availability", *args, stdout=out)
        self.live.refresh_from_db()
        self.flagged.refresh_from_db()
        return out.getvalue()

    # ── the write itself ────────────────────────────────────────────────

    def test_a_dry_run_writes_nothing(self):
        report = self._run([f"{self.live.pk},10842-1-AP,discontinued,gone\n"])
        self.assertFalse(self.live.out_of_market)
        self.assertIn("DRY RUN", report)

    def test_discontinued_sets_the_flag(self):
        self._run([f"{self.live.pk},10842-1-AP,discontinued,\"Product Discontinued\"\n"],
                  apply=True)
        self.assertTrue(self.live.out_of_market)

    def test_available_clears_a_stale_flag(self):
        # The check found four of these: flagged years ago, on sale today.
        self._run([f"{self.flagged.pk},60009-1-Ig,available,\"Add to Basket\"\n"],
                  apply=True)
        self.assertFalse(self.flagged.out_of_market)

    # ── what it refuses ─────────────────────────────────────────────────

    def test_a_catalogue_number_that_disagrees_is_refused_by_name(self):
        report = self._run(
            [f"{self.live.pk},SOMETHING-ELSE,discontinued,gone\n"], apply=True)
        self.assertFalse(self.live.out_of_market)
        self.assertIn("Refused", report)
        # Both spellings, or the reader cannot tell which end is wrong.
        self.assertIn("SOMETHING-ELSE", report)
        self.assertIn("10842-1-AP", report)

    def test_an_unknown_record_number_is_refused_not_created(self):
        before = Antibody.objects.count()
        report = self._run(["99999,10842-1-AP,discontinued,gone\n"], apply=True)
        self.assertEqual(Antibody.objects.count(), before)
        self.assertIn("no antibody with that record number", report)

    def test_a_row_that_is_not_a_record_number_is_refused(self):
        report = self._run(["not-a-number,10842-1-AP,discontinued,gone\n"], apply=True)
        self.assertIn("Refused", report)

    def test_a_file_missing_the_verdict_column_is_refused_whole(self):
        path = self.tmp / "bad.csv"
        path.write_text("oga_id,catalogue_number\n1,X\n", encoding="utf-8")
        with self.assertRaises(CommandError) as caught:
            call_command("import_antibody_availability", str(path), stdout=StringIO())
        self.assertIn("availability_checked", str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("import_antibody_availability",
                         str(self.tmp / "nope.csv"), stdout=StringIO())

    # ── unclear ─────────────────────────────────────────────────────────

    def test_unclear_writes_nothing_in_either_direction(self):
        self._run([
            f"{self.live.pk},10842-1-AP,unclear,\"contact the facility\"\n",
            f"{self.flagged.pk},60009-1-Ig,unclear,\"temporarily unavailable\"\n",
        ], apply=True)
        self.assertFalse(self.live.out_of_market)
        self.assertTrue(self.flagged.out_of_market)

    def test_unclear_is_counted_apart_from_unchanged(self):
        # "already correct" and "nobody could tell" are different facts about
        # the run, and only one of them is worth somebody's time.
        report = self._run([f"{self.live.pk},10842-1-AP,unclear,\n"])
        self.assertIn("could not tell", report)
        self.assertIn("10842-1-AP", report)

    # ── the committed file ──────────────────────────────────────────────

    def test_the_committed_file_parses_and_names_only_known_verdicts(self):
        import csv
        from pipeline.management.commands.import_antibody_availability import (
            DEFAULT_CSV, REQUIRED_COLUMNS, WRITES,
        )
        with open(DEFAULT_CSV, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1645)
        self.assertFalse(REQUIRED_COLUMNS - set(rows[0]))
        self.assertEqual(
            {r["availability_checked"] for r in rows},
            set(WRITES) | {"unclear"})
        # A pk twice would make the run order decide the answer.
        ids = [r["oga_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))
        # Every discontinued verdict carries the quote it rests on — the file is
        # the only record of why a product was marked gone.
        self.assertTrue(all(
            r["evidence_quote"].strip()
            for r in rows if r["availability_checked"] == "discontinued"))
