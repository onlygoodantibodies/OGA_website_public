"""``import_antibody_availability`` writes to the row it was told to, or not at all.

The command keys on ``oga_id``, which is a primary key — meaningful only against
the database the check was run on. Regenerate the file against a restored dump
and the same integers name different reagents, so a pk-only write would silently
mark the wrong products discontinued across the public site with nothing on any
screen to catch it by. The catalogue number is what confirms the row, and these
pin that it is actually consulted.

The other silent shape is a verdict that writes nothing. Three do — ``unclear``
(the answer could not be determined), ``not_rechecked`` (the question was not
asked in this pass) and ``out_of_stock`` (a shelf, not a catalogue) — and they
are three different facts about the run, so they are counted and listed apart
from each other and from "unchanged". The one that would do real harm folded in
is ``not_rechecked``: two rows are ``discontinued`` on the record and were not
re-looked-at on 30 Aug, and treating "nobody looked" as "on sale" would put a
withdrawn product back on a public gene page.
"""
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pipeline.models import Antibody, Company, Target

DB = "pipeline_db"

HEADER = "oga_id,catalogue_number,availability_checked,evidence_quote\n"
GRADED = HEADER.rstrip("\n") + ",evidence_type\n"


def _csv(tmp_path, rows, header=HEADER):
    path = tmp_path / "availability.csv"
    path.write_text(header + "".join(rows), encoding="utf-8")
    return str(path)


class AvailabilityImportTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

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

    def _run(self, rows, *, apply=False, header=HEADER):
        out = StringIO()
        args = [_csv(self.tmp, rows, header)]
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

    # ── the verdicts that write nothing ─────────────────────────────────

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

    def test_not_rechecked_leaves_a_discontinued_row_flagged(self):
        # The harmful direction: "nobody looked" read as "on sale" would put a
        # withdrawn product back on its gene page.
        self._run([f"{self.flagged.pk},60009-1-Ig,not_rechecked,\n"], apply=True)
        self.assertTrue(self.flagged.out_of_market)

    def test_out_of_stock_is_not_a_withdrawal(self):
        self._run([f"{self.live.pk},10842-1-AP,out_of_stock,\n"], apply=True)
        self.assertFalse(self.live.out_of_market)

    def test_the_three_abstentions_are_named_apart_from_each_other(self):
        report = self._run([
            f"{self.live.pk},10842-1-AP,not_rechecked,\n",
            f"{self.flagged.pk},60009-1-Ig,out_of_stock,\n",
        ])
        self.assertIn("not looked at in this pass", report)
        self.assertIn("temporarily out of stock", report)
        self.assertNotIn("could not tell", report)

    def test_an_unrecognised_verdict_is_not_folded_in_with_the_deliberate_ones(self):
        report = self._run([f"{self.live.pk},10842-1-AP,disconinued,\n"], apply=True)
        self.assertFalse(self.live.out_of_market)
        self.assertIn("verdict not recognised", report)
        self.assertIn("disconinued", report)

    def test_a_withdrawal_with_no_quote_says_so(self):
        # A blank evidence cell printed as a blank line reads as a verdict that
        # was checked. 38 of the 30 Aug withdrawals have no quote behind them.
        report = self._run([f"{self.live.pk},10842-1-AP,discontinued,\n"])
        self.assertIn("no on-page quote recorded", report)

    # ── the evidence a withdrawal rests on ──────────────────────────────

    def test_a_withdrawal_needs_evidence_once_the_file_grades_it(self):
        # Abcam's third state: a full datasheet, no buy panel, and no notice
        # saying the product is gone. Hiding it would be a claim nobody made.
        report = self._run(
            [f"{self.live.pk},10842-1-AP,discontinued,\"no buy panel\","
             f"no_purchase_option_no_statement\n"],
            apply=True, header=GRADED)
        self.assertFalse(self.live.out_of_market)
        self.assertIn("no discontinuation notice", report)
        self.assertIn("10842-1-AP", report)

    def test_an_on_page_statement_still_writes(self):
        self._run(
            [f"{self.live.pk},10842-1-AP,discontinued,\"Product Discontinued\","
             f"on_page_statement\n"],
            apply=True, header=GRADED)
        self.assertTrue(self.live.out_of_market)

    def test_a_404_is_not_a_withdrawal(self):
        # 7 of 9 GeneTex URLs that 404'd were live under a changed slug.
        self._run([f"{self.live.pk},10842-1-AP,discontinued,\"404\","
                   f"unverified_404\n"], apply=True, header=GRADED)
        self.assertFalse(self.live.out_of_market)

    def test_not_orderable_is_a_supply_state_not_a_withdrawal(self):
        self._run([f"{self.live.pk},10842-1-AP,discontinued,\"not orderable\","
                   f"on_page_statement_soft\n"], apply=True, header=GRADED)
        self.assertFalse(self.live.out_of_market)

    def test_an_unfamiliar_evidence_type_is_held_back_not_written(self):
        # A type nobody recognises is not a standard anybody checked against,
        # and the harmful direction is hiding a product still on sale.
        report = self._run([f"{self.live.pk},10842-1-AP,discontinued,\"x\","
                            f"vibes\n"], apply=True, header=GRADED)
        self.assertFalse(self.live.out_of_market)
        self.assertIn("vibes", report)

    def test_a_blank_evidence_type_is_held_back(self):
        self._run([f"{self.live.pk},10842-1-AP,discontinued,\"x\",\n"],
                  apply=True, header=GRADED)
        self.assertFalse(self.live.out_of_market)

    def test_a_file_with_no_evidence_type_column_is_not_held_to_one(self):
        # The 11-12 Aug file predates the column; reading it must not turn
        # every one of its 180 withdrawals into a refusal.
        self._run([f"{self.live.pk},10842-1-AP,discontinued,\"gone\"\n"],
                  apply=True)
        self.assertTrue(self.live.out_of_market)

    def test_the_gate_never_blocks_clearing_a_flag(self):
        # Evidence is what a *withdrawal* is held to. Putting a product back on
        # sale is the safe direction and is not gated.
        self._run([f"{self.flagged.pk},60009-1-Ig,available,\"\",\n"],
                  apply=True, header=GRADED)
        self.assertFalse(self.flagged.out_of_market)

    # ── the committed file ──────────────────────────────────────────────

    def _committed(self, path):
        import csv
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_the_committed_file_parses_and_names_only_known_verdicts(self):
        from pipeline.management.commands.import_antibody_availability import (
            DEFAULT_CSV, LEAVES_ALONE, REQUIRED_COLUMNS, WRITES,
        )
        rows = self._committed(DEFAULT_CSV)
        self.assertEqual(len(rows), 1645)
        self.assertFalse(REQUIRED_COLUMNS - set(rows[0]))
        self.assertFalse(
            {r["availability_checked"] for r in rows}
            - (set(WRITES) | set(LEAVES_ALONE)))
        # A pk twice would make the run order decide the answer.
        ids = [r["oga_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_recheck_carries_the_august_12_verdict_forward(self):
        # The 30 Aug file replaces the 11-12 Aug one as the default, so it has
        # to hold what that file knew — two rows are discontinued on the record
        # and were not re-looked-at, and nothing else records that.
        import os
        from pipeline.management.commands.import_antibody_availability import (
            DEFAULT_CSV,
        )
        august = self._committed(os.path.join(
            os.path.dirname(DEFAULT_CSV), "antibody_availability_2026_08.csv"))
        recheck = {r["oga_id"]: r for r in self._committed(DEFAULT_CSV)}
        self.assertEqual(len(august), len(recheck))
        for row in august:
            later = recheck[row["oga_id"]]
            self.assertEqual(later["previous_verdict"],
                             row["availability_checked"])
            self.assertEqual(later["catalogue_number"], row["catalogue_number"])

    def test_a_quote_carried_forward_is_dated_so_it_reads_as_the_earlier_pass(self):
        # A row re-visited on 30 Aug carries what that visit saw; every other
        # quote is the earlier pass's and says so, or the file would date
        # August-12 evidence to the 30th.
        from pipeline.management.commands.import_antibody_availability import (
            DEFAULT_CSV,
        )
        for row in self._committed(DEFAULT_CSV):
            quote = row["evidence_quote"].strip()
            self.assertTrue(quote, row["oga_id"])
            if not row["evidence_type"].strip():
                self.assertTrue(quote.startswith("[11-12 Aug]"), quote[:60])

    def test_every_new_withdrawal_in_the_committed_file_is_graded(self):
        # A withdrawal with no evidence type is held back, so an ungraded one
        # would be a row silently doing nothing rather than a decision.
        from pipeline.management.commands.import_antibody_availability import (
            DEFAULT_CSV, WITHDRAWAL_EVIDENCE,
        )
        graded = 0
        for row in self._committed(DEFAULT_CSV):
            moved = (row["availability_checked"] == "discontinued"
                     and row["previous_verdict"] != "discontinued")
            if moved:
                self.assertIn(row["evidence_type"], WITHDRAWAL_EVIDENCE,
                              row["oga_id"])
                graded += 1
        self.assertEqual(graded, 38)
        # 18 of the 38 clear the standard; the rest are held back on purpose.
        writes = sum(
            1 for row in self._committed(DEFAULT_CSV)
            if row["availability_checked"] == "discontinued"
            and row["previous_verdict"] != "discontinued"
            and WITHDRAWAL_EVIDENCE[row["evidence_type"]] is None)
        self.assertEqual(writes, 18)

    def test_every_verdict_the_recheck_moved_was_actually_rechecked(self):
        # A row stamped 30 Aug that nobody looked at, or a moved verdict stamped
        # with the old date, would both misdate the evidence for a withdrawal.
        from pipeline.management.commands.import_antibody_availability import (
            DEFAULT_CSV,
        )
        for row in self._committed(DEFAULT_CSV):
            rechecked = row["availability_checked"] != "not_rechecked"
            self.assertEqual(rechecked, row["checked_on"] == "2026-08-30",
                             row["oga_id"])
            if (row["availability_checked"] == "discontinued"
                    and row["previous_verdict"] != "discontinued"):
                # A new withdrawal was re-visited on the 30th, so it carries
                # that visit's own quote and not the earlier pass's. The other
                # moves -- out of stock, back on sale -- were not re-quoted,
                # and neither writes.
                quote = row["evidence_quote"].strip()
                self.assertTrue(quote, row["oga_id"])
                self.assertFalse(quote.startswith("[11-12 Aug]"), row["oga_id"])
